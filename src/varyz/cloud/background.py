"""Opt-in sync in the existing refresh cycle, without starting another daemon."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import keyring

from varyz.cloud.archive_sync import ArchiveSync, read_archive_journal
from varyz.cloud.client import CloudClient, credential_name, load_tokens
from varyz.cloud.local_sync import LocalSync, read_journal
from varyz.cloud.session_report_sync import ReportSync
from varyz.cloud.session_report_sync import journal_path as report_path
from varyz.cloud.session_report_sync import read_journal as read_report_journal
from varyz.db.connection import LockHeldError


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
            if report_journal["enabled"]:
                report_result = ReportSync(conn, config).sync(client, journal["account_id"])
                result["uploaded"] += report_result["uploaded"] + report_result["deleted"]
        finally:
            client.close()
        if not result["uploaded"] and not result["downloaded"]:
            return None
        return f"Cloud synced: {result['uploaded']} sent, {result['downloaded']} received"
    except LockHeldError:
        return None  # another authorized sync owns the journal
    except (ValueError, OSError, sqlite3.Error, keyring.errors.KeyringError):
        # Do not write private payloads/provider exceptions into background logs.
        return "Cloud sync needs attention; local preferences remain available. Check cloud status."
