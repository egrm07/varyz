"""Deleting the generations nothing reads, and not deleting the ones it does.

A rebuild stages a whole generation beside the live one. Nothing ever removed
the old one, the failed ones, or the ones autosync abandoned when a new entry
renamed the work — thirteen of them held 485,251 alias rows to serve one live
graph of 73,277. These tests pin what prune takes and, more importantly, what
it refuses to take.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.graph.identity import self_entity_id
from varyz.graph.prune import prunable_generations, prune_graph
from varyz.graph.service import rebuild_graph


def _note(conn, external_id: str, body: str, *, day: int = 1) -> str:
    entry_id, _ = dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.NOTE,
            external_id=external_id,
            body=body,
            occurred_start=datetime(2026, 3, day, 9, 0, tzinfo=UTC),
            captured_at=datetime(2026, 3, day, 9, 0, tzinfo=UTC),
        ),
        source="manual",
        sensitivity=Sensitivity.PERSONAL,
    )
    return entry_id


def _generations(conn) -> list[tuple[str, str]]:
    return [
        (row["id"], row["state"])
        for row in conn.execute(
            "SELECT id, state FROM graph_runs ORDER BY started_at, id"
        )
    ]


def _build_generations(conn, cfg, count: int) -> None:
    cfg.graph.extractor = "deterministic"
    cfg.graph.prune_on_sync = False
    for index in range(count):
        _note(conn, f"entry-{index}", f"I moved to Berlin in 201{index}.", day=index + 1)
        rebuild_graph(conn, cfg)


def test_prune_keeps_the_live_graph_and_the_rollback_window(conn, cfg):
    _build_generations(conn, cfg, 4)
    before = _generations(conn)
    assert len(before) == 4

    result = prune_graph(conn, keep=2)

    after = _generations(conn)
    survivors = {state for _, state in after}
    assert len(after) == 2
    assert "active" in survivors
    assert result.generations
    # Nothing that survived is missing its rows.
    active = next(run for run, state in after if state == "active")
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE graph_generation_id=?", (active,)
    ).fetchone()[0]


def test_prune_never_deletes_the_active_generation(conn, cfg):
    _build_generations(conn, cfg, 3)
    active = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]

    prune_graph(conn, keep=1)

    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE id=?", (active,)
    ).fetchone()[0] == 1


def test_prune_preserves_a_generation_cited_by_the_current_personal_model(conn, cfg):
    """A displayed interpretation must keep the graph evidence it was compiled from."""
    _build_generations(conn, cfg, 3)
    cited = prunable_generations(conn, keep=1)[0]["id"]
    claim_id = conn.execute(
        "SELECT id FROM claims WHERE graph_generation_id=? LIMIT 1", (cited,)
    ).fetchone()[0]
    with conn:
        conn.execute(
            """INSERT INTO personal_model_generations
                 (id, graph_generation_id, compiler_version, policy_version,
                  input_content_hash, state, created_at, completed_at, activated_at)
               VALUES ('model-current', ?, 'compiler-v1', 'policy-v1',
                       'model-input', 'staged', ?, ?, NULL)""",
            (
                cited,
                "2026-03-10T09:00:00+00:00",
                "2026-03-10T09:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO personal_model_records
                 (generation_id, record_id, record_kind, record_json,
                  canonical_json_hash, dependency_hash, readiness,
                  sensitivity, created_at)
               VALUES ('model-current', 'interpretation-1', 'interpretation',
                       '{}', 'record-hash', 'dependency-hash', 'ready',
                       'personal', '2026-03-10T09:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO personal_model_record_claims
                 (generation_id, record_id, ordinal, claim_id)
               VALUES ('model-current', 'interpretation-1', 0, ?)""",
            (claim_id,),
        )
        conn.execute(
            """UPDATE personal_model_generations
               SET state='current', activated_at='2026-03-10T09:00:00+00:00'
               WHERE id='model-current'"""
        )
        conn.execute(
            """INSERT INTO personal_model_active_generation
                 (singleton, generation_id, activated_at)
               VALUES (1, 'model-current', '2026-03-10T09:00:00+00:00')"""
        )

    prune_graph(conn, keep=1)

    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE id=?", (cited,)
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE id=?", (claim_id,)).fetchone()[
        0
    ] == 1


def test_prune_derives_new_generation_tables_from_foreign_keys(conn, cfg):
    """A later CASCADE table is reclaimed without extending a Python tuple."""
    _build_generations(conn, cfg, 3)
    doomed = prunable_generations(conn, keep=1)[0]["id"]
    with conn:
        conn.execute(
            """CREATE TABLE later_graph_rows (
                 id TEXT PRIMARY KEY,
                 graph_generation_id TEXT NOT NULL
                   REFERENCES graph_runs(id) ON DELETE CASCADE
               )"""
        )
        conn.execute(
            "INSERT INTO later_graph_rows (id, graph_generation_id) VALUES ('later-1', ?)",
            (doomed,),
        )

    result = prune_graph(conn, keep=1)

    assert result.rows_by_table["later_graph_rows"] == 1
    assert conn.execute("SELECT COUNT(*) FROM later_graph_rows").fetchone()[0] == 0


def test_prune_spares_a_rebuild_that_may_still_be_in_flight(conn, cfg):
    """The newest run can be a rebuild mid-staging, or one that can resume."""
    _build_generations(conn, cfg, 2)
    _note(conn, "later", "I decided to keep measuring.", day=9)
    cfg.graph.extractor = "deterministic"
    seen = 0

    def stop_after_one() -> bool:
        nonlocal seen
        seen += 1
        return seen > 1

    rebuild_graph(conn, cfg, should_stop=stop_after_one)
    staging = conn.execute(
        "SELECT id FROM graph_runs WHERE state='staging' ORDER BY started_at DESC"
    ).fetchone()[0]

    assert staging not in {run["id"] for run in prunable_generations(conn, keep=1)}

    prune_graph(conn, keep=1)

    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE id=?", (staging,)
    ).fetchone()[0] == 1


def test_prune_removes_the_policy_labels_of_what_it_deletes(conn, cfg):
    """An orphaned label describes a row that is gone; the doctor counts those."""
    _build_generations(conn, cfg, 3)
    doomed = [run["id"] for run in prunable_generations(conn, keep=1)]
    assert doomed
    alias_ids = [
        row["id"]
        for run in doomed
        for row in conn.execute(
            "SELECT id FROM entity_aliases WHERE graph_generation_id=?", (run,)
        )
    ]
    assert alias_ids
    placeholders = ",".join("?" for _ in alias_ids)
    assert conn.execute(
        f"SELECT COUNT(*) FROM policy_labels WHERE resource_type='alias'"
        f" AND resource_id IN ({placeholders})",
        alias_ids,
    ).fetchone()[0]

    result = prune_graph(conn, keep=1)

    assert result.labels
    assert conn.execute(
        f"SELECT COUNT(*) FROM policy_labels WHERE resource_type='alias'"
        f" AND resource_id IN ({placeholders})",
        alias_ids,
    ).fetchone()[0] == 0


def test_a_dry_run_reports_without_deleting(conn, cfg):
    _build_generations(conn, cfg, 3)
    before = _generations(conn)

    result = prune_graph(conn, keep=1, dry_run=True)

    assert result.dry_run
    assert result.generations
    assert all(generation.rows for generation in result.generations)
    assert _generations(conn) == before


def test_a_dry_run_counts_what_an_apply_would_delete(conn, cfg):
    """The report said "would delete 0 rows" under the table listing them.

    A dry run counts rows per generation and never fills `rows_by_table`, which
    is what the total summed -- so on the author's archive the command listed
    four generations holding 107,320 rows and then reported none. The policy
    labels and finished jobs in the same sentence were never counted at all.
    """
    _build_generations(conn, cfg, 3)

    planned = prune_graph(conn, keep=1, dry_run=True)

    assert planned.rows == (
        sum(item.rows for item in planned.generations) + planned.labels + planned.jobs
    )
    assert planned.rows
    assert planned.labels

    applied = prune_graph(conn, keep=1)

    assert applied.rows == planned.rows
    assert applied.labels == planned.labels


def test_pruning_leaves_the_live_graph_answering_the_same_questions(conn, cfg):
    _build_generations(conn, cfg, 3)
    active = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    claims = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE graph_generation_id=?", (active,)
    ).fetchone()[0]
    owner = self_entity_id(conn)

    prune_graph(conn, keep=1)

    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE graph_generation_id=?", (active,)
    ).fetchone()[0] == claims
    assert self_entity_id(conn) == owner


def _job(conn, job_id: str, state: str, completed_at: str | None) -> None:
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            """INSERT INTO jobs (id, job_key, resource_type, resource_id, task,
                                 version, state, input_content_hash, created_at,
                                 updated_at, completed_at)
               VALUES (?, ?, 'entry', 'entry-0', 'facets', 'v1', ?, 'hash', ?, ?, ?)""",
            (job_id, job_id, state, now, now, completed_at),
        )


def test_finished_jobs_expire_and_running_ones_do_not(conn, cfg):
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    _job(conn, "job-stale", "done", old)
    _job(conn, "job-recent", "done", fresh)
    _job(conn, "job-pending", "pending", None)
    # A job that never finished has no completion stamp to age out on, and one
    # that finished this morning is still the record of the last cycle.
    _job(conn, "job-forgotten", "done", None)

    result = prune_graph(conn, keep=1, keep_job_days=30)

    assert result.jobs == 1
    survivors = {
        row[0] for row in conn.execute("SELECT id FROM jobs WHERE id LIKE 'job-%'")
    }
    assert survivors == {"job-recent", "job-pending", "job-forgotten"}
