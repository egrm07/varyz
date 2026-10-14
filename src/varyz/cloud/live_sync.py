"""Realtime Cloud sync while the daemon runs: push at once, long-poll for the rest.

autosync syncs every 15 minutes. With the daemon up, a rule written here goes to
the cloud within a couple of seconds, and one written on another device arrives
as soon as the cloud has it:

* the watcher reads ``PRAGMA data_version`` every second; when another
  connection committed and `LocalSync.plan` has changes, it syncs;
* the listener holds ``GET /api/sync?wait=`` open; when it returns records, it
  wakes the watcher to sync. A cloud that does not hold the request (no
  ``waited`` in its answer) is polled every ``POLL_SECONDS`` instead.

Only what the owner selected for Cloud (`varyz cloud enable`) moves, through
`LocalSync`, as in every other sync; nothing runs until it is enabled. Errors are
never logged with their payloads: a failing cloud is retried with backoff and the
local archive stays as it is.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

WAIT_SECONDS = 25
POLL_SECONDS = 15.0
WATCH_SECONDS = 1.0
IDLE_SECONDS = 30.0  # how often a disabled or disconnected sync looks again
MAX_BACKOFF_SECONDS = 300.0


def journal_path(config: Path) -> Path:
    return config.with_name(config.stem + ".cloud-sync.json")


def _client(config: Path) -> Any | None:
    """A client for the selected cloud, or None while sync is off or disconnected."""
    from varyz.cloud.archive_sync import read_archive_journal
    from varyz.cloud.client import CloudClient, credential_name, load_tokens
    from varyz.cloud.local_sync import read_journal
    from varyz.cloud.session_report_sync import journal_path as report_path
    from varyz.cloud.session_report_sync import read_journal as read_report_journal

    path = journal_path(config)
    if not path.exists():
        return None
    try:
        journal = read_journal(path)
        archive = read_archive_journal(config.with_name(config.stem + ".cloud-archive.json"))
        report_journal = read_report_journal(report_path(config))
        if not (
            journal.get("enabled") or archive["enabled"] or report_journal["enabled"]
        ) or not journal.get("url"):
            return None
        name = credential_name(config, journal["url"])
        return CloudClient(journal["url"], load_tokens(name), credential=name)
    except Exception:
        return None


class LiveCloudSync:
    def __init__(
        self,
        config: Path,
        db_path: Path,
        *,
        client: Callable[[Path], Any | None] = _client,
        connect: Callable[[Path], sqlite3.Connection] | None = None,
    ) -> None:
        self.config = config
        self.db_path = db_path
        self._make_client = client
        self._connect = connect or _connect
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._threads: list[threading.Thread] = []
        self.syncs = 0  # syncs that ran, for status and tests

    def start(self) -> None:
        for target, name in (
            (self._watch, "varyz-cloud-watch"),
            (self._listen, "varyz-cloud-listen"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    # -- the watcher: sends what changed here, and syncs when woken --------------

    def _watch(self) -> None:
        conn = self._connect(self.db_path)
        try:
            version = _data_version(conn)
            backoff = 0.0
            while not self._stop.is_set():
                kicked = self._kick.wait(max(WATCH_SECONDS, backoff))
                self._kick.clear()
                if self._stop.is_set():
                    return
                current = _data_version(conn)
                changed, version = current != version, current
                if not (kicked or changed or backoff):
                    continue
                outcome = self._sync(conn, only_if_planned=not kicked and not backoff)
                if outcome is False:
                    backoff = min(MAX_BACKOFF_SECONDS, max(WATCH_SECONDS * 4, backoff * 2))
                else:
                    backoff = 0.0
        finally:
            conn.close()

    def _sync(self, conn: sqlite3.Connection, *, only_if_planned: bool) -> bool | None:
        """True synced, None nothing to do, False failed (retried with backoff)."""
        from varyz.cloud.archive_sync import ArchiveSync, read_archive_journal
        from varyz.cloud.local_sync import LocalSync
        from varyz.cloud.session_report_sync import ReportSync
        from varyz.cloud.session_report_sync import journal_path as report_path
        from varyz.cloud.session_report_sync import read_journal as read_report_journal
        from varyz.db.connection import LockHeldError

        try:
            sync = LocalSync(conn, journal_path(self.config))
            archive_path = self.config.with_name(self.config.stem + ".cloud-archive.json")
            archive_journal = read_archive_journal(archive_path)
            archive = ArchiveSync(conn, archive_path)
            report_journal = read_report_journal(report_path(self.config))
            report_sync = ReportSync(conn, self.config)
            preferences_enabled = bool(sync.journal.get("enabled"))
            if only_if_planned and not (
                preferences_enabled
                and sync.plan()
                or archive_journal["enabled"]
                and (archive_journal.get("pending_permits") or archive.plan()["changes"])
                or report_journal["enabled"]
                and (
                    report_journal["pending_permits"]
                    or report_journal["pending_revokes"]
                    or report_sync.plan()["changes"]
                )
            ):
                return None
            client = self._make_client(self.config)
            if client is None:
                return None
            try:
                from varyz.cloud.client import CloudStorageLimit

                storage_full = False
                if preferences_enabled:
                    try:
                        sync.sync(client)
                    except CloudStorageLimit:
                        storage_full = True
                if archive_journal["enabled"]:
                    from varyz.cloud.archive import CachedEmbedder

                    embedder = (
                        CachedEmbedder(archive_journal["model_cache"])
                        if archive_journal.get("semantic") and archive_journal.get("model_cache")
                        else None
                    )
                    outcome = archive.sync(client, sync.journal["account_id"], embedder=embedder)
                    storage_full = storage_full or bool(outcome.get("storage_full"))
                if report_journal["enabled"]:
                    outcome = report_sync.sync(client, sync.journal["account_id"])
                    storage_full = storage_full or bool(outcome.get("storage_full"))
                if storage_full:
                    return False  # Preserve pending records; retry with the existing backoff.
            finally:
                with contextlib.suppress(Exception):
                    client.close()
        except LockHeldError:
            return None  # another sync owns the journal and does the same work
        except Exception:
            # A conflict, an outage or a revoked sign-in: `varyz cloud status` says
            # which; nothing private is logged from a background thread.
            return False
        self.syncs += 1
        return True

    # -- the listener: waits on the cloud for another device's change ------------

    def _listen(self) -> None:
        from varyz.cloud.local_sync import read_journal

        backoff = 0.0
        kicked_at: int | None = None
        stuck = 0
        while not self._stop.is_set():
            client = self._make_client(self.config)
            if client is None:
                self._stop.wait(IDLE_SECONDS)
                continue
            try:
                while not self._stop.is_set():
                    cursor = int(read_journal(journal_path(self.config)).get("cursor") or 0)
                    page = client.pull(cursor, wait=WAIT_SECONDS)
                    backoff = 0.0
                    if page.get("records"):
                        # The same cursor again: the last sync did not get past
                        # it (a conflict), so wake the watcher less and less often.
                        stuck = stuck + 1 if cursor == kicked_at else 0
                        kicked_at = cursor
                        self._kick.set()
                        # The watcher syncs; give it the moment it needs first.
                        self._stop.wait(
                            WATCH_SECONDS * 2
                            if not stuck
                            else min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2**stuck)
                        )
                    elif not page.get("waited"):
                        self._stop.wait(POLL_SECONDS)
            except Exception:
                backoff = min(MAX_BACKOFF_SECONDS, max(POLL_SECONDS, backoff * 2))
                self._stop.wait(backoff)
            finally:
                with contextlib.suppress(Exception):
                    client.close()


def _connect(db_path: Path) -> sqlite3.Connection:
    from varyz.db.connection import open_db

    return open_db(db_path)


def _data_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


__all__ = ["LiveCloudSync", "journal_path"]
