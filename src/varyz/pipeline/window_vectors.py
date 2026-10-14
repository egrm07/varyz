"""Embed conversations, so one can be found by what it meant.

The words of a conversation are indexed in `conversation_windows_fts` and that
gets the right conversation to the top of the list 45.8% of the time on this
archive. The half it misses are the ones that never repeat the question's
words: the question says "plumber" and the exchange says "he", the question
says rent and the exchange says "the money for the flat". That is what a vector
is for, and it is the last retrieval unit in varyz that did not have one.

The chunking, the width alignment and the model are the entry pipeline's,
deliberately: one embedding model, one set of assumptions, and a window vector
that can be compared against a question vector the same way a passage can. What
is separate is the storage, because a window chunk surfacing in a passage
search would be a private message appearing where a citable excerpt belongs.

Nothing here decides who may read a conversation. The label does, at query
time; an unread window has no label and stays shut.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from varyz.pipeline.embed import CHARS_PER_TOKEN, chunk_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from varyz.config import Config
    from varyz.pipeline.embed import Embedder

VECTOR_WIDTH_SQL = "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_window_chunks'"

# The most neighbours sqlite-vec will return for one query. Its own limit, not
# a preference: asking for more raises rather than truncating.
KNN_MAX = 4096


def _serialize(vector: list[float]) -> bytes:
    from sqlite_vec import serialize_float32

    return serialize_float32(vector)


def _width(conn: sqlite3.Connection) -> int | None:
    import re

    row = conn.execute(VECTOR_WIDTH_SQL).fetchone()
    if row is None or not row["sql"]:
        return None
    match = re.search(r"float\s*\[\s*(\d+)\s*\]", row["sql"], re.IGNORECASE)
    return int(match.group(1)) if match else None


def _resize(conn: sqlite3.Connection, width: int) -> None:
    """Rebuild the index at a new width and mark every window chunk unembedded.

    A change of width is a change of model, and vectors from two models cannot
    be compared. Clearing `embed_model` is what makes this self-healing rather
    than a table of orphans claiming to be embedded.
    """
    with conn:
        conn.execute("DROP TABLE IF EXISTS vec_window_chunks")
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_window_chunks USING vec0(embedding float[{int(width)}])"
        )
        conn.execute("UPDATE conversation_window_chunks SET embed_model=NULL")


def _stale(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Windows with no chunks, or chunks cut from text that has since changed."""
    return conn.execute(
        "SELECT w.id, w.content_hash FROM conversation_windows w"
        " LEFT JOIN conversation_window_chunks c"
        "   ON c.window_id = w.id AND c.content_hash = w.content_hash"
        " WHERE c.window_id IS NULL"
        " GROUP BY w.id"
    ).fetchall()


def _replace(
    conn: sqlite3.Connection, window_id: str, content_hash: str, texts: list[str]
) -> None:
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM conversation_window_chunks WHERE window_id = ?", (window_id,)
        )
    ]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vec_window_chunks WHERE rowid IN ({marks})", ids)
        conn.execute("DELETE FROM conversation_window_chunks WHERE window_id = ?", (window_id,))
    conn.executemany(
        "INSERT INTO conversation_window_chunks (window_id, seq, text, content_hash)"
        " VALUES (?, ?, ?, ?)",
        [(window_id, seq, text, content_hash) for seq, text in enumerate(texts)],
    )


def embed_windows(
    conn: sqlite3.Connection,
    cfg: Config,
    embedder: Embedder,
    *,
    batch_size: int = 64,
    limit: int | None = None,
) -> int:
    """Cut and embed every conversation whose text has no current vector.

    Returns the number of chunks embedded. Resumable in the same way the entry
    pipeline is: the work is recorded per chunk, so an interrupted run costs
    the batch in flight and nothing else.
    """
    from varyz.pipeline.windows import window_text

    max_chars = cfg.embedding.chunk_tokens * CHARS_PER_TOKEN
    stale = _stale(conn)
    if limit is not None:
        stale = stale[:limit]
    for window in stale:
        text = window_text(conn, window["id"])
        with conn:
            _replace(conn, window["id"], window["content_hash"], chunk_text(text, max_chars))

    pending = conn.execute(
        "SELECT id, text FROM conversation_window_chunks"
        " WHERE embed_model IS NULL OR embed_model != ?",
        (embedder.model_name,),
    ).fetchall()
    if not pending:
        return 0

    width = len(embedder.embed_query("dimension probe"))
    if _width(conn) != width:
        _resize(conn, width)
        pending = conn.execute(
            "SELECT id, text FROM conversation_window_chunks"
        ).fetchall()

    embedded = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embedder.embed_documents([row["text"] for row in batch])
        with conn:
            for row, vector in zip(batch, vectors, strict=True):
                conn.execute("DELETE FROM vec_window_chunks WHERE rowid = ?", (row["id"],))
                conn.execute(
                    "INSERT INTO vec_window_chunks (rowid, embedding) VALUES (?, ?)",
                    (row["id"], _serialize(vector)),
                )
                conn.execute(
                    "UPDATE conversation_window_chunks SET embed_model = ? WHERE id = ?",
                    (embedder.model_name, row["id"]),
                )
        embedded += len(batch)
    return embedded


def nearest(
    conn: sqlite3.Connection, query_vector: list[float], limit: int
) -> list[tuple[str, float]]:
    """(window id, distance) for the conversations closest to a question.

    One row per conversation, at its best chunk -- a long conversation should
    not beat a short one by having more chances to be close.

    Asking for more neighbours than the index will serve is an error, not a
    short answer, and the handler below would turn it into an empty one: the
    vector arm disappearing without a word on exactly the deep queries it was
    added for. So the request is clamped instead. The words arm is unbounded
    and still ranks past this point; the vectors simply stop extending it.
    """
    try:
        rows = conn.execute(
            "SELECT c.window_id, MIN(v.distance) AS dist"
            " FROM (SELECT rowid, distance FROM vec_window_chunks"
            "       WHERE embedding MATCH ? AND k = ? ORDER BY distance) v"
            " JOIN conversation_window_chunks c ON c.id = v.rowid"
            " GROUP BY c.window_id"
            " ORDER BY dist",
            (_serialize(query_vector), min(limit * 3, KNN_MAX)),
        ).fetchall()
    except sqlite3.OperationalError:
        # An archive older than the migration, or one that has never been
        # embedded. The words index still answers.
        return []
    return [(row["window_id"], float(row["dist"])) for row in rows[:limit]]


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(chunks, embedded) -- how much of the conversation index is usable."""
    try:
        row = conn.execute(
            "SELECT count(*) AS n, sum(embed_model IS NOT NULL) AS done"
            " FROM conversation_window_chunks"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    return int(row["n"] or 0), int(row["done"] or 0)
