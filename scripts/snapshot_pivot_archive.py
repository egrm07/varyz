"""Create the compact pivot archive without mutating the live Varyz database.

The source database is larger than the free space available for a literal
``VACUUM INTO``.  This script builds the same useful result from a fresh schema:
ground truth, retrieval state, policy/audit state and paid caches are copied;
Mirror and personal-model generation rows are deliberately left behind.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from varyz.db.connection import open_db

KEEP_PERSONAL_MODEL_TABLES = frozenset(
    {
        "personal_model_candidate_window_cache",
        "personal_model_observation_annotation_cache",
        "personal_model_overrides",
    }
)
SKIP_TABLES = frozenset({"schema_migrations", "sqlite_sequence"})


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _copy_allowed(name: str) -> bool:
    if name in SKIP_TABLES or name.startswith("mirror_"):
        return False
    return not (
        name.startswith("personal_model_") and name not in KEEP_PERSONAL_MODEL_TABLES
    )


def _visible_columns(conn: sqlite3.Connection, database: str, table: str) -> tuple[str, ...]:
    rows = conn.execute(
        f"PRAGMA {_quoted(database)}.table_xinfo({_quoted(table)})"
    ).fetchall()
    return tuple(str(row[1]) for row in rows if int(row[6]) == 0)


def snapshot(source: Path, destination: Path) -> dict[str, int]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    paused = source.parent / "autosync" / "PAUSED"
    if not paused.is_file():
        raise RuntimeError(f"source autosync is not paused: missing {paused}")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(destination)
    copied: dict[str, int] = {}
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA foreign_keys=OFF")
        # The source is guarded by the PAUSED sentinel above and only appears
        # on the right-hand side of SELECT statements in this script.
        conn.execute("ATTACH DATABASE ? AS source", (str(source),))

        source_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM source.sqlite_master WHERE type='table'"
            )
        }
        destination_tables = conn.execute(
            "SELECT name, sql FROM main.sqlite_master "
            "WHERE type='table' AND sql IS NOT NULL ORDER BY rowid"
        ).fetchall()
        virtual_tables = {
            str(name)
            for name, sql in destination_tables
            if str(sql).lstrip().upper().startswith("CREATE VIRTUAL TABLE")
        }

        for raw_name, raw_sql in destination_tables:
            name = str(raw_name)
            sql = str(raw_sql)
            if not _copy_allowed(name) or name not in source_tables:
                continue
            # FTS5 and sqlite-vec expose implementation tables through
            # sqlite_master. Their owning virtual table populates them; copying
            # both paths either duplicates rows or corrupts the index.
            if any(name.startswith(f"{virtual_name}_") for virtual_name in virtual_tables):
                continue
            # External-content FTS indexes are rebuilt by the triggers on their
            # source tables. Copying them as well would duplicate every row.
            if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE") and "content=" in sql:
                continue

            destination_columns = _visible_columns(conn, "main", name)
            source_columns = set(_visible_columns(conn, "source", name))
            columns = tuple(column for column in destination_columns if column in source_columns)
            if not columns:
                continue
            names = ", ".join(_quoted(column) for column in columns)
            table = _quoted(name)
            conn.execute(f"DELETE FROM main.{table}")
            conn.execute(
                f"INSERT INTO main.{table} ({names}) "
                f"SELECT {names} FROM source.{table}"
            )
            count = int(conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
            copied[name] = count
            print(f"{name}: {count:,}", flush=True)

        conn.commit()
        conn.execute("DETACH DATABASE source")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"destination integrity check failed: {integrity}")
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"destination has {len(foreign_key_errors)} foreign-key violations"
            )
        conn.execute("VACUUM")
    except BaseException:
        conn.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    copied = snapshot(args.source, args.destination)
    size = args.destination.stat().st_size
    print(f"snapshot complete: {len(copied)} tables, {size:,} bytes")


if __name__ == "__main__":
    main()
