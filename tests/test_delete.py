"""Deleting an entry removes it from the DB, vectors, FTS, facets, and (when
unreferenced) the vault — and it disappears from search, timeline, and ask."""

from __future__ import annotations

from varyz.connectors.manual import ManualConnector
from varyz.db import dao
from varyz.deletion import forget_entry
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.pipeline.embed import embed_pending
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.retrieval.search import hybrid_search


def _ingest(conn, vault, cfg, text, todo=False):
    gate = SensitivityGate(cfg.sensitivity)
    entry_id, _ = ingest_item(conn, vault, ManualConnector(), gate,
                              ManualConnector.make_item(text))
    if todo:
        with conn:
            dao.create_todo(conn, text, entry_id=entry_id, source_quote=text)
    return entry_id


def test_forget_removes_all_derived_rows(conn, vault, cfg, fake_embedder):
    eid = _ingest(conn, vault, cfg, "signed the Berlin lease on 2025-03-02", todo=True)
    embed_pending(conn, cfg, fake_embedder)

    chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE entry_id=?", (eid,))]
    assert chunk_ids
    vault_ref = dao.get_entry(conn, eid).vault_ref
    assert vault.has(vault_ref)

    assert forget_entry(conn, vault, eid) is True

    assert dao.get_entry(conn, eid) is None
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE entry_id=?", (eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM todos WHERE entry_id=?", (eid,)).fetchone()[0] == 0
    # vectors gone (vec0 isn't an FK target — must be cleared explicitly)
    qs = ",".join("?" * len(chunk_ids))
    left = conn.execute(f"SELECT COUNT(*) FROM vec_chunks WHERE rowid IN ({qs})", chunk_ids)
    assert left.fetchone()[0] == 0
    # FTS row gone (via delete trigger)
    assert conn.execute(
        "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH 'Berlin'"
    ).fetchone()[0] == 0
    # vault blob removed since nothing else referenced it
    assert not vault.has(vault_ref)


def test_forget_missing_entry_returns_false(conn, vault):
    assert forget_entry(conn, vault, "nope") is False


def test_forget_keeps_shared_vault_blob(conn, vault, cfg):
    """Two entries backed by the same blob: deleting one keeps the file."""
    norm = NormalizedEntry(kind=Kind.NOTE, external_id="a", body="same bytes")
    ref = vault.put(b"same bytes", ".json")
    id1, _ = dao.upsert_entry(conn, norm, source="manual", vault_ref=ref,
                              sensitivity=Sensitivity.PERSONAL)
    norm2 = NormalizedEntry(kind=Kind.NOTE, external_id="b", body="same bytes")
    id2, _ = dao.upsert_entry(conn, norm2, source="other", vault_ref=ref,
                              sensitivity=Sensitivity.PERSONAL)

    forget_entry(conn, vault, id1)
    assert vault.has(ref)  # still referenced by id2
    forget_entry(conn, vault, id2)
    assert not vault.has(ref)  # now unreferenced -> removed


def test_deleted_entry_gone_from_search(conn, vault, cfg, fake_embedder):
    keep = _ingest(conn, vault, cfg, "kayaking on the lake with Anna")
    drop = _ingest(conn, vault, cfg, "boring meeting about spreadsheets")
    embed_pending(conn, cfg, fake_embedder)

    forget_entry(conn, vault, drop)
    ids = {h.entry.id for h in hybrid_search(conn, "spreadsheets meeting", fake_embedder, limit=10)}
    assert drop not in ids
    assert keep == next(iter(
        h.entry.id for h in hybrid_search(conn, "kayaking lake", fake_embedder, limit=1)
    ))


def test_durable_forget_blocks_the_same_connector_item_from_reimport(
    conn, vault, cfg
):
    connector = ManualConnector()
    item = ManualConnector.make_item("do not restore this private note")
    gate = SensitivityGate(cfg.sensitivity)
    entry_id, status = ingest_item(conn, vault, connector, gate, item)
    assert status == "new"

    assert forget_entry(conn, vault, entry_id, prevent_reimport=True)
    restored_id, restored_status = ingest_item(conn, vault, connector, gate, item)

    assert restored_id == ""
    assert restored_status == "excluded"
    assert dao.count_entries(conn) == 0
    assert dao.is_entry_excluded(conn, "manual", "", item.external_id)


def test_one_time_forget_still_allows_reimport(conn, vault, cfg):
    connector = ManualConnector()
    item = ManualConnector.make_item("a note that may return")
    gate = SensitivityGate(cfg.sensitivity)
    entry_id, _ = ingest_item(conn, vault, connector, gate, item)

    assert forget_entry(conn, vault, entry_id, prevent_reimport=False)
    restored_id, restored_status = ingest_item(conn, vault, connector, gate, item)

    assert restored_id
    assert restored_status == "new"
