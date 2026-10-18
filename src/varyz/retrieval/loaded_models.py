"""The judged `recall` path's models, for a process that answers for hours.

The resolution daemon answers `ask` from the archive (tier 2). Keyword search
alone found the records for 11 of 27 `recall_bench` held-out questions there;
the embedder, the cross-encoder and the owner's local model reading the
shortlist found them for 19, without a fabricated TRR answer (see CHANGELOG). The three
start loading on the first question that can use them, which takes keyword search
rather than wait 1.5-3 s for them, and are let go of after `IDLE_SECONDS` without
one, as the MCP server does with its own.

The judge decides whether the other two are worth loading at all: tier 2
answers from them only when the judge has read the records, so without a
local model the daemon keeps to keyword search and loads nothing.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from varyz.config import Config

# The MCP server's figure: the models hold about 1.8 GB of the process's
# memory, and loading them again costs the next question two to four seconds.
IDLE_SECONDS = 300.0
# How long "no local model" holds before it is asked again: Ollama may be
# started, or a model pulled, while the daemon runs.
RECHECK_SECONDS = 60.0


class RetrievalModels:
    """Embedder, reranker and judge, loaded together on first use."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._loaded: tuple[Any, Any, Any] | None = None
        self._checked_at: float | None = None
        self._loading: threading.Thread | None = None
        self._release_timer: threading.Timer | None = None

    def get(self) -> tuple[Any, Any, Any] | None:
        """(embedder, reranker, judge) once loaded; until then None, and loads them."""
        with self._lock:
            loaded = self._loaded
            if loaded is not None:
                self._release_later()
                return loaded
            now = time.monotonic()
            recently = self._checked_at is not None and now - self._checked_at < RECHECK_SECONDS
            if self._loading is None and not recently:
                self._checked_at = now
                self._loading = threading.Thread(
                    target=self._load_now, name="varyz-retrieval-models", daemon=True
                )
                self._loading.start()
            return None

    def wait(self, timeout: float | None = None) -> tuple[Any, Any, Any] | None:
        """What `get` returns once a load it started has finished."""
        self.get()
        loading = self._loading
        if loading is not None:
            loading.join(timeout)
        with self._lock:
            return self._loaded

    def _load_now(self) -> None:
        loaded = self._load()
        with self._lock:
            self._loaded = loaded
            self._loading = None
            if loaded is not None:
                self._release_later()

    def _load(self) -> tuple[Any, Any, Any] | None:
        if os.environ.get("VARYZ_DISABLE_EMBEDDINGS") == "1":
            return None
        try:
            from varyz.retrieval.judge import load_judge
            from varyz.retrieval.rerank import load_reranker

            reranker = load_reranker(self._cfg)
            # The judge reads the reranker's shortlist; without one it has none.
            judge = load_judge(self._cfg) if reranker is not None else None
            if judge is None:
                return None
            from varyz.pipeline.embed import Embedder

            reranker.load()
            return Embedder(self._cfg), reranker, judge
        except Exception:
            # Keyword evidence is a worse answer, not no answer.
            return None

    def release(self) -> None:
        """Let go of the models; the next question loads them again."""
        with self._lock:
            if self._release_timer is not None:
                self._release_timer.cancel()
                self._release_timer = None
            if self._loaded is None:
                return
            self._loaded = None
            self._checked_at = None
            gc.collect()

    def _release_later(self) -> None:
        if self._release_timer is not None:
            self._release_timer.cancel()
        timer = threading.Timer(IDLE_SECONDS, self.release)
        timer.daemon = True
        timer.start()
        self._release_timer = timer
