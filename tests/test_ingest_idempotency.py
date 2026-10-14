"""The core guarantee: re-running any connector creates zero new rows."""

from varyz.config import Config
from varyz.connectors.base import get_connector
from varyz.db import dao
from varyz.pipeline.ingest import sync_connector


def _sync(conn, vault, cfg, connector_id):
    return sync_connector(conn, vault, get_connector(connector_id), cfg)


def _configure(cfg: Config, connector_id: str, path) -> Config:
    cfg.connectors[connector_id] = {"approved": True, "paths": [str(path)]}
    return cfg


def test_document_sync_twice_is_noop(conn, vault, cfg, journal_dir):
    _configure(cfg, "documents", journal_dir)
    first = _sync(conn, vault, cfg, "documents")
    assert first.new == 3 and first.errors == 0

    second = _sync(conn, vault, cfg, "documents")
    assert second.new == 0 and second.updated == 0
    assert second.unchanged == 3
    assert dao.count_entries(conn) == 3


def test_edited_file_updates_not_duplicates(conn, vault, cfg, journal_dir):
    _configure(cfg, "documents", journal_dir)
    _sync(conn, vault, cfg, "documents")

    md = journal_dir / "2020-05-17.md"
    md.write_text(
        md.read_text(encoding="utf-8") + "\n\nLater: Anna lost her keys.", encoding="utf-8"
    )
    result = _sync(conn, vault, cfg, "documents")
    assert result.updated == 1
    assert result.new == 0
    assert dao.count_entries(conn) == 3
    row = conn.execute(
        "SELECT body FROM entries WHERE external_id=?", (str(md.resolve()),)
    ).fetchone()
    assert "lost her keys" in row["body"]


def test_update_reenqueues_embedding(conn, vault, cfg, journal_dir):
    _configure(cfg, "documents", journal_dir)
    _sync(conn, vault, cfg, "documents")
    conn.execute("UPDATE enrichment_jobs SET status='done'")

    md = journal_dir / "2020-05-17.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nmore", encoding="utf-8")
    _sync(conn, vault, cfg, "documents")

    pending = conn.execute(
        "SELECT COUNT(*) FROM enrichment_jobs WHERE status='pending' AND task='embed'"
    ).fetchone()[0]
    assert pending == 1


def test_fts_stays_consistent_after_update(conn, vault, cfg, journal_dir):
    _configure(cfg, "documents", journal_dir)
    _sync(conn, vault, cfg, "documents")

    md = journal_dir / "2020-05-17.md"
    md.write_text(
        "---\ntitle: Lake day\ndate: 2020-05-17\n---\n\nRewritten: kayaking instead.",
        encoding="utf-8",
    )
    _sync(conn, vault, cfg, "documents")

    assert conn.execute(
        "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH 'kayaking'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH 'freezing'"
    ).fetchone()[0] == 0


def test_progress_is_reported_once_per_hundred_items_read(conn, vault, cfg, tmp_path):
    """A skipped file leaves the count where it was.

    Progress was announced whenever the count was a multiple of a hundred, so
    an unreadable file met before anything was counted printed "...0 items",
    and every one met after the hundredth printed "...100 items" again.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "000-broken.pdf").write_bytes(b"not a pdf at all")
    for n in range(100):
        (docs / f"{n + 1:03d}.md").write_text(f"note number {n}", encoding="utf-8")
    for n in range(3):
        (docs / f"zzz-broken-{n}.pdf").write_bytes(b"not a pdf either")
    _configure(cfg, "documents", docs)

    reported: list[int] = []
    stats = sync_connector(
        conn,
        vault,
        get_connector("documents"),
        cfg,
        on_progress=lambda s: reported.append(s.total),
    )

    assert stats.new == 100
    assert reported == [100]
