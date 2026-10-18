"""Delete the graph generations nothing reads any more.

A rebuild stages a whole new generation beside the live one and activates it at
the end. Nothing ever deleted the old one, or the ones that failed, or the ones
autosync abandoned halfway through when a new entry arrived and renamed the
work. On the archive this was written against, thirteen generations held
485,251 alias rows, 368,663 candidate pairs and 71,841 claim relations to
support a single active generation of 73,277 aliases and 696 claims.

What is kept is deliberately conservative: the active generation, the most
recent staging run — which may be a rebuild still in flight, or one that is
resumable — and `keep` recently activated generations behind the live one, so a
bad activation can still be rolled back to a graph that was once trusted.
Everything else is failed runs, abandoned staging runs, and superseded
generations older than the rollback window.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from varyz.domain import iso, utc_now
from varyz.events import emit_event

# Sensitivity labels are keyed by (resource_type, resource_id) with no foreign
# key, so deleting a claim or an alias without them leaves a label describing a
# row that no longer exists — and `varyz trust doctor` counts those as
# unlabelled graph resources.
LABELLED_RESOURCES = (
    ("alias", "entity_aliases", "id"),
    ("claim", "claims", "id"),
    ("evidence_quote", "claim_evidence", "id"),
)

# Mirror generations copy the text and provenance they display, and migration
# 0011 deliberately did not make this informational id a foreign key. It may
# therefore name a pruned graph generation. Every other table that calls a
# column `graph_generation_id` must declare how graph deletion treats it; an
# uncategorised future migration stops prune instead of silently dangling data.
NON_OWNING_GENERATION_REFERENCES = frozenset({"mirror_runs"})


@dataclass(frozen=True)
class PrunedGeneration:
    run_id: str
    state: str
    extractor_version: str
    started_at: str
    rows: int


@dataclass(frozen=True)
class PruneResult:
    generations: tuple[PrunedGeneration, ...] = ()
    rows_by_table: dict[str, int] = field(default_factory=dict)
    labels: int = 0
    jobs: int = 0
    reclaimable_bytes: int = 0
    vacuumed_bytes: int = 0
    dry_run: bool = False

    @property
    def rows(self) -> int:
        # A dry run counts per generation and never fills `rows_by_table`, so
        # this summed to nothing: the command listed four generations holding
        # 107,320 rows and then reported "would delete 0 rows".
        counted = (
            sum(item.rows for item in self.generations)
            if self.dry_run
            else sum(self.rows_by_table.values())
        )
        return counted + self.labels + self.jobs


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _generation_dependencies(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Return cascade-owned tables and references that prohibit deletion.

    SQLite's foreign-key actions are the deletion order. Deriving this policy
    from the migrated schema means a new generation table with ``CASCADE`` is
    reclaimed automatically, while ``RESTRICT``/``NO ACTION`` references keep
    their evidence generation alive.
    """
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    owned: list[str] = []
    protected_by: list[tuple[str, str]] = []
    for table in tables:
        quoted = _quote_identifier(table)
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")}
        if "graph_generation_id" not in columns:
            continue
        references = [
            row
            for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")
            if row[2] == "graph_runs" and row[3] == "graph_generation_id" and row[4] == "id"
        ]
        if not references:
            if table not in NON_OWNING_GENERATION_REFERENCES:
                raise RuntimeError(
                    f"Graph prune has no deletion policy for {table}.graph_generation_id"
                )
            continue
        if len(references) != 1:
            raise RuntimeError(
                f"Graph prune found ambiguous generation references on {table}"
            )
        action = str(references[0][6]).upper()
        if action == "CASCADE":
            owned.append(table)
        elif action in {"RESTRICT", "NO ACTION"}:
            protected_by.append((table, "graph_generation_id"))
        elif action != "SET NULL":
            raise RuntimeError(
                f"Graph prune does not support ON DELETE {action} on "
                f"{table}.graph_generation_id"
            )
    return tuple(owned), tuple(protected_by)


def _referenced_generations(
    conn: sqlite3.Connection, protected_by: tuple[tuple[str, str], ...]
) -> set[str]:
    protected: set[str] = set()
    for table, column in protected_by:
        quoted_table = _quote_identifier(table)
        quoted_column = _quote_identifier(column)
        protected.update(
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
                f"WHERE {quoted_column} IS NOT NULL"
            )
        )
    return protected


def prunable_generations(conn: sqlite3.Connection, *, keep: int = 2) -> list[sqlite3.Row]:
    """Generations no reader and no rollback needs, oldest first."""
    runs = conn.execute(
        "SELECT * FROM graph_runs ORDER BY started_at DESC, id DESC"
    ).fetchall()
    if not runs:
        return []
    protected = {runs[0]["id"]}  # newest run of any state: may be in flight
    activated = [run for run in runs if run["state"] in {"active", "superseded"}]
    protected.update(run["id"] for run in activated[: max(keep, 1)])
    protected.update(run["id"] for run in runs if run["state"] == "active")
    _, protected_by = _generation_dependencies(conn)
    protected.update(_referenced_generations(conn, protected_by))
    return [run for run in reversed(runs) if run["id"] not in protected]


def _delete_generation(
    conn: sqlite3.Connection, run_id: str, generation_tables: tuple[str, ...]
) -> tuple[dict[str, int], int]:
    rows = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
            "WHERE graph_generation_id=?",
            (run_id,),
        ).fetchone()[0]
        for table in generation_tables
    }
    labels = 0
    for resource_type, table, column in LABELLED_RESOURCES:
        cursor = conn.execute(
            f"""DELETE FROM policy_labels
                 WHERE resource_type=?
                   AND resource_id IN (
                     SELECT {column} FROM {table} WHERE graph_generation_id=?
                   )""",
            (resource_type, run_id),
        )
        labels += cursor.rowcount
    # A job that named this generation as its output still records that the
    # work was done; only the pointer becomes meaningless.
    conn.execute(
        "UPDATE jobs SET output_generation_id=NULL WHERE output_generation_id=?",
        (run_id,),
    )
    # The migrated schema owns child-before-parent ordering. Direct generation
    # rows cascade, entity provenance is nulled, and restrictive references
    # were excluded by `prunable_generations`.
    conn.execute("DELETE FROM graph_runs WHERE id=?", (run_id,))
    return rows, labels


def _count_generation(
    conn: sqlite3.Connection, run_id: str, generation_tables: tuple[str, ...]
) -> int:
    total = 0
    for table in generation_tables:
        total += conn.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
            "WHERE graph_generation_id=?",
            (run_id,),
        ).fetchone()[0]
    return total


def _count_labels(conn: sqlite3.Connection, run_ids: list[str]) -> int:
    """What `_delete_generation` would take from `policy_labels`, counted."""
    total = 0
    for resource_type, table, column in LABELLED_RESOURCES:
        for run_id in run_ids:
            total += conn.execute(
                f"""SELECT COUNT(*) FROM policy_labels
                     WHERE resource_type=?
                       AND resource_id IN (
                         SELECT {column} FROM {table} WHERE graph_generation_id=?
                       )""",
                (resource_type, run_id),
            ).fetchone()[0]
    return total


def _finished_jobs_horizon(conn: sqlite3.Connection, *, keep_days: int) -> str | None:
    if keep_days <= 0:
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if not {"state", "completed_at"} <= columns:
        return None
    return iso(utc_now() - timedelta(days=keep_days))


def count_finished_jobs(conn: sqlite3.Connection, *, keep_days: int) -> int:
    """The same window `prune_finished_jobs` deletes, without deleting it."""
    horizon = _finished_jobs_horizon(conn, keep_days=keep_days)
    if horizon is None:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state='done' AND completed_at IS NOT NULL"
        "  AND completed_at < ?",
        (horizon,),
    ).fetchone()[0]


def prune_finished_jobs(conn: sqlite3.Connection, *, keep_days: int) -> int:
    """Drop completed `jobs` rows past the retention window.

    Only `jobs`. `enrichment_jobs` looks like the same thing and is not: it has
    no completion timestamp, only `created_at`, so a retention window there
    would age rows out by when the work was *queued*. It is also keyed
    UNIQUE (entry_id, task) — a per-entry ledger rather than a log — and at
    88,705 rows it is not what makes this database large.
    """
    horizon = _finished_jobs_horizon(conn, keep_days=keep_days)
    if horizon is None:
        return 0
    cursor = conn.execute(
        "DELETE FROM jobs WHERE state='done' AND completed_at IS NOT NULL"
        "  AND completed_at < ?",
        (horizon,),
    )
    return cursor.rowcount


def reclaimable_bytes(conn: sqlite3.Connection) -> int:
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return int(page_size) * int(free)


def prune_graph(
    conn: sqlite3.Connection,
    *,
    keep: int = 2,
    keep_job_days: int = 30,
    dry_run: bool = False,
    vacuum: bool = False,
) -> PruneResult:
    """Delete abandoned, failed and out-of-window graph generations."""
    generation_tables, _ = _generation_dependencies(conn)
    candidates = prunable_generations(conn, keep=keep)
    if dry_run:
        generations = tuple(
            PrunedGeneration(
                run_id=run["id"],
                state=run["state"],
                extractor_version=run["extractor_version"],
                started_at=run["started_at"],
                rows=_count_generation(conn, run["id"], generation_tables),
            )
            for run in candidates
        )
        return PruneResult(
            generations=generations,
            # Named in the same sentence as the row count, so counted with it.
            # A dry run left both at zero and said "0 policy labels, 0 finished
            # jobs" about work it had not looked at.
            labels=_count_labels(conn, [run["id"] for run in candidates]),
            jobs=count_finished_jobs(conn, keep_days=keep_job_days),
            reclaimable_bytes=reclaimable_bytes(conn),
            dry_run=True,
        )

    pruned: list[PrunedGeneration] = []
    totals: dict[str, int] = {}
    labels = 0
    for run in candidates:
        with conn:
            rows, run_labels = _delete_generation(conn, run["id"], generation_tables)
        for table, count in rows.items():
            totals[table] = totals.get(table, 0) + count
        labels += run_labels
        pruned.append(
            PrunedGeneration(
                run_id=run["id"],
                state=run["state"],
                extractor_version=run["extractor_version"],
                started_at=run["started_at"],
                rows=sum(rows.values()),
            )
        )
    with conn:
        jobs = prune_finished_jobs(conn, keep_days=keep_job_days)
        if pruned:
            emit_event(
                conn,
                "graph_generations_pruned",
                "graph_run",
                pruned[-1].run_id,
                {
                    "generations": len(pruned),
                    "rows": sum(totals.values()) + labels + jobs,
                },
            )

    reclaimable = reclaimable_bytes(conn)
    vacuumed = 0
    if vacuum:
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute("VACUUM")
        after = conn.execute("PRAGMA page_count").fetchone()[0]
        vacuumed = max(int(before) - int(after), 0) * int(page_size)
        reclaimable = reclaimable_bytes(conn)

    return PruneResult(
        generations=tuple(pruned),
        rows_by_table=totals,
        labels=labels,
        jobs=jobs,
        reclaimable_bytes=reclaimable,
        vacuumed_bytes=vacuumed,
    )
