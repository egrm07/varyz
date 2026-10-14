"""Reactivate a superseded graph generation, named explicitly.

A rebuild activates what it builds. When the measurements that follow say the
new generation is worse, this puts the old one back without rebuilding it. The
target is always named on the command line: there is no "previous", because a
wrong guess here is the archive answering from a graph nobody chose.

    uv run --no-sync python scripts/rollback_generation.py --list
    uv run --no-sync python scripts/rollback_generation.py <run-id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import load_config  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.graph.repository import GraphRepository  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", nargs="?", help="Generation to reactivate.")
    parser.add_argument("--list", action="store_true", help="Show the generations.")
    args = parser.parse_args(argv)

    cfg = load_config(None)
    conn = open_db(cfg.storage.data_dir / "varyz.db")
    if args.list or not args.run:
        rows = conn.execute(
            "SELECT id, state, extractor_version, completed_at, activated_at "
            "FROM graph_runs ORDER BY coalesce(completed_at, activated_at, '') DESC"
        ).fetchall()
        for row in rows:
            print(
                f"{row['id']}  {row['state']:<10} v{row['extractor_version']}  "
                f"completed {row['completed_at']}  activated {row['activated_at']}"
            )
        return 0 if args.list else 2

    GraphRepository(conn).rollback(args.run)
    print(f"active generation is now {args.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
