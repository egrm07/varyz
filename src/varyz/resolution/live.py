"""Live reading: what the owner says to an AI agent reaches the archive at once.

autosync reads the AI chats every 15 minutes. A rule the owner states in a chat
should be in effect for their next task, so each prompt also nudges the daemon
(`POST /v1/live/nudge`), which reads just that connector a moment later, and a
live reader (`profile_reader.live_offer`) reads it on its first call. Both are
incremental: the connector reads only what was appended since its cursor.

It never waits for the archive: a running autosync, a held write lock or the
kill switch makes it skip, and the next nudge or autosync picks it up.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

CONNECTOR = "ai_conversations"
# The agent writes the prompt to its transcript as the hook runs; read a moment
# later so that prompt is there, and fold nudges that come together into one read.
NUDGE_DELAY_SECONDS = 2.0


def ingest_new_turns(cfg: Any, *, wait: float = 0.0) -> int | None:
    """Read the AI chats' new messages into the archive: how many, or None when it could not now.

    ``wait`` is how long to wait for another writer; a running autosync reads
    them itself.
    """
    from varyz.autosync import claim_path, is_paused

    if not cfg.connector_options(CONNECTOR).get("approved", False):
        return None
    if is_paused(cfg) or not cfg.db_path.is_file():
        return None
    deadline = time.monotonic() + wait
    while True:
        if claim_path(cfg).exists():
            return None
        try:
            return _ingest(cfg)
        except _Busy:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)


class _Busy(Exception):
    pass


def _ingest(cfg: Any) -> int:
    from varyz.connectors.base import get_connector
    from varyz.db.connection import LockHeldError, WriteLock, open_db
    from varyz.pipeline.ingest import sync_connector
    from varyz.vault import Vault

    conn = open_db(cfg.db_path)
    try:
        try:
            with WriteLock(cfg.data_dir):
                stats = sync_connector(conn, Vault(cfg.vault_dir), get_connector(CONNECTOR), cfg)
        except LockHeldError:
            raise _Busy from None
    finally:
        conn.close()
    return stats.new + stats.updated


class LiveIngest:
    """The daemon's side: a nudge reads the chats once, shortly after, in the background."""

    def __init__(self, cfg: Any, *, delay: float = NUDGE_DELAY_SECONDS,
                 ingest: Callable[[Any], int | None] | None = None) -> None:
        self.cfg = cfg
        self.delay = delay
        self._ingest = ingest or ingest_new_turns
        self._guard = threading.Lock()
        self._running = False
        self._again = False

    def nudge(self) -> bool:
        """Read soon; True when this started a read, False when one was already coming."""
        with self._guard:
            if self._running:
                # A prompt that came in while a read ran is read right after it.
                self._again = True
                return False
            self._running = True
        threading.Thread(target=self._run, name="varyz-live-ingest", daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            while True:
                time.sleep(self.delay)
                with contextlib.suppress(Exception):  # the next nudge or autosync reads it
                    self._ingest(self.cfg)
                with self._guard:
                    if not self._again:
                        self._running = False
                        return
                    self._again = False
        except BaseException:
            with self._guard:
                self._running = False
            raise


__all__ = ["CONNECTOR", "LiveIngest", "ingest_new_turns"]
