"""Transactional internal event/outbox helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from varyz.domain import iso, new_id, utc_now


def emit_event(
    conn: sqlite3.Connection,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict | None = None,
) -> str:
    event_id = new_id()
    material = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """INSERT OR IGNORE INTO internal_events
           (id, event_type, aggregate_type, aggregate_id, payload_json, created_at)
           VALUES (?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            material,
            iso(utc_now()),
        ),
    )
    row = conn.execute(
        """SELECT id FROM internal_events
           WHERE event_type=? AND aggregate_type=? AND aggregate_id=? AND payload_json=?""",
        (event_type, aggregate_type, aggregate_id, material),
    ).fetchone()
    return row["id"]


def pending_events(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM internal_events WHERE consumed_at IS NULL
           ORDER BY created_at, id LIMIT ?""",
        (limit,),
    ).fetchall()


def consume_events(conn: sqlite3.Connection, event_ids: Iterable[str]) -> None:
    ids = list(event_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE internal_events SET consumed_at=? WHERE id IN ({placeholders})",
        (iso(utc_now()), *ids),
    )
