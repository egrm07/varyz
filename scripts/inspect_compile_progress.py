"""Read-only checkpoint view for a personal-model benchmark database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    generations = conn.execute(
        """SELECT id,state,record_count,reused_record_count,quarantine_count,created_at
           FROM personal_model_generations ORDER BY created_at DESC,id DESC LIMIT 3"""
    ).fetchall()
    for generation in generations:
        print(dict(generation))
        for checkpoint in conn.execute(
            """SELECT stage,state,detail_json FROM personal_model_compiler_checkpoints
               WHERE generation_id=? ORDER BY rowid""",
            (generation["id"],),
        ):
            print("  ", dict(checkpoint))


if __name__ == "__main__":
    main()
