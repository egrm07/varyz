"""Deleting what an older version built for the parts the alpha holds back.

An archive from 0.1.0 keeps every personal-model generation and graph it built
(1.3 GB and 1.7 million labels on the owner's), and the alpha reads none of it.
These tests pin what the cleanup takes, what it leaves (anything the owner
wrote) and that the source tree, which still builds both, keeps them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.graph.service import rebuild_graph
from varyz.held_back_data import clear_held_back_data
from varyz.privacy.labels import label_resource, resource_label
from varyz.privacy.models import LabelSource

_AT = "2026-03-10T09:00:00+00:00"


def _count(conn, table: str, where: str = "1") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _archive(conn, cfg) -> None:
    """An 0.1.0 archive: two graph generations and a current personal model."""
    cfg.graph.extractor = "deterministic"
    cfg.graph.prune_on_sync = False
    for index in range(2):
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=f"entry-{index}",
                body=f"I moved to Berlin in 201{index}.",
                occurred_start=datetime(2026, 3, index + 1, 9, 0, tzinfo=UTC),
                captured_at=datetime(2026, 3, index + 1, 9, 0, tzinfo=UTC),
            ),
            source="manual",
            sensitivity=Sensitivity.PERSONAL,
        )
        rebuild_graph(conn, cfg)
    graph = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    claim = conn.execute(
        "SELECT id FROM claims WHERE graph_generation_id=? LIMIT 1", (graph,)
    ).fetchone()[0]
    with conn:
        conn.execute(
            """INSERT INTO personal_model_generations
                 (id, graph_generation_id, compiler_version, policy_version,
                  input_content_hash, state, created_at, completed_at, activated_at)
               VALUES ('model-current', ?, 'v1', 'p1', 'input', 'staged', ?, ?, NULL)""",
            (graph, _AT, _AT),
        )
        for index in range(3):
            conn.execute(
                """INSERT INTO personal_model_records
                     (generation_id, record_id, record_kind, record_json,
                      canonical_json_hash, dependency_hash, readiness,
                      sensitivity, created_at)
                   VALUES ('model-current', ?, 'interpretation', '{}', 'h', 'd',
                           'ready', 'personal', ?)""",
                (f"record-{index}", _AT),
            )
        conn.execute(
            """INSERT INTO personal_model_record_claims
                 (generation_id, record_id, ordinal, claim_id)
               VALUES ('model-current', 'record-0', 0, ?)""",
            (claim,),
        )
        conn.execute(
            "UPDATE personal_model_generations SET state='current', activated_at=? "
            "WHERE id='model-current'",
            (_AT,),
        )
        conn.execute(
            "INSERT INTO personal_model_active_generation "
            "(singleton, generation_id, activated_at) VALUES (1, 'model-current', ?)",
            (_AT,),
        )


@pytest.fixture
def alpha(monkeypatch):
    import varyz.graph
    import varyz.personal_model

    monkeypatch.setattr(varyz.graph, "builder_is_shipped", lambda: False)
    monkeypatch.setattr(varyz.personal_model, "compiler_is_shipped", lambda: False)


def test_the_alpha_deletes_the_model_and_graph_and_keeps_the_owners_words(
    conn, cfg, alpha
):
    _archive(conn, cfg)
    entity = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()[0]
    with conn:
        label_resource(
            conn, "entity", entity, Sensitivity.SENSITIVE, LabelSource.USER_OVERRIDE, []
        )
        label_resource(
            conn, "claim", "gone", Sensitivity.SENSITIVE, LabelSource.USER_OVERRIDE, []
        )
    entities = _count(conn, "entities")
    entry_labels = _count(conn, "policy_labels", "resource_type='entry'")

    result = clear_held_back_data(conn, db_path=cfg.db_path)

    assert not result.stopped
    for table in (
        "personal_model_generations",
        "personal_model_records",
        "personal_model_record_claims",
        "personal_model_active_generation",
        "graph_runs",
        "claims",
        "claim_evidence",
        "entity_aliases",
    ):
        assert _count(conn, table) == 0, table
    assert result.rows_by_table["personal_model_records"] == 3
    # Entities outlive generations, and a correction may name one.
    assert _count(conn, "entities") == entities
    assert _count(conn, "policy_labels", "resource_type='entry'") == entry_labels
    # What the owner set stays, even for a row that is gone.
    assert _count(conn, "policy_labels", "label_source='user_override'") == 2
    assert resource_label(conn, "entity", entity)[0] == Sensitivity.SENSITIVE
    assert _count(
        conn,
        "policy_labels",
        "resource_type IN ('alias','claim','evidence_quote') "
        "AND label_source<>'user_override'",
    ) == 0

    again = clear_held_back_data(conn, db_path=cfg.db_path)
    assert again.rows == 0


def test_repeated_entity_labels_collapse_to_one_with_the_same_label(conn, cfg, alpha):
    _archive(conn, cfg)
    entity = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()[0]
    with conn:
        for _ in range(5):
            label_resource(
                conn, "entity", entity, Sensitivity.SENSITIVE, LabelSource.INHERITED, ["x"]
            )
    expected = resource_label(conn, "entity", entity)[0]

    clear_held_back_data(conn, db_path=cfg.db_path)

    assert _count(
        conn,
        "policy_labels",
        f"resource_type='entity' AND resource_id='{entity}' AND reasons_json='[\"x\"]'",
    ) == 1
    assert resource_label(conn, "entity", entity)[0] == expected == Sensitivity.SENSITIVE


def test_the_source_tree_keeps_what_it_still_builds(conn, cfg):
    _archive(conn, cfg)
    graphs = _count(conn, "graph_runs")

    result = clear_held_back_data(conn, db_path=cfg.db_path)

    assert result.rows == 0
    assert _count(conn, "graph_runs") == graphs
    assert _count(conn, "personal_model_records") == 3


def test_an_interrupted_clear_finishes_on_the_next_run(conn, cfg, alpha):
    _archive(conn, cfg)

    first = clear_held_back_data(conn, db_path=cfg.db_path, should_stop=lambda: True)

    assert first.stopped
    # The model goes before the graph it restricts, so the graph is untouched.
    assert _count(conn, "graph_runs")
    second = clear_held_back_data(conn, db_path=cfg.db_path)
    assert not second.stopped
    assert _count(conn, "personal_model_records") == 0
    assert _count(conn, "graph_runs") == 0


def _windows_and_jobs(conn) -> None:
    """What an 0.1.x sync left beside the entries: windows, queued graph jobs."""
    from varyz.jobs.repository import enqueue_in_transaction

    with conn:
        for index in range(3):
            conn.execute(
                """INSERT INTO conversation_windows
                     (id, source, thread_id, started_at, ended_at, turns, characters,
                      policy_version, content_hash, created_at)
                   VALUES (?, 'ai_conversations', 't', ?, ?, 4, 400, 'p', 'h', ?)""",
                (f"window-{index}", _AT, _AT, _AT),
            )
            conn.execute(
                "INSERT INTO conversation_windows_fts (window_id, text) VALUES (?, 'hello')",
                (f"window-{index}",),
            )
        conn.execute(
            """INSERT INTO conversation_window_labels
                 (window_id, label, agreed, votes_json, prompt_version, content_hash,
                  created_at)
               VALUES ('window-0', 'personal', 1, '[]', 'v', 'h', ?)""",
            (_AT,),
        )
        for index in range(2):
            enqueue_in_transaction(
                conn, "entry", f"entry-{index}", "graph_extract", "deterministic:1", "h"
            )


def test_the_alpha_deletes_windows_and_the_graph_jobs_it_never_runs(
    conn, cfg, alpha, monkeypatch
):
    import varyz.pipeline

    monkeypatch.setattr(varyz.pipeline, "windows_are_shipped", lambda: False)
    _windows_and_jobs(conn)

    result = clear_held_back_data(conn, db_path=cfg.db_path)

    assert result.rows_by_table["conversation_windows"] == 3
    assert result.rows_by_table["jobs"] == 2
    assert _count(conn, "conversation_windows") == 0
    assert _count(conn, "conversation_window_labels") == 0
    assert _count(conn, "conversation_windows_fts") == 0
    assert _count(conn, "jobs", "task='graph_extract'") == 0
    assert clear_held_back_data(conn, db_path=cfg.db_path).rows == 0


def test_the_source_tree_keeps_its_windows_and_jobs(conn, cfg):
    _windows_and_jobs(conn)

    clear_held_back_data(conn, db_path=cfg.db_path)

    assert _count(conn, "conversation_windows") == 3
    assert _count(conn, "jobs", "task='graph_extract'") == 2


def test_internal_events_keep_a_month(conn):
    from varyz.held_back_data import prune_events

    with conn:
        for event_id, created in (("old", "2026-01-01T00:00:00+00:00"), ("new", None)):
            conn.execute(
                """INSERT INTO internal_events
                     (id, event_type, aggregate_type, aggregate_id, created_at)
                   VALUES (?, 'entry_created', 'entry', ?, COALESCE(?, ?))""",
                (event_id, event_id, created, datetime.now(UTC).isoformat()),
            )

    assert prune_events(conn) == 1
    assert [row[0] for row in conn.execute("SELECT id FROM internal_events")] == ["new"]
