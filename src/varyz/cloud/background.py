"""Opt-in sync in the existing refresh cycle, without starting another daemon."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import keyring

from varyz.cloud.archive_sync import ArchiveSync, read_archive_journal
from varyz.cloud.client import (
    CloudAccessInactive,
    CloudClient,
    CloudError,
    credential_name,
    load_tokens,
)
from varyz.cloud.local_sync import LocalSync, read_journal, save_journal
from varyz.cloud.session_report_sync import ReportSync
from varyz.cloud.session_report_sync import journal_path as report_path
from varyz.cloud.session_report_sync import read_journal as read_report_journal
from varyz.db.connection import LockHeldError

STORAGE_FULL = (
    "Varyz Cloud storage is full, so new history and reports are not uploaded. "
    "Free space in Cloud, or pick fewer projects with varyz cloud projects. "
    "Local Varyz keeps working."
)


def health_path(config: Path) -> Path:
    return config.with_name(config.stem + ".cloud-health.json")


def read_health(config: Path) -> dict[str, Any]:
    """The last background sync's outcome: ok, the plain problem, and when."""
    try:
        value = json.loads(health_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _record(config: Path, ok: bool, message: str = "", **extra: Any) -> None:
    with contextlib.suppress(OSError, ValueError):
        save_journal(
            health_path(config),
            {"ok": ok, "message": message, "checked_at": time.time(), **extra},
        )


def health(config: Path) -> tuple[bool, str] | None:
    """What doctor and `varyz cloud status` say about Cloud; None when not connected."""
    try:
        journal = read_journal(config.with_name(config.stem + ".cloud-sync.json"))
    except (OSError, ValueError):
        return False, "the Cloud sync journal cannot be read; run varyz cloud status"
    if not journal.get("url"):
        return None
    full = False
    for path in (config.with_name(config.stem + ".cloud-archive.json"), report_path(config)):
        with contextlib.suppress(OSError, ValueError):
            full = full or bool(json.loads(path.read_text(encoding="utf-8")).get("storage_full"))
    if full:
        return False, STORAGE_FULL
    last = read_health(config)
    if last and not last.get("ok"):
        return False, str(last.get("message") or "the last Cloud sync did not finish")
    if last.get("offline"):
        return True, "connected; Cloud could not be reached at the last sync (offline?)"
    return True, "connected; last background sync finished" if last else "connected"


def sync_if_selected(config: Path, conn: sqlite3.Connection) -> str | None:
    path = config.with_name(config.stem + ".cloud-sync.json")
    if not path.exists():
        return None
    try:
        journal = read_journal(path)
        archive_path = config.with_name(config.stem + ".cloud-archive.json")
        archive_journal = read_archive_journal(archive_path)
        report_journal = read_report_journal(report_path(config))
        if not journal.get("url") or not (journal.get("enabled") or archive_journal["enabled"]
                                          or report_journal["enabled"]):
            return None
        name = credential_name(config, journal["url"])
        client = CloudClient(journal["url"], load_tokens(name), credential=name)
        storage_full = False
        blocked = 0
        try:
            result = {"uploaded": 0, "downloaded": 0}
            if journal.get("enabled"):
                result = LocalSync(conn, path).sync(client)
            if archive_journal["enabled"]:
                from varyz.cloud.archive import CachedEmbedder

                embedder = (
                    CachedEmbedder(archive_journal["model_cache"])
                    if archive_journal.get("semantic") and archive_journal.get("model_cache")
                    else None
                )
                archive_result = ArchiveSync(conn, archive_path).sync(
                    client,
                    journal["account_id"],
                    embedder=embedder,
                )
                result["uploaded"] += archive_result["uploaded"] + archive_result["deleted"]
                storage_full = storage_full or bool(archive_result.get("storage_full"))
                blocked += int(archive_result.get("blocked") or 0)
            if report_journal["enabled"]:
                report_result = ReportSync(conn, config).sync(client, journal["account_id"])
                result["uploaded"] += report_result["uploaded"] + report_result["deleted"]
                storage_full = storage_full or bool(report_result.get("storage_full"))
                blocked += int(report_result.get("blocked") or 0)
        finally:
            client.close()
        if storage_full:
            # Said every cycle: a full Cloud used to look like "nothing to upload".
            _record(config, False, STORAGE_FULL, storage_full=True, withheld=blocked)
            return STORAGE_FULL
        _record(config, True, withheld=blocked)
        if not result["uploaded"] and not result["downloaded"]:
            return None
        return f"Cloud synced: {result['uploaded']} sent, {result['downloaded']} received"
    except LockHeldError:
        return None  # another authorized sync owns the journal
    except CloudAccessInactive as error:
        from varyz.cloud.local_access import observe

        with contextlib.suppress(Exception):
            observe(config, error.access)
        _record(config, False, str(error))
        return str(error)
    except CloudError as error:
        if isinstance(error.__cause__, httpx.HTTPError):
            # Offline for a moment is not a problem to repair; the next cycle retries.
            _record(config, True, offline=True)
            return None
        # CloudError messages are written to be safe: no server payloads.
        message = f"Cloud sync did not finish: {error}"
        _record(config, False, message)
        return message
    except keyring.errors.KeyringError:
        message = (
            "Cloud sync did not finish: the Cloud sign-in could not be read from the "
            "keychain; run varyz cloud connect. Local preferences remain available."
        )
        _record(config, False, message)
        return message
    except (ValueError, OSError, sqlite3.Error):
        # Do not write private payloads/provider exceptions into background logs.
        message = (
            "Cloud sync did not finish: a local sync file or the archive could not be "
            "read. Local preferences remain available. Run varyz doctor."
        )
        _record(config, False, message)
        return message
