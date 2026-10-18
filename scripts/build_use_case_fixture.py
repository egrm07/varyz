"""Build the public, synthetic multi-source archive used by the use-case eval."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build(corpus: Path, destination: Path) -> tuple[Path, Path, int]:
    destination.mkdir(parents=True, exist_ok=True)
    database = destination / "varyz.db"
    database.unlink(missing_ok=True)
    config = destination / "config.toml"
    config.write_text(
        '[storage]\ndata_dir = "."\n\n[retrieval]\ndistill = "off"\n',
        encoding="utf-8",
    )
    conn = open_db(database)
    rows = load_jsonl(corpus)
    with conn:
        for row in rows:
            norm = NormalizedEntry(
                kind=Kind(row["kind"]),
                external_id=row["external_id"],
                title=row.get("title"),
                body=row["body"],
                occurred_start=instant(row["occurred"]),
                captured_at=instant(row["occurred"]),
                authored_by_user=row["source"] in {"git", "manual", "journal_import"},
            )
            entry_id, _ = dao.upsert_entry(
                conn,
                norm,
                source=row["source"],
                sensitivity=Sensitivity(row.get("sensitivity", "public")),
            )
            dao.replace_chunks(conn, entry_id, [f'{row.get("title", "")}\n\n{row["body"]}'])
    conn.close()
    return database, config, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("evals/varys_use_cases_corpus_v1.jsonl"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("data/evals/varys_use_cases_v1"),
    )
    args = parser.parse_args()
    database, config, count = build(args.corpus, args.destination)
    print(f"entries={count}")
    print(f"database={database.resolve()}")
    print(f"config={config.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
