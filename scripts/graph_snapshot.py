"""Count what the active graph holds, so two generations can be compared.

Counts only -- predicates, origins, scopes, evidence rows. Nothing here reads
a subject, an object or a quote, so the output is safe to keep in a report.

    uv run --no-sync python scripts/graph_snapshot.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--run", help="Generation to count. Default: the active one.")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    cfg = load_config(None)
    db_path = args.db or (cfg.storage.data_dir / "varyz.db")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    run = args.run
    if run is None:
        row = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()
        if row is None:
            print("no active graph generation")
            return 2
        run = row[0]

    snapshot: dict[str, object] = {"run": run}
    snapshot["claims_total"] = conn.execute(
        "SELECT count(*) FROM claims WHERE graph_generation_id=?", (run,)
    ).fetchone()[0]
    for column in ("predicate", "origin", "scope"):
        snapshot[f"by_{column}"] = dict(
            conn.execute(
                f"SELECT coalesce({column},'(null)'), count(*) FROM claims "
                "WHERE graph_generation_id=? GROUP BY 1 ORDER BY 2 DESC",
                (run,),
            ).fetchall()
        )
    snapshot["evidence_rows"] = conn.execute(
        "SELECT count(*) FROM claim_evidence e JOIN claims c ON c.id=e.claim_id "
        "WHERE c.graph_generation_id=?",
        (run,),
    ).fetchone()[0]
    snapshot["entities"] = dict(
        conn.execute(
            "SELECT entity_type, count(*) FROM entities WHERE graph_generation_id=? "
            "GROUP BY 1 ORDER BY 2 DESC",
            (run,),
        ).fetchall()
    )
    print(json.dumps(snapshot, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
