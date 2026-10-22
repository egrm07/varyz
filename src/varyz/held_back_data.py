"""Delete what an older version built for parts this build holds back.

The alpha ships neither the personal-model compiler, the graph builder nor
the conversation windows (docs/SHIPPING.md), and nothing it answers reads what
they wrote. An archive an
older version filled keeps all of it: on the owner's, 34 personal-model
generations (1.3 GB, none of them read) and 1.7 million sensitivity labels for
41 entities, most describing graph rows long since replaced. The owner chose
to have updated installs delete it (2026-10-22).

Everything here is derived and rebuildable from the entries. What the owner
wrote stays: `personal_model_overrides`, `graph_overrides`, the entities a
correction may name, and every `user_override` label. Running it again finds
nothing to do, so an interrupted run finishes on the next cycle.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from varyz.domain import iso, utc_now
from varyz.events import emit_event

# Rows per committed window. A personal-model record carries a dozen evidence
# and dependency rows; deleting a generation's records in one statement can
# fill the disk with write-ahead log, the space this is meant to recover.
_ROWID_SPAN = 1_000
_LABEL_ROWID_SPAN = 20_000

# Caches only the compiler reads, keyed by content rather than a generation.
_COMPILER_CACHES = (
    "personal_model_candidate_window_cache",
    "personal_model_observation_annotation_cache",
)

# Graph resources and the table each label's resource_id points into.
_GRAPH_LABELS = (
    ("entity", "entities"),
    ("alias", "entity_aliases"),
    ("claim", "claims"),
    ("evidence_quote", "claim_evidence"),
)

# Conversation windows: deleting a window cascades its entries, labels,
# conclusions, runs and chunks. The two indexes beside them hold no key.
_WINDOW_INDEXES = ("vec_window_chunks", "conversation_windows_fts")

# Nothing reads the internal event log; it only grew, by one row per entry.
EVENT_RETENTION_DAYS = 30

# VACUUM writes a full copy of what remains; leave room for it and the log.
_VACUUM_MIN_FREED = 100_000_000
_VACUUM_HEADROOM = 2.0


@dataclass(frozen=True)
class ClearResult:
    rows_by_table: dict[str, int] = field(default_factory=dict)
    stopped: bool = False
    vacuumed_bytes: int = 0
    reclaimable_bytes: int = 0

    @property
    def rows(self) -> int:
        return sum(self.rows_by_table.values())


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _windowed_delete(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    span: int,
    should_stop: Callable[[], bool],
) -> tuple[int, bool]:
    """Delete matching rows in committed rowid windows; (rows, stopped)."""
    bounds = conn.execute(f'SELECT MIN(rowid), MAX(rowid) FROM "{table}"').fetchone()
    if bounds[0] is None:
        return 0, False
    deleted = 0
    for lower in range(int(bounds[0]), int(bounds[1]) + 1, span):
        if should_stop():
            return deleted, True
        with conn:
            window = conn.execute(
                f'DELETE FROM "{table}" WHERE rowid>=? AND rowid<? AND ({where})',
                (lower, lower + span),
            ).rowcount
        if window:
            deleted += window
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted, False


def _unlink_jobs(conn: sqlite3.Connection, generations: str) -> None:
    """A job that produced a generation still did the work; drop the pointer."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
    if "output_generation_id" in columns:
        conn.execute(
            "UPDATE jobs SET output_generation_id=NULL WHERE output_generation_id IN "
            f"(SELECT id FROM {generations})"
        )


def _clear_personal_model(
    conn: sqlite3.Connection, rows: dict[str, int], should_stop: Callable[[], bool]
) -> bool:
    tables = _tables(conn)
    if "personal_model_generations" not in tables:
        return False
    if not _count(conn, "personal_model_generations"):
        for cache in _COMPILER_CACHES:
            if cache in tables and _count(conn, cache):
                with conn:
                    rows[cache] = conn.execute(f'DELETE FROM "{cache}"').rowcount
        return False
    with conn:
        # The pointer restricts deleting the generation it names.
        conn.execute("DELETE FROM personal_model_active_generation")
        # Triggers keep an activated generation's rows immutable. Retiring it
        # first lets them go; an interrupted run leaves it retired, not live.
        conn.execute(
            "UPDATE personal_model_generations "
            "SET state='failed', failed_at=?, failure_reason=? "
            "WHERE state IN ('current','superseded')",
            (iso(utc_now()), "retired: this build holds the compiler back"),
        )
        _unlink_jobs(conn, "personal_model_generations")
    # Records first, in windows: their evidence and dependencies cascade.
    deleted, stopped = _windowed_delete(
        conn, "personal_model_records", "1", _ROWID_SPAN, should_stop
    )
    rows["personal_model_records"] = deleted
    if stopped:
        return True
    with conn:
        # The empty headers cascade the small generation-scoped rows.
        rows["personal_model_generations"] = conn.execute(
            "DELETE FROM personal_model_generations"
        ).rowcount
        for cache in _COMPILER_CACHES:
            if cache in tables:
                rows[cache] = conn.execute(f'DELETE FROM "{cache}"').rowcount
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return False


def _clear_graph(
    conn: sqlite3.Connection, rows: dict[str, int], should_stop: Callable[[], bool]
) -> bool:
    tables = _tables(conn)
    if "graph_runs" not in tables:
        return False
    if "personal_model_generations" in tables and _count(conn, "personal_model_generations"):
        # A personal-model generation restricts deleting the graph it came from.
        return False
    if _count(conn, "graph_runs"):
        with conn:
            _unlink_jobs(conn, "graph_runs")
            # Claims, evidence, relations, aliases and candidates cascade.
            # Entities outlive generations and a correction may name one.
            rows["graph_runs"] = conn.execute("DELETE FROM graph_runs").rowcount
            if "graph_extraction_cache" in tables:
                rows["graph_extraction_cache"] = conn.execute(
                    "DELETE FROM graph_extraction_cache"
                ).rowcount
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Labels have no foreign key, so a deleted graph row leaves its label
    # behind; each rebuild also labelled every surviving entity again.
    clauses = " OR ".join(
        f"(resource_type='{kind}' AND NOT EXISTS "
        f"(SELECT 1 FROM \"{table}\" r WHERE r.id=policy_labels.resource_id))"
        for kind, table in _GRAPH_LABELS
        if table in tables
    ) or "0"
    kinds = ",".join(f"'{kind}'" for kind, _ in _GRAPH_LABELS)
    # The oldest copy of each distinct label stays. One grouped pass finds
    # them; checking each row against its entity's ten thousand copies did not
    # finish on the owner's archive.
    conn.execute("DROP TABLE IF EXISTS temp.held_back_label_keep")
    conn.execute("CREATE TEMP TABLE held_back_label_keep (kept INTEGER PRIMARY KEY)")
    try:
        conn.execute(
            "INSERT INTO temp.held_back_label_keep (kept) SELECT MIN(rowid) "
            f"FROM policy_labels WHERE resource_type IN ({kinds}) "
            "GROUP BY resource_type, resource_id, label, label_source, reasons_json, "
            "policy_version, active"
        )
        duplicate = (
            f"(resource_type IN ({kinds}) AND rowid NOT IN "
            "(SELECT kept FROM temp.held_back_label_keep))"
        )
        where = f"label_source<>'user_override' AND ({clauses} OR {duplicate})"
        deleted, stopped = _windowed_delete(
            conn, "policy_labels", where, _LABEL_ROWID_SPAN, should_stop
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.held_back_label_keep")
    if deleted:
        rows["policy_labels"] = deleted
    return stopped


def _clear_graph_jobs(
    conn: sqlite3.Connection, rows: dict[str, int], should_stop: Callable[[], bool]
) -> bool:
    """Graph jobs an older ingest queued for a builder that will never run."""
    if "jobs" not in _tables(conn):
        return False
    deleted, stopped = _windowed_delete(
        conn,
        "jobs",
        "task='graph_extract' AND state IN ('pending','failed','superseded')",
        _LABEL_ROWID_SPAN,
        should_stop,
    )
    rows["jobs"] = deleted
    return stopped


def _clear_windows(
    conn: sqlite3.Connection, rows: dict[str, int], should_stop: Callable[[], bool]
) -> bool:
    tables = _tables(conn)
    if "conversation_windows" not in tables or not _count(conn, "conversation_windows"):
        return False
    for index in _WINDOW_INDEXES:
        if index in tables:
            with conn:
                rows[index] = conn.execute(f'DELETE FROM "{index}"').rowcount
    deleted, stopped = _windowed_delete(
        conn, "conversation_windows", "1", _ROWID_SPAN, should_stop
    )
    rows["conversation_windows"] = deleted
    return stopped


def prune_events(
    conn: sqlite3.Connection,
    *,
    keep_days: int = EVENT_RETENTION_DAYS,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Delete internal events older than `keep_days`; rows removed.

    Every build writes them and none reads them, so this runs whatever ships.
    """
    if "internal_events" not in _tables(conn):
        return 0
    cutoff = iso(utc_now() - timedelta(days=keep_days))
    deleted, _ = _windowed_delete(
        conn,
        "internal_events",
        f"created_at < '{cutoff}'",
        _LABEL_ROWID_SPAN,
        should_stop,
    )
    return deleted


def _reclaimable(conn: sqlite3.Connection) -> int:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    return page_size * int(conn.execute("PRAGMA freelist_count").fetchone()[0])


def _vacuum_if_room(conn: sqlite3.Connection, db_path: Path | None) -> int:
    """VACUUM when it frees enough and the disk can hold the copy it writes."""
    if db_path is None or _reclaimable(conn) < _VACUUM_MIN_FREED:
        return 0
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    before = int(conn.execute("PRAGMA page_count").fetchone()[0])
    remaining = (before - int(conn.execute("PRAGMA freelist_count").fetchone()[0])) * page_size
    if shutil.disk_usage(db_path.parent).free < remaining * _VACUUM_HEADROOM:
        return 0
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = int(conn.execute("PRAGMA page_count").fetchone()[0])
    return max(before - after, 0) * page_size


def clear_held_back_data(
    conn: sqlite3.Connection,
    *,
    db_path: Path | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> ClearResult:
    """Delete personal-model, graph and window data this build cannot read.

    The caller holds the writer lock. Each part runs only where its builder is
    held back, so the source tree, which still builds both, keeps them.
    """
    from varyz.graph import builder_is_shipped
    from varyz.personal_model import compiler_is_shipped
    from varyz.pipeline import windows_are_shipped

    if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        # The deletes rely on cascades; without them they would orphan rows.
        raise RuntimeError("clearing held-back data needs foreign keys enabled")
    rows: dict[str, int] = {}
    stopped = False
    if not compiler_is_shipped():
        stopped = _clear_personal_model(conn, rows, should_stop)
    if not stopped and not builder_is_shipped():
        stopped = _clear_graph(conn, rows, should_stop)
        if not stopped:
            stopped = _clear_graph_jobs(conn, rows, should_stop)
    if not stopped and not windows_are_shipped():
        stopped = _clear_windows(conn, rows, should_stop)
    rows = {table: count for table, count in rows.items() if count}
    vacuumed = 0
    if rows and not stopped:
        with conn:
            emit_event(
                conn, "held_back_data_cleared", "archive", "held_back", {"rows": rows}
            )
        vacuumed = _vacuum_if_room(conn, db_path)
    return ClearResult(
        rows_by_table=rows,
        stopped=stopped,
        vacuumed_bytes=vacuumed,
        reclaimable_bytes=_reclaimable(conn),
    )
