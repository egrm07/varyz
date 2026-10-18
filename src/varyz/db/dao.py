"""Thin data-access layer over raw SQL. The schema is the product — keep it visible."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence

from sqlite_vec import serialize_float32

from varyz.domain import (
    ConversationMetadata,
    Cursor,
    Entry,
    NormalizedEntry,
    Sensitivity,
    iso,
    new_id,
    utc_now,
)


def content_hash(title: str | None, body: str) -> str:
    material = (title or "") + "\x00" + body
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def row_to_entry(row: sqlite3.Row) -> Entry:
    # sqlite3.Row's `in` operator matches values, not column names — use keys().
    data = dict(zip(row.keys(), tuple(row), strict=True))
    raw_conversation = data.pop("conversation_metadata_json", None)
    try:
        data["conversation"] = ConversationMetadata.model_validate_json(
            raw_conversation or "{}"
        )
    except (ValueError, TypeError):
        # A corrupt optional metadata envelope must not make the underlying
        # evidence unreadable.  The vault still holds the source payload and a
        # re-sync can heal this additive projection.
        data["conversation"] = ConversationMetadata()
    return Entry(**{k: v for k, v in data.items() if k in Entry.model_fields})


def _conversation_json(conversation: ConversationMetadata) -> str:
    """Canonical compact JSON for equality checks and SQLite storage."""
    return conversation.model_dump_json(exclude_none=True, exclude_defaults=True)


# --- entries -----------------------------------------------------------------


def upsert_entry(
    conn: sqlite3.Connection,
    norm: NormalizedEntry,
    *,
    source: str,
    source_account: str = "",
    vault_ref: str | None = None,
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED,
    significance: float | None = None,
) -> tuple[str, str]:
    """Insert or update by (source, source_account, external_id).

    Returns (entry_id, status) where status is 'new' | 'updated' | 'unchanged'.
    Unchanged is decided by content_hash, so re-ingest is a no-op and an
    upstream edit (e.g. calendar event) becomes an update that re-enriches.
    """
    chash = content_hash(norm.title, norm.body)
    existing = conn.execute(
        "SELECT id, content_hash, sensitivity, authored_by_user,"
        " conversation_metadata_json FROM entries"
        " WHERE source=? AND source_account=? AND external_id=?",
        (source, source_account, norm.external_id),
    ).fetchone()

    if existing is not None:
        if existing["content_hash"] == chash:
            # Authorship is not part of the content hash, so an archive that
            # predates the column would keep answering "0 authored entries"
            # through every re-sync — the coverage gate would then refuse a
            # Mirror the evidence actually supports. Correcting it in place
            # costs one UPDATE and no re-embedding: the text is identical.
            conversation_json = _conversation_json(norm.conversation)
            authorship_changed = (
                bool(existing["authored_by_user"]) != norm.authored_by_user
            )
            conversation_changed = (
                (existing["conversation_metadata_json"] or "{}")
                != conversation_json
            )
            # A rule that did not exist at ingest never reached the entries
            # already in the archive: an unchanged entry kept whatever label it
            # was first given, so a source default or a pattern added later
            # protected new material only. Escalation is the one direction
            # applied here -- it can lock text away but never release it, and
            # 'sensitive' stays sticky as it does on an edit below.
            escalated = (
                sensitivity == Sensitivity.SENSITIVE
                and existing["sensitivity"] != Sensitivity.SENSITIVE
            )
            if authorship_changed or conversation_changed or escalated:
                with conn:
                    conn.execute(
                        """UPDATE entries
                           SET authored_by_user=?, conversation_metadata_json=?,
                               sensitivity=?
                           WHERE id=?""",
                        (
                            int(norm.authored_by_user),
                            conversation_json,
                            Sensitivity.SENSITIVE if escalated else existing["sensitivity"],
                            existing["id"],
                        ),
                    )
            return existing["id"], "unchanged"
        # Re-apply classification on edit, but 'sensitive' is sticky — an entry
        # once escalated never silently becomes cloud-eligible again.
        if existing["sensitivity"] == Sensitivity.SENSITIVE:
            sensitivity = Sensitivity.SENSITIVE
        conn.execute(
            """UPDATE entries SET kind=?, content_hash=?, title=?, body=?,
               occurred_start=?, occurred_end=?, tz=?, time_precision=?,
               captured_at=?, vault_ref=COALESCE(?, vault_ref), thread_id=?,
               conversation_metadata_json=?, authored_by_user=?, sensitivity=?,
               significance=COALESCE(?, significance),
               enrich_version=0
               WHERE id=?""",
            (
                norm.kind,
                chash,
                norm.title,
                norm.body,
                iso(norm.occurred_start),
                iso(norm.occurred_end),
                norm.tz,
                norm.time_precision,
                iso(norm.captured_at),
                vault_ref,
                norm.thread_id,
                _conversation_json(norm.conversation),
                int(norm.authored_by_user),
                sensitivity,
                significance,
                existing["id"],
            ),
        )
        return existing["id"], "updated"

    entry_id = new_id()
    conn.execute(
        """INSERT INTO entries (id, kind, source, source_account, external_id,
           content_hash, title, body, occurred_start, occurred_end, tz,
           time_precision, captured_at, ingested_at, vault_ref, thread_id,
           conversation_metadata_json, authored_by_user, sensitivity, significance)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entry_id,
            norm.kind,
            source,
            source_account,
            norm.external_id,
            chash,
            norm.title,
            norm.body,
            iso(norm.occurred_start),
            iso(norm.occurred_end),
            norm.tz,
            norm.time_precision,
            iso(norm.captured_at),
            iso(utc_now()),
            vault_ref,
            norm.thread_id,
            _conversation_json(norm.conversation),
            int(norm.authored_by_user),
            sensitivity,
            significance,
        ),
    )
    return entry_id, "new"


def get_entry(conn: sqlite3.Connection, entry_id: str) -> Entry | None:
    row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    return row_to_entry(row) if row else None


def exclude_entry(
    conn: sqlite3.Connection,
    entry: Entry,
    *,
    reason: str = "user_forget",
) -> None:
    """Persist a local tombstone for one connector-owned natural key."""
    conn.execute(
        """INSERT INTO entry_exclusions
           (source, source_account, external_id, content_hash, reason, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(source, source_account, external_id) DO UPDATE SET
             content_hash=excluded.content_hash,
             reason=excluded.reason,
             created_at=excluded.created_at""",
        (
            entry.source,
            entry.source_account,
            entry.external_id,
            entry.content_hash,
            reason,
            iso(utc_now()),
        ),
    )


def is_entry_excluded(
    conn: sqlite3.Connection,
    source: str,
    source_account: str,
    external_id: str,
) -> bool:
    return conn.execute(
        """SELECT 1 FROM entry_exclusions
           WHERE source=? AND source_account=? AND external_id=?""",
        (source, source_account, external_id),
    ).fetchone() is not None


def list_entry_exclusions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT source, source_account, external_id, reason, created_at
           FROM entry_exclusions ORDER BY created_at DESC"""
    ).fetchall()


def remove_entry_exclusion(
    conn: sqlite3.Connection,
    source: str,
    source_account: str,
    external_id: str,
) -> bool:
    cursor = conn.execute(
        """DELETE FROM entry_exclusions
           WHERE source=? AND source_account=? AND external_id=?""",
        (source, source_account, external_id),
    )
    return cursor.rowcount > 0


def set_sensitivity(conn: sqlite3.Connection, entry_id: str, level: Sensitivity) -> None:
    conn.execute("UPDATE entries SET sensitivity=? WHERE id=?", (level, entry_id))


def count_entries(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> None:
    """Hard-delete an entry and everything derived from it. Chunks, facets,
    entity links and the FTS row go via ON DELETE CASCADE / triggers; the
    vec0 virtual table isn't an FK target, so clear its rows explicitly first."""
    chunk_ids = [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE entry_id=?", (entry_id,)
    )]
    if chunk_ids:
        qs = ",".join("?" * len(chunk_ids))
        conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({qs})", chunk_ids)
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))


def vault_ref_count(conn: sqlite3.Connection, vault_ref: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM entries WHERE vault_ref=?", (vault_ref,)
    ).fetchone()[0]


def external_ids_for_source(
    conn: sqlite3.Connection, source: str, account: str = ""
) -> list[tuple[str, str]]:
    """(entry id, external id) for everything one source account has ingested.

    Used to work out what a source used to hold and no longer does. Scoped by
    account as well as connector, because two mailboxes indexed by the same
    connector know nothing about each other's messages.
    """
    return [
        (row["id"], row["external_id"])
        for row in conn.execute(
            "SELECT id, external_id FROM entries WHERE source=? AND source_account=?",
            (source, account),
        )
    ]


# --- chunks & vectors ---------------------------------------------------------

_VECTOR_WIDTH = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


def vector_width(conn: sqlite3.Connection) -> int | None:
    """How many dimensions the vector index is declared to hold, or None.

    vec0 fixes its width at CREATE time and reports a mismatch only when a row
    is inserted, so the width has to be read back rather than assumed.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    ).fetchone()
    if row is None or not row["sql"]:
        return None
    match = _VECTOR_WIDTH.search(row["sql"])
    return int(match.group(1)) if match else None


def resize_vector_index(conn: sqlite3.Connection, width: int) -> None:
    """Rebuild the vector index at a new width and mark every chunk unembedded.

    Nothing is lost that was not already worthless: a change of width means a
    change of model, and vectors from one model cannot be compared against
    another's. Clearing `embed_model` is what makes the rebuild self-healing —
    otherwise chunks would go on claiming to be embedded while the index that
    held them no longer exists.
    """
    if width < 1:
        raise ValueError(f"vector width must be positive, got {width}")
    with conn:
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{int(width)}])"
        )
        conn.execute("UPDATE chunks SET embed_model=NULL")


def replace_chunks(
    conn: sqlite3.Connection,
    entry_id: str,
    texts: Sequence[str],
    recipe: str | None = None,
) -> list[int]:
    """Replace an entry's chunks (and their vectors). Returns new chunk ids.

    `recipe` records how the text was cut, so that changing the cut is
    detectable later; see entries_needing_chunks.
    """
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE entry_id=?", (entry_id,))]
    if old:
        qs = ",".join("?" * len(old))
        conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({qs})", old)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({qs})", old)
    ids = []
    for seq, text in enumerate(texts):
        cur = conn.execute(
            "INSERT INTO chunks (entry_id, seq, text, chunk_recipe) VALUES (?,?,?,?)",
            (entry_id, seq, text, recipe),
        )
        ids.append(cur.lastrowid)
    return ids


def store_embeddings(
    conn: sqlite3.Connection,
    chunk_ids: Sequence[int],
    vectors: Sequence[Sequence[float]],
    model: str,
) -> None:
    for chunk_id, vec in zip(chunk_ids, vectors, strict=True):
        conn.execute(
            "INSERT OR REPLACE INTO vec_chunks (rowid, embedding) VALUES (?,?)",
            (chunk_id, serialize_float32(list(vec))),
        )
        conn.execute("UPDATE chunks SET embed_model=? WHERE id=?", (model, chunk_id))


def entries_needing_chunks(conn: sqlite3.Connection, recipe: str | None = None) -> list[Entry]:
    """Entries with no chunks, plus — when a recipe is given — entries whose
    chunks were cut by different rules.

    Without the second clause an entry is chunked once and never again, so a
    changed `chunk_tokens` or a fixed splitter applies only to whatever is
    ingested afterwards, leaving one archive holding two incompatible
    chunkings and no sign of it.
    """
    if recipe is None:
        rows = conn.execute(
            """SELECT e.* FROM entries e
               WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.entry_id = e.id)
                 AND length(trim(coalesce(e.title,'') || e.body)) > 0"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.* FROM entries e
               WHERE NOT EXISTS (
                       SELECT 1 FROM chunks c
                        WHERE c.entry_id = e.id
                          AND c.chunk_recipe IS ?)
                 AND length(trim(coalesce(e.title,'') || e.body)) > 0""",
            (recipe,),
        ).fetchall()
    return [row_to_entry(r) for r in rows]


def chunks_needing_embedding(conn: sqlite3.Connection, model: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, text FROM chunks WHERE embed_model IS NULL OR embed_model != ?",
        (model,),
    ).fetchall()


# --- sync state ----------------------------------------------------------------


def get_cursor(conn: sqlite3.Connection, connector_id: str, account: str = "") -> Cursor | None:
    row = conn.execute(
        "SELECT cursor_json FROM sync_state WHERE connector_id=? AND account=?",
        (connector_id, account),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return Cursor(data=json.loads(row[0]))


def set_cursor(
    conn: sqlite3.Connection,
    connector_id: str,
    cursor: Cursor | None,
    account: str = "",
    status: str = "ok",
) -> None:
    conn.execute(
        """INSERT INTO sync_state (connector_id, account, cursor_json, last_run_at, last_status)
           VALUES (?,?,?,?,?)
           ON CONFLICT(connector_id, account) DO UPDATE SET
             cursor_json=excluded.cursor_json,
             last_run_at=excluded.last_run_at,
             last_status=excluded.last_status""",
        (
            connector_id,
            account,
            json.dumps(cursor.data) if cursor else None,
            iso(utc_now()),
            status,
        ),
    )


# --- enrichment jobs -------------------------------------------------------------


def enqueue_job(conn: sqlite3.Connection, entry_id: str, task: str) -> None:
    conn.execute(
        """INSERT INTO enrichment_jobs (id, entry_id, task, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(entry_id, task) DO UPDATE SET
             status='pending', attempts=0, error=NULL""",
        (new_id(), entry_id, task, iso(utc_now())),
    )


# --- todos -----------------------------------------------------------------------


def create_todo(
    conn: sqlite3.Connection,
    text: str,
    *,
    entry_id: str | None = None,
    due: str | None = None,
    source_quote: str | None = None,
) -> str:
    todo_id = new_id()
    conn.execute(
        "INSERT INTO todos (id, entry_id, text, due, created_at, source_quote)"
        " VALUES (?,?,?,?,?,?)",
        (todo_id, entry_id, text, due, iso(utc_now()), source_quote),
    )
    return todo_id


def list_todos(conn: sqlite3.Connection, status: str = "open") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM todos WHERE status=? ORDER BY COALESCE(due, '9999'), created_at",
        (status,),
    ).fetchall()


def complete_todo(conn: sqlite3.Connection, todo_id: str) -> bool:
    cur = conn.execute(
        "UPDATE todos SET status='done', completed_at=? WHERE id=? AND status='open'",
        (iso(utc_now()), todo_id),
    )
    return cur.rowcount > 0
