"""The second look `recall` takes before handing evidence to an agent.

First-stage retrieval -- keywords and one embedding per question -- decides
what is worth reading. It is fast and broad, and it is bad at the question an
agent actually asks. Measured on `recall_bench_v1` (4,683 records), the record
that answers "can I still send back the standing desk?" is the Flexidesk
delivery email, and the one that says *how* is a separate returns-policy email
sharing no word with the question; the first stage ranked the first 99th by
meaning and never found the second.

Two things close that gap, both local:

  rerank  a cross-encoder reads the question and each candidate together and
          scores how well the candidate answers it. A bi-encoder compared two
          vectors made separately; this compares the texts.
  chain   the records the cross-encoder trusts most are themselves searched
          for, so a delivery email brings back the policy from the same
          vendor, a commit brings back the chat that proposed it, an alert
          brings back its postmortem. Every record found this way is still
          scored against the original question before it is kept.

The model is downloaded once, like the embedding model, and runs on an NVIDIA
card when onnxruntime can reach one (the Windows installer adds DirectML for
it). On the processor it is off unless the owner turns it on: measured on an
8 GB Apple M1 (2026-10-27), one `recall` took 8-10 s and 25-38 s of CPU, and
three agents asking at once took up to 60 s each and pushed the load average
from 4 to 208. When it is on there, it uses two threads, one process at a time
on the machine, and gives up inside `recall`'s time budget.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from varyz.config import Config

# MIT-licensed and built on a multilingual encoder; the stronger multilingual
# reranker in the same registry is licensed for non-commercial use only, which
# an archive tool cannot ship as a default.
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


@dataclass(frozen=True)
class RerankCalibration:
    """Where this model's scores mean something. Logits, so model-specific.

    margin      a candidate scoring more than this below the best one is
                noise next to it, however high it ranks in absolute terms
    confident   a best score at or above this is an answer even when the
                embedding distance alone would have abstained
    floor       nothing scoring below this is evidence, however close it
                sits to a weak best
    """

    margin: float
    confident: float
    floor: float


# Measured on recall_bench_v1 dev only; see reports/RECALL_BENCH_*.md.
CALIBRATIONS: dict[str, RerankCalibration] = {
    "BAAI/bge-reranker-base": RerankCalibration(margin=4.0, confident=-6.0, floor=-8.0),
}

# The cross-encoder reads the question and the passage together, so the cost
# grows with the passage. The best chunk is already the part that matched.
_MAX_DOC_CHARS = 1200

# Agents ask in sentences (a median 175 characters in the owner's transcripts,
# against 50 on the benchmark), and on 16 CPU threads the cross-encoder spent
# 3.6 s of a 5.5 s `ask` reading them against about 47 passages. Through
# DirectML on an RTX 5070 the same calls took 121 ms instead of 1,525 ms, with
# the same scores to four places (2026-09-27).
_GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider")
# What the DirectML session took on that card: 1,083 MiB loaded, 1,789 MiB
# after 40 of the owner's questions. It goes on the card only if 30% of the
# card is still free after it, the classifier's rule, so a training run or a
# second varyz process leaves it on the processor.
_GPU_MIB = 2048
_VRAM_HEADROOM = 0.3
# On the processor: onnxruntime otherwise takes every core, in every agent's
# process at once.
CPU_THREADS = 2

# The moment, on the monotonic clock, after which `score` stops and raises
# `RerankBudgetSpent`; set per call by `recall` (a context variable, so the
# daemon's threads each keep their own).
_DEADLINE: ContextVar[float | None] = ContextVar("varyz_rerank_deadline", default=None)


class RerankBudgetSpent(RuntimeError):
    """The second look ran past the caller's time budget."""


@contextmanager
def deadline(seconds: float | None) -> Iterator[None]:
    token = _DEADLINE.set(None if seconds is None else time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _past_deadline() -> bool:
    limit = _DEADLINE.get()
    return limit is not None and time.monotonic() > limit


@contextmanager
def machine_slot(cfg: Config) -> Iterator[bool]:
    """True when this process may run the cross-encoder now.

    Every agent thread has its own MCP process with its own copy of the model;
    on the processor, one at a time on the machine. A busy slot is not waited
    for: the caller answers without the second look.
    """
    from varyz.db.connection import FileLock

    lock = FileLock(cfg.data_dir / "locks" / "rerank.lock")
    try:
        held = lock.acquire()
    except OSError:
        held = False
    try:
        yield held
    finally:
        if held:
            lock.release()


def gpu_reachable() -> bool:
    try:
        return _providers() is not None
    except Exception:  # no onnxruntime, or a driver query that failed
        return False


def _providers() -> list[str] | None:
    """A GPU for the cross-encoder, or None for onnxruntime's default, the CPU.

    Only an NVIDIA card counts, as for the judge. DirectML also offers
    integrated graphics and a software renderer; neither was measured, and
    both are likely slower than the processor. The card is read once, when the
    model loads.
    """
    import onnxruntime

    available = onnxruntime.get_available_providers()
    name = next((name for name in _GPU_PROVIDERS if name in available), None)
    if name is None:
        return None
    from varyz.resolution.local_classifier import gpu_memory_mib

    memory = gpu_memory_mib()
    if memory is None:
        return None
    used, total = memory
    if used + _GPU_MIB > total * (1 - _VRAM_HEADROOM):
        return None
    return [name, "CPUExecutionProvider"]


class Reranker:
    """Lazy wrapper around fastembed's cross-encoder."""

    def __init__(self, cfg: Config, model_name: str | None = None):
        self.model_name = model_name or cfg.retrieval.rerank_model
        self.cache_dir = os.environ.get("VARYZ_EMBEDDING_CACHE", str(cfg.data_dir / "models"))
        self._model = None
        # A DirectML session runs one call at a time; the daemon's questions
        # are read on threads of their own.
        self._gpu = False
        self._lock = threading.Lock()

    @property
    def on_gpu(self) -> bool:
        return self._gpu

    @property
    def calibration(self) -> RerankCalibration | None:
        return CALIBRATIONS.get(self.model_name)

    def is_downloaded(self) -> bool:
        folder = "models--" + self.model_name.replace("/", "--")
        return (Path(self.cache_dir) / folder).is_dir()

    def load(self):
        """Load the model now rather than on the first question."""
        return self._ensure()

    def _ensure(self):
        if self._model is None:
            from varyz.pipeline.embed import _prefer_copies_to_symlinks, _quiet_hub_notices

            _prefer_copies_to_symlinks()
            _quiet_hub_notices()
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            if not self.is_downloaded():
                print(
                    "First use: fetching the local answer-checking model "
                    "(about 1 GB, into the models folder). This happens once.",
                    file=sys.stderr,
                )
            providers = _providers()
            try:
                self._model = TextCrossEncoder(
                    self.model_name, cache_dir=self.cache_dir, providers=providers,
                    threads=None if providers else CPU_THREADS,
                )
            except Exception:
                if providers is None:
                    raise
                # A card the runtime lists but cannot open leaves the processor.
                self._model = TextCrossEncoder(
                    self.model_name, cache_dir=self.cache_dir, threads=CPU_THREADS
                )
            else:
                self._gpu = providers is not None
        return self._model

    def score(self, question: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        texts = [passage[:_MAX_DOC_CHARS] for passage in passages]
        # A batch is padded to its longest passage; batches of passages of
        # about the same length waste the least. Measured 15% faster.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        model = self._ensure()
        # On the card, batches of 16 took 121 ms a call against 142 ms for 8.
        batch_size = 16 if self._gpu else 8
        if _past_deadline():
            raise RerankBudgetSpent
        scored: list[float] = []
        with self._lock if self._gpu else nullcontext():
            for start in range(0, len(order), batch_size):
                if _past_deadline():
                    raise RerankBudgetSpent
                batch = [texts[i] for i in order[start:start + batch_size]]
                scored.extend(model.rerank(question, batch, batch_size=batch_size))
        scores = [0.0] * len(texts)
        for i, score in zip(order, scored, strict=True):
            scores[i] = float(score)
        return scores


def load_reranker(cfg: Config) -> Reranker | None:
    """The configured reranker, or None when it is off or has no calibration.

    `auto` uses the model only once it is on disk, so an agent's first
    question never stalls behind a gigabyte download; the first index
    build fetches it (`fetch_reranker`). It also needs a graphics card: on
    the processor the second look cost a Mac 25-38 s of CPU a question (see
    the module note). `on` fetches it on first use and runs it anywhere.
    """
    mode = cfg.retrieval.rerank
    if mode == "off":
        return None
    reranker = Reranker(cfg)
    if reranker.calibration is None:
        return None
    if mode == "auto" and (not reranker.is_downloaded() or not gpu_reachable()):
        return None
    return reranker


def worth_fetching(cfg: Config) -> bool:
    """Whether `auto` should download the 1.1 GB reranker at all.

    Measured 2026-10-22, it earned its place only by shortening the list a
    judge reads; without a judge the answers were the same. So `auto` fetches
    it only where a judge runs, and `on` fetches it regardless.
    """
    if cfg.retrieval.rerank != "auto":
        return cfg.retrieval.rerank == "on"
    if not gpu_reachable():  # `auto` would never load it
        return False
    from varyz.retrieval.judge import load_judge

    try:
        return load_judge(cfg) is not None
    except Exception:
        return False


def fetch_reranker(cfg: Config) -> bool:
    """Download the configured reranker if `recall` would use it and it is absent.

    A failed download is reported and skipped: `recall` answers without the
    second look until the next index build succeeds, which is worse evidence,
    not a broken archive.
    """
    if cfg.retrieval.rerank == "off" or os.environ.get("VARYZ_DISABLE_EMBEDDINGS") == "1":
        return False
    reranker = Reranker(cfg)
    if reranker.calibration is None or reranker.is_downloaded():
        return False
    if not worth_fetching(cfg):
        return False
    try:
        reranker.load()
    except Exception as exc:  # network, disk, or a registry change
        print(f"Could not fetch the answer-checking model: {exc}", file=sys.stderr)
        return False
    return True


# "now", "these days", "currently": the newest statement of a changing fact
# is the answer, and an older one is history.
_CURRENT = re.compile(
    r"\b(now|nowadays|these days|current(ly)?|at the moment|latest|still|anymore|today)\b",
    re.IGNORECASE,
)


def asks_for_current_state(question: str) -> bool:
    return bool(_CURRENT.search(question))
