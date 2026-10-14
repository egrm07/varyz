from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.snapshot_pivot_archive import snapshot

from varyz.db.connection import open_db


def test_snapshot_keeps_ground_truth_and_caches_but_omits_derived_rows(tmp_path: Path) -> None:
    source = tmp_path / "source" / "varyz.db"
    source.parent.mkdir(parents=True)
    (source.parent / "autosync").mkdir()
    (source.parent / "autosync" / "PAUSED").write_text("paused\n", encoding="utf-8")
    conn = open_db(source)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO entries "
        "(id,kind,source,source_account,external_id,content_hash,body,ingested_at) "
        "VALUES ('entry-1','note','manual','','one','hash','kept','2026-09-19T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO graph_extraction_cache "
        "(cache_key,entry_id,scope,content_hash,window_fingerprint,batch_json,"
        "created_at,last_used_at) VALUES "
        "('cache-1','entry-1','entry','hash','window','{}',"
        "'2026-09-19T00:00:00Z','2026-09-19T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO mirror_runs "
        "(id,graph_generation_id,generated_at,state,gate_passed,gate_reason) "
        "VALUES ('mirror-1','graph-1','2026-09-19T00:00:00Z','current',1,'ready')"
    )
    conn.commit()
    conn.close()

    destination = tmp_path / "destination" / "varyz.db"
    copied = snapshot(source, destination)
    result = sqlite3.connect(destination)
    try:
        assert result.execute("SELECT body FROM entries").fetchone()[0] == "kept"
        assert result.execute("SELECT COUNT(*) FROM graph_extraction_cache").fetchone()[0] == 1
        assert result.execute("SELECT COUNT(*) FROM mirror_runs").fetchone()[0] == 0
        assert "entries" in copied
        assert "mirror_runs" not in copied
    finally:
        result.close()
