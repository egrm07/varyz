"""Graph integrity diagnostics used by CLI, tests, and the local UI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class GraphIssue:
    code: str
    message: str
    count: int
    severity: str = "error"


def diagnose(
    conn: sqlite3.Connection, *, stale_after: timedelta = timedelta(hours=24)
) -> list[GraphIssue]:
    issues: list[GraphIssue] = []

    orphan_evidence = conn.execute(
        """SELECT COUNT(*) FROM claim_evidence e
           LEFT JOIN claims c ON c.id=e.claim_id
           LEFT JOIN entries n ON n.id=e.entry_id
           WHERE c.id IS NULL OR n.id IS NULL"""
    ).fetchone()[0]
    if orphan_evidence:
        issues.append(
            GraphIssue(
                "orphan_evidence",
                "Evidence links reference a missing claim or entry.",
                orphan_evidence,
            )
        )

    invalid_intervals = conn.execute(
        """SELECT COUNT(*) FROM claims
           WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL
             AND valid_to < valid_from"""
    ).fetchone()[0]
    if invalid_intervals:
        issues.append(
            GraphIssue(
                "invalid_interval",
                "Claims have a validity end before their start.",
                invalid_intervals,
            )
        )

    unresolved = conn.execute(
        """SELECT COUNT(*) FROM entity_candidates c
           JOIN graph_runs g ON g.id=c.graph_generation_id
           WHERE c.state='pending' AND g.state='active'"""
    ).fetchone()[0]
    if unresolved:
        issues.append(
            GraphIssue(
                "unresolved_identity",
                "Identity candidates await a merge/separate decision.",
                unresolved,
                "warning",
            )
        )

    cutoff = (datetime.now(UTC) - stale_after).isoformat(timespec="seconds")
    stale_runs = conn.execute(
        """SELECT COUNT(*) FROM graph_runs
           WHERE state='staging' AND started_at < ?""",
        (cutoff,),
    ).fetchone()[0]
    if stale_runs:
        issues.append(
            GraphIssue(
                "stale_graph_run",
                "Staging graph runs have exceeded the expected lease window.",
                stale_runs,
            )
        )

    unsupported = conn.execute(
        """SELECT COUNT(*) FROM claims c
           JOIN graph_runs r ON r.id=c.graph_generation_id AND r.state='active'
           WHERE c.origin<>'user' AND c.lifecycle_state IN ('active','superseded')
             AND NOT EXISTS (
               SELECT 1 FROM claim_evidence e WHERE e.claim_id=c.id
             )"""
    ).fetchone()[0]
    if unsupported:
        issues.append(
            GraphIssue(
                "claim_without_active_evidence",
                "Active derived claims have no remaining evidence.",
                unsupported,
            )
        )

    stale_quotes = conn.execute(
        """SELECT COUNT(*) FROM claim_evidence e
           JOIN entries n ON n.id=e.entry_id
           WHERE instr(n.body, e.quote)=0"""
    ).fetchone()[0]
    if stale_quotes:
        issues.append(
            GraphIssue(
                "stale_evidence_quote",
                "Evidence quotes no longer occur in their source entry.",
                stale_quotes,
            )
        )

    merged_cycles = conn.execute(
        """WITH RECURSIVE chain(start_id, current_id, depth) AS (
             SELECT id, merged_into_id, 1 FROM entities WHERE merged_into_id IS NOT NULL
             UNION ALL
             SELECT chain.start_id, e.merged_into_id, chain.depth+1
               FROM chain JOIN entities e ON e.id=chain.current_id
              WHERE e.merged_into_id IS NOT NULL AND chain.depth < 100
           )
           SELECT COUNT(*) FROM chain WHERE start_id=current_id"""
    ).fetchone()[0]
    if merged_cycles:
        issues.append(
            GraphIssue(
                "entity_merge_cycle",
                "Entity merge overrides contain a cycle.",
                merged_cycles,
            )
        )
    return issues
