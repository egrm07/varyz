"""One closed question, answered on this machine: the shared local classifier.

Several calls in varyz are a yes-or-no about the owner's own words: is this prompt
work or a question, is this task new or a small change, does it redo a step, is
this answer a habit or a one-off, does this stored decision answer that question.
Each is put the same way, in the manner of a Jev decision: one closed question,
answered in one pass by a small local model, one letter read from the first
token's log-probabilities. The input is the owner's, so the model is reached on
loopback only and never loaded when it would crowd a running GPU job.

Each use is a chain, tried in order; the first that answers decides:

* ``LocalModel`` - Ollama on loopback, a few authored examples, the letter A or B;
* ``Centroids`` - the archive's own sentence embedding, nearest of two centroids of
  the same authored examples, no GPU;
* the rule that was there before - regexes or a verb list - which always answers.

Every use is measured on an authored held-out set that no prompt or example here
was tuned on; see reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md.
"""

from __future__ import annotations

import math
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from varyz.llm.request_deadline import request_with_deadline

# How long a loaded model stays on the card after its last answer, unless
# `[workflows] classifier_keep_alive` says otherwise. On 1,848 of the owner's
# prompts, 10 minutes finds it loaded for 75% of them (5 minutes: 62%, 30: 86%)
# for 4.2 GB-hours of VRAM a day; longer holds 3 GB a GPU job may want.
KEEP_ALIVE = "10m"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
READY_FOR = 60.0  # seconds a readiness probe holds
# The window every request loads the model with. `recall`'s judge asks the same
# model about a 40-record shortlist and needs 8k; Ollama reloads a model whose
# window changes, so a classifier left on the 4k default and a judge on 8k
# reloaded it at every switch (1,194 loads in a day's server log, each answer
# 4-5 s instead of 0.5 s). 8k costs qwen3.5:4b 204 MiB more than 4k.
CONTEXT_WINDOW = 8192


class ClassifierUnavailable(RuntimeError):
    """This link of a chain cannot answer now; the next one is asked."""


@dataclass(frozen=True)
class Question:
    """A closed question with two answers, A and B, and authored examples.

    ``few_shot`` holds (turn text, letter) pairs shown to the model before the
    input, in order. ``examples`` holds (text, is_a) pairs from the same dev set
    for the embedding's centroids.
    """

    name: str
    instruction: str
    few_shot: tuple[tuple[str, str], ...]
    examples: tuple[tuple[str, bool], ...] = ()
    input_label: str = "Request"

    def turn(self, text: str) -> str:
        return f"{self.input_label}: {' '.join(text.split())[:2000]}\nAnswer (A or B):"

    def messages(self, text: str, *, few_shot: bool = True) -> list[dict[str, str]]:
        """The chat for one input: the instruction, examples if asked, the input."""
        messages = [{"role": "system", "content": self.instruction}]
        if few_shot:
            for example, letter in self.few_shot:
                messages.append({"role": "user", "content": self.turn(example)})
                messages.append({"role": "assistant", "content": letter})
        messages.append({"role": "user", "content": self.turn(text)})
        return messages


def gpu_memory_mib() -> tuple[int, int] | None:
    """(used, total) MiB of the first NVIDIA card, or None when there is none."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed local tool, fixed arguments
            [exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3.0, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        used, total = (int(part) for part in out.splitlines()[0].split(","))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return used, total


def loaded_mib(size_bytes: int) -> int:
    """What a model takes on the card once Ollama loads it, from its size on disk.

    The weights are not all of it: the KV cache, compute buffers and the runner's
    CUDA context come on top. Measured 2026-09-25 on this RTX 5070: gemma4 12B,
    7248 MiB on disk, took 8246 MiB (+14%, about 1 GiB). Weights + 15% + 512 MiB
    covers that with room.
    """
    return int(size_bytes / (1024 * 1024) * 1.15) + 512


# A model whose weights take more than half the machine's memory runs, but
# pushes everything else to swap: qwen3.5:4b on an 8 GB Mac left 12-19% free.
# Swap needs disk, and a Mac whose disk filled up while it ran restarted
# (2026-09-29).
MIN_FREE_DISK_MIB = 2 * 1024
EASY_FREE_DISK_MIB = 10 * 1024
OTHERS_MIB = 2 * 1024  # what the system and the owner's apps need beside it


def ram_mib() -> int | None:
    """This machine's memory in MiB (a Mac's unified memory), or None when unknown."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (AttributeError, ValueError, OSError):
        return None


def free_disk_mib(path: str | os.PathLike[str]) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


def machine_fit(
    size_bytes: int,
    *,
    free_disk: Callable[[], int],
    gpu_memory: Callable[[], tuple[int, int] | None] = gpu_memory_mib,
    ram: Callable[[], int | None] = ram_mib,
) -> tuple[str, str]:
    """How this machine runs a model this size: 'fits', 'tight' or 'no', and why.

    An NVIDIA card is checked when the model loads (``LocalModel.ready``); only
    the disk is checked here for it. Without one the model shares the memory
    everything else runs in, which is what this checks.
    """
    disk = free_disk()
    if disk < MIN_FREE_DISK_MIB:
        return "no", f"only {disk / 1024:.1f} GB of disk is free"
    need = loaded_mib(size_bytes)
    total = ram() if gpu_memory() is None else None
    if total is not None:
        if total - need < OTHERS_MIB:
            return "no", (
                f"the model needs about {need / 1024:.1f} GB of this machine's "
                f"{total / 1024:.0f} GB memory"
            )
        if need > total / 2:
            return "tight", (
                f"the model takes {need / 1024:.1f} GB of this machine's "
                f"{total / 1024:.0f} GB memory"
            )
    if disk < EASY_FREE_DISK_MIB:
        return "tight", f"only {disk / 1024:.1f} GB of disk is free"
    return "fits", ""


class LocalModel:
    """A small local model through Ollama, asked for one letter."""

    def __init__(
        self,
        model: str,
        *,
        url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 10.0,
        vram_headroom: float = 0.3,
        gpu_memory: Callable[[], tuple[int, int] | None] = gpu_memory_mib,
        when_cold: str = "load",
        keep_alive: str = KEEP_ALIVE,
    ) -> None:
        parts = urlsplit(url)
        if parts.hostname not in _LOOPBACK:
            # The owner's words are the input: they never go to another machine.
            raise ValueError(f"the local classifier runs on this machine only, not {url}")
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
        self.model = model
        self.host = host
        self.port = parts.port or 11434
        self.url = f"{parts.scheme}://{'[::1]' if host == '::1' else host}:{self.port}"
        self.timeout_seconds = timeout_seconds
        self.vram_headroom = vram_headroom
        self._gpu_memory = gpu_memory
        # 'load': a question waits for the model to load (about 4 s). 'warm': it is
        # answered only by a loaded model; a cold one is loaded in a separate
        # process for the next question, and this one falls to the next link.
        # 'skip': only a loaded model answers, and a cold one is left cold.
        self.when_cold = when_cold
        self.keep_alive = keep_alive
        # A probe holds for a minute: a hook asks one or two questions back to
        # back. It must not hold longer: once the model unloads, a job may have
        # taken the card, and the next load is checked again.
        self._checked_at: float | None = None

    def p_a(self, question: Question, text: str, *, few_shot: bool = True) -> float:
        """P(A) against P(B) for one input."""
        self.ready()
        try:
            response = request_with_deadline(
                "POST", f"{self.url}/api/chat",
                timeout_seconds=self.timeout_seconds, server_name="Ollama server",
                request_function=httpx.post,
                json={
                    "model": self.model,
                    "messages": question.messages(text, few_shot=few_shot),
                    "stream": False,
                    "think": False,
                    "logprobs": True,
                    "top_logprobs": 10,
                    "keep_alive": self.keep_alive,
                    "options": {"temperature": 0.0, "num_predict": 1, "num_ctx": CONTEXT_WINDOW},
                },
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            self._checked_at = None
            raise ClassifierUnavailable(f"the model did not answer: {exc}") from exc
        return p_first_letter(body)

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = request_with_deadline(
                "GET", f"{self.url}{path}", timeout_seconds=2.0,
                server_name="Ollama server", request_function=httpx.get,
            )
            response.raise_for_status()
            return dict(response.json())
        except Exception as exc:
            raise ClassifierUnavailable(f"Ollama did not answer {path}: {exc}") from exc

    def size_bytes(self) -> int:
        """The model's size on disk, from Ollama; ``ClassifierUnavailable`` if unknown."""
        base = self.model.split(":")[0]
        for entry in self._get("/api/tags").get("models", []):
            name = str(entry.get("name", ""))
            if name == self.model or (":" not in self.model and name.split(":")[0] == base):
                return int(entry.get("size", 0))
        raise ClassifierUnavailable(f"{self.model} is not pulled")

    def ready(self) -> None:
        """Ollama is up, the model is pulled, and answering will not crowd the card."""
        if self._checked_at is not None and time.monotonic() - self._checked_at < READY_FOR:
            return
        try:
            with socket.create_connection((self.host, self.port), timeout=0.3):
                pass
        except OSError as exc:
            # The standalone Ollama setup installs has nothing that starts it
            # at login; it is started here, for the next question.
            from varyz.resolution.ollama_setup import start_recorded

            if start_recorded(self.host, self.port):
                raise ClassifierUnavailable("Ollama is starting for the next question") from exc
            raise ClassifierUnavailable("Ollama is not running") from exc
        base = self.model.split(":")[0]

        def named(entry: dict[str, Any]) -> bool:
            name = str(entry.get("name", ""))
            return name == self.model or (":" not in self.model and name.split(":")[0] == base)

        if any(named(entry) for entry in self._get("/api/ps").get("models", [])):
            self._checked_at = time.monotonic()
            return
        pulled = [entry for entry in self._get("/api/tags").get("models", []) if named(entry)]
        if not pulled:
            raise ClassifierUnavailable(f"{self.model} is not pulled")
        memory = self._gpu_memory()
        if memory is not None:
            used, total = memory
            need = loaded_mib(int(pulled[0].get("size", 0)))
            if total - used - need < self.vram_headroom * total:
                raise ClassifierUnavailable(
                    f"loading {self.model} would leave the GPU under "
                    f"{self.vram_headroom:.0%} free ({used} of {total} MiB in use)"
                )
        if self.when_cold == "warm":
            self.warm_in_background()
            raise ClassifierUnavailable(f"{self.model} is loading for the next question")
        if self.when_cold == "skip":
            raise ClassifierUnavailable(f"{self.model} is not loaded")
        self._checked_at = time.monotonic()

    def warm_in_background(self) -> None:
        """Load the model in a detached process; Ollama drops a load its caller leaves."""
        script = (
            "import sys, httpx; httpx.post(sys.argv[1] + '/api/generate', "
            "json={'model': sys.argv[2], 'keep_alive': sys.argv[3], "
            "'options': {'num_ctx': int(sys.argv[4])}}, timeout=120.0)"
        )
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        subprocess.Popen(  # noqa: S603 - fixed local module invocation, loopback URL
            [sys.executable, "-c", script, self.url, self.model, self.keep_alive,
             str(CONTEXT_WINDOW)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
            start_new_session=sys.platform != "win32",
        )

    # Kept for callers written against the task sizer.
    _ready = ready


def p_first_letter(body: dict[str, Any]) -> float:
    """P(A) against P(B) in the first token's top log-probabilities."""
    first = (body.get("logprobs") or [{}])[0]
    mass = {"A": 0.0, "B": 0.0}
    for entry in first.get("top_logprobs") or [first]:
        token = str(entry.get("token", "")).strip().upper()
        if token in mass and "logprob" in entry:
            mass[token] += math.exp(float(entry["logprob"]))
    total = mass["A"] + mass["B"]
    if total > 0:
        return mass["A"] / total
    letter = str(body.get("message", {}).get("content", "")).strip().upper()[:1]
    if letter in mass:
        return 1.0 if letter == "A" else 0.0
    raise ClassifierUnavailable("the model answered neither A nor B")


class Centroids:
    """The archive's own sentence embedding, nearest of two authored centroids."""

    def __init__(
        self,
        embed: Callable[[list[str]], list[list[float]]],
        examples: Sequence[tuple[str, bool]],
    ) -> None:
        self._embed = embed
        self._examples = tuple(examples)
        self._centroids: tuple[list[float], list[float]] | None = None

    def is_a(self, text: str) -> bool:
        a, b = self._fit()
        vector = unit(self.vectors([" ".join(text.split())[:2000]])[0])
        return dot(vector, a) >= dot(vector, b)

    def vectors(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embed(texts)
        except Exception as exc:
            raise ClassifierUnavailable(f"the local embedding is not available: {exc}") from exc

    def _fit(self) -> tuple[list[float], list[float]]:
        if self._centroids is None:
            vectors = [unit(v) for v in self.vectors([text for text, _ in self._examples])]
            a = [v for v, (_, is_a) in zip(vectors, self._examples, strict=True) if is_a]
            b = [v for v, (_, is_a) in zip(vectors, self._examples, strict=True) if not is_a]
            self._centroids = (mean(a), mean(b))
        return self._centroids


def unit(vector: Sequence[float]) -> list[float]:
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


def mean(vectors: Sequence[Sequence[float]]) -> list[float]:
    return [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass(frozen=True)
class Verdict:
    """The answer, what gave it ('model', 'embedding' or 'rule'), and P(A) if known."""

    is_a: bool
    by: str
    p: float | None = None


class BinaryClassifier:
    """The model, then the embedding, then the rule; the first that answers decides.

    ``threshold`` is the P(A) the model must reach to answer A. Uses where a wrong
    A costs more than a wrong B (a one-off choice stored as a habit) set it high.
    """

    def __init__(
        self,
        question: Question,
        rule: Callable[[str], bool],
        *,
        model: LocalModel | None = None,
        centroids: Centroids | None = None,
        threshold: float = 0.5,
    ) -> None:
        self.question = question
        self.rule = rule
        self.model = model
        self.centroids = centroids
        self.threshold = threshold
        self.skipped: list[str] = []

    def __call__(self, text: str) -> Verdict:
        if self.model is not None:
            try:
                p = self.model.p_a(self.question, text)
                return Verdict(p >= self.threshold, "model", p)
            except ClassifierUnavailable as exc:
                self.skipped.append(str(exc))
        if self.centroids is not None:
            try:
                return Verdict(self.centroids.is_a(text), "embedding")
            except ClassifierUnavailable as exc:
                self.skipped.append(str(exc))
        return Verdict(self.rule(text), "rule")


Mode = str  # 'model' | 'embedding' | 'rule' (the task sizer calls its rule 'lexicon')


def local_model_from_config(
    cfg: Any,
    *,
    gpu_memory: Callable[[], tuple[int, int] | None] = gpu_memory_mib,
    when_cold: str = "load",
) -> LocalModel:
    """The one local model `[workflows]` configures, shared by every use."""
    settings = cfg.workflows
    return LocalModel(
        settings.classifier_model,
        url=settings.classifier_url,
        timeout_seconds=settings.classifier_timeout_seconds,
        vram_headroom=settings.classifier_vram_headroom,
        gpu_memory=gpu_memory,
        when_cold=when_cold,
        keep_alive=settings.classifier_keep_alive,
    )


def embedder_from_config(cfg: Any) -> Callable[[list[str]], list[list[float]]]:
    """The archive's own sentence embedding; loaded on first use, not here."""
    from varyz.pipeline.embed import Embedder

    return Embedder(cfg).embed_documents


def classifier_from_config(
    cfg: Any,
    question: Question,
    rule: Callable[[str], bool],
    mode: Mode,
    *,
    threshold: float = 0.5,
    model: LocalModel | None = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> BinaryClassifier:
    """A chain for one use: `mode` says where it starts (model, embedding or rule)."""
    chain_model = None
    centroids = None
    if mode == "model":
        chain_model = model or local_model_from_config(cfg)
    if mode in ("model", "embedding") and question.examples:
        centroids = Centroids(embed or embedder_from_config(cfg), question.examples)
    return BinaryClassifier(
        question, rule, model=chain_model, centroids=centroids, threshold=threshold
    )


__all__ = [
    "CONTEXT_WINDOW",
    "KEEP_ALIVE",
    "BinaryClassifier",
    "Centroids",
    "ClassifierUnavailable",
    "LocalModel",
    "Question",
    "Verdict",
    "classifier_from_config",
    "embedder_from_config",
    "free_disk_mib",
    "gpu_memory_mib",
    "loaded_mib",
    "local_model_from_config",
    "machine_fit",
    "p_first_letter",
    "ram_mib",
]
