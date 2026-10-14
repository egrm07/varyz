"""Document diversity and evidence integrity under repeated chunk neighbors."""

import pytest

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.retrieval.search import vector_search


def seed(conn, chunk_counts):
    entries = []
    with conn:
        for doc, count in enumerate(chunk_counts):
            entry_id, _ = dao.upsert_entry(
                conn,
                NormalizedEntry(kind=Kind.NOTE, external_id=str(doc), body=f"Document {doc}"),
                source="manual",
                sensitivity=Sensitivity.PUBLIC,
            )
            entries.append(entry_id)
            ids = dao.replace_chunks(conn, entry_id, [f"{doc}:{i}" for i in range(count)])
            vectors = [[doc + i / (count + 1) * 0.25] + [0.0] * 383 for i in range(count)]
            dao.store_embeddings(conn, ids, vectors, "controlled-distance-v1")
    return entries


def test_long_document_cannot_starve_other_nearest_entries(conn):
    entries = seed(conn, [180, 2, 3, 1, 2, 1])
    found, chunks = vector_search(conn, [0.0] * 384, 5)
    assert found == entries[:5]
    assert chunks == {entry_id: f"{doc}:0" for doc, entry_id in enumerate(entries[:5])}


def test_small_index_stops_when_exhausted(conn):
    entries = seed(conn, [4, 1])
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        found, _ = vector_search(conn, [0.0] * 384, 10)
    finally:
        conn.set_trace_callback(None)
    assert found == entries
    assert sum("SELECT c.entry_id" in statement for statement in statements) == 1


def test_uncrowded_search_keeps_one_scan(conn):
    entries = seed(conn, [1] * 8)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        found, _ = vector_search(conn, [0.0] * 384, 5)
    finally:
        conn.set_trace_callback(None)
    assert found == entries[:5]
    assert sum("SELECT c.entry_id" in statement for statement in statements) == 1


def test_extreme_crowding_stops_at_bounded_budget(conn):
    entries = seed(conn, [500, 1])
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        found, chunks = vector_search(conn, [0.0] * 384, 2)
    finally:
        conn.set_trace_callback(None)
    # Six chunks initially, then 24, 96, 384. Return underfilled at the cap
    # rather than scan arbitrarily far for the next distinct entry.
    assert found == entries[:1]
    assert chunks[entries[0]] == "0:0"
    assert sum("SELECT c.entry_id" in statement for statement in statements) == 4


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit_does_not_query(conn, limit):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert vector_search(conn, [0.0] * 384, limit) == ([], {})
    finally:
        conn.set_trace_callback(None)
    assert statements == []
