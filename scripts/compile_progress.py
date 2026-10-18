"""One-line JSON view of personal-model compile progress, for a supervisor.

Read-only and never fatal: a probe that raises would stop the run it is
watching, so every failure is reported as a field rather than an exit code.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# The compiler's stages in order.  Repeated here rather than imported because
# this probe runs under whatever interpreter the supervisor can find, which is
# not always one that can import varyz.  tests/test_compile_progress.py
# fails if this drifts from ``CompilerStage``.
STAGES = (
    "pin_inputs",
    "identity",
    "episodes",
    "observations",
    "memory",
    "projects",
    "relationships",
    "synthesis",
    "overrides",
    "persist",
    "readiness",
    "validate",
    "activate",
)


def _connect(database: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        # A WAL database whose shared-memory file does not exist yet cannot be
        # opened read-only, because a read-only connection may not create it.
        # This connection still only reads -- but it would create the file if
        # it were missing, and a mistyped argument then leaves an empty
        # database on disk under the name of the flag that was meant to follow
        # the path.  Only fall back onto a database that is already there.
        if not database.exists():
            raise
        conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _scalar(conn: sqlite3.Connection, sql: str) -> int | None:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else row[0]


def _newest_generation(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            """SELECT id FROM personal_model_generations
                ORDER BY created_at DESC, id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["id"])


def _detail(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """What the run is worth, rather than how far along it is.

    A compile activates even when its semantic stages went stale -- the
    composed policy sets ``allow_literal_only_activation`` -- so "activated"
    and "outcome: completed" are both true of a run whose Mirror has nothing
    in it.  Told apart by these two: which stages did not reach ``complete``,
    and what kinds of record the generation actually holds.  Generation
    01M00VV3 carries 27,215 records and every one of them is a
    ``conversation_episode``, which is the shape this is here to make visible
    in the report rather than in a follow-up query nobody runs.

    Only for the report.  The poll loop must stay cheap: it runs once a
    minute against an archive a compile is writing to.
    """

    generation = _newest_generation(conn)
    payload["newest_generation"] = generation
    if generation is None:
        return
    try:
        payload["record_kinds"] = {
            str(row["record_kind"]): int(row["n"])
            for row in conn.execute(
                """SELECT record_kind, COUNT(*) AS n FROM personal_model_records
                    WHERE generation_id=? GROUP BY record_kind ORDER BY n DESC""",
                (generation,),
            )
        }
    except sqlite3.Error:
        payload["record_kinds"] = None
    try:
        recorded = {
            str(row["stage"]): str(row["state"])
            for row in conn.execute(
                """SELECT stage, state FROM personal_model_compiler_checkpoints
                    WHERE generation_id=? ORDER BY rowid""",
                (generation,),
            )
        }
        # A stage the compile never reached writes no row at all, so reading
        # only what is recorded says "nothing outstanding" about a run that
        # died at ``memory`` with nine stages still ahead of it.  Name every
        # stage, and call the missing ones what they are.
        payload["checkpoints"] = {
            stage: recorded.get(stage, "not reached") for stage in STAGES
        }
    except sqlite3.Error:
        payload["checkpoints"] = None
    try:
        # The other half of a hollow activation, and the one no stage state
        # shows: a stage can reach ``complete`` having run with no semantic
        # model behind it, and the compiler records which ones did exactly
        # here.  An empty list is the good case and says so.
        row = conn.execute(
            """SELECT metric_json FROM personal_model_run_metrics
                WHERE generation_id=? AND stage='compiler' AND metric_name='stale_stages'""",
            (generation,),
        ).fetchone()
        payload["stale_stages"] = (
            json.loads(str(row["metric_json"])) if row is not None else None
        )
    except (sqlite3.Error, ValueError):
        payload["stale_stages"] = None


def _activating(conn: sqlite3.Connection) -> bool:
    """Say whether the newest generation is inside the activation switch.

    The compiler writes the ``activate`` checkpoint *before* it activates,
    because every generation-scoped row becomes immutable the moment the
    generation goes ``current``.  So ``activate: complete`` above a generation
    still at ``staged`` means the switch is being thrown right now.

    That window writes nothing a watcher can see -- no annotation, no record,
    no database growth, no log line -- and it is long: it validates the
    integrity of every record in the generation and then rewrites the
    generation row inside one transaction.  On 2026-09-07 it ran past
    forty-five minutes twice and the supervisor killed the compile as stalled
    both times, four and a half hours into a run, each time at the last step.
    """
    try:
        row = conn.execute(
            """SELECT g.state AS state, c.state AS checkpoint
                 FROM personal_model_generations g
                 LEFT JOIN personal_model_compiler_checkpoints c
                   ON c.generation_id = g.id AND c.stage = 'activate'
                ORDER BY g.created_at DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return row["state"] == "staged" and row["checkpoint"] == "complete"


def main() -> None:
    database = Path(sys.argv[1])
    full = "--full" in sys.argv[2:]
    payload: dict[str, Any] = {"database": str(database)}
    try:
        payload["db_bytes"] = database.stat().st_size
    except OSError:
        payload["db_bytes"] = None
    try:
        conn = _connect(database)
    except sqlite3.Error as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(payload, sort_keys=True))
        return
    try:
        payload["annotations"] = _scalar(
            conn, "SELECT COUNT(*) FROM personal_model_observation_annotation_cache"
        )
        payload["records"] = _scalar(conn, "SELECT COUNT(*) FROM personal_model_records")
        try:
            row = conn.execute(
                """SELECT generation_id,state,episodes_annotated,episodes_total,
                          episodes_remaining,annotation_calls,cache_hits,
                          budget_exhausted_by,updated_at
                     FROM personal_model_observation_progress
                    ORDER BY updated_at DESC, generation_id DESC LIMIT 1"""
            ).fetchone()
            payload["latest"] = dict(row) if row is not None else None
        except sqlite3.Error as exc:
            payload["latest"] = None
            payload["error"] = f"{type(exc).__name__}: {exc}"
        # There is no `active` state. The column's CHECK constraint allows
        # `staged`, `current`, `superseded` and `failed`, so the old query here
        # matched nothing, ever, and every compile report Egor has read since
        # this probe was written says `active generation |  |` -- blank on the
        # runs that activated one just as loudly as on the runs that did not.
        # What activation actually writes is the singleton row below, which is
        # what `PersonalModelRepository.active_generation_id` reads; `current`
        # is the state that row implies and is kept as a fallback for an
        # archive caught between the two writes.
        try:
            row = conn.execute(
                "SELECT generation_id AS id FROM personal_model_active_generation"
                " WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is None:
            try:
                row = conn.execute(
                    "SELECT id FROM personal_model_generations"
                    " WHERE state='current' LIMIT 1"
                ).fetchone()
            except sqlite3.Error:
                row = None
        payload["active_generation"] = None if row is None else row["id"]
        payload["activating"] = _activating(conn)
        if full:
            _detail(conn, payload)
    finally:
        conn.close()
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
