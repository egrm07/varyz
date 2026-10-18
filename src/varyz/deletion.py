"""Forget an entry: remove it from the database and drop its original artifact
from the vault when nothing else references it (content-addressed dedup means
a blob can back more than one entry)."""

from __future__ import annotations

import sqlite3

from varyz.db import dao
from varyz.events import emit_event
from varyz.vault import Vault


def forget_entry(
    conn: sqlite3.Connection,
    vault: Vault,
    entry_id: str,
    *,
    prevent_reimport: bool = False,
) -> bool:
    """Delete one local entry, optionally preventing connector re-import."""
    entry = dao.get_entry(conn, entry_id)
    if entry is None:
        return False
    vault_ref = entry.vault_ref
    with conn:
        if prevent_reimport:
            dao.exclude_entry(conn, entry)
        emit_event(
            conn,
            "entry_deleted",
            "entry",
            entry_id,
            {
                "content_hash": entry.content_hash,
                "prevent_reimport": prevent_reimport,
            },
        )
        dao.delete_entry(conn, entry_id)
    if vault_ref and dao.vault_ref_count(conn, vault_ref) == 0:
        vault.remove(vault_ref)
    return True
