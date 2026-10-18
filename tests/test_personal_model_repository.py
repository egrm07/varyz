"""Focused tests for personal-model generations, reuse, and owner overrides."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from varyz.domain import Sensitivity
from varyz.personal_model.compiler import _staging_order
from varyz.personal_model.models import (
    AlternativeExplanation,
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactValueKind,
    InterpretationCandidate,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    Observation,
    ObservationKind,
    ObservationModality,
    Person,
    Polarity,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    ScopeKind,
    SourceTurn,
    SubjectReference,
    TextFactValue,
    TextSpan,
)
from varyz.personal_model.repository import (
    GenerationState,
    OverrideAction,
    OverrideTargetKind,
    PersonalModelRepository,
    ReplayOutcome,
    compiler_input_dependency,
    model_record_dependency,
    required_model_references,
    required_record_dependencies,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def add_active_graph(conn: sqlite3.Connection, graph_id: str = "graph-1") -> None:
    conn.execute(
        """INSERT INTO graph_runs
           (id, generation_key, extractor_name, extractor_version,
            predicate_version, identity_version, input_content_hash, state, started_at,
            completed_at, activated_at)
           VALUES (?,?,?,?,?,?,?,'active',?,?,?)""",
        (
            graph_id,
            f"key-{graph_id}",
            "test",
            "1",
            "1",
            "1",
            f"input-{graph_id}",
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()


def add_entry(conn: sqlite3.Connection, entry_id: str, content_hash: str) -> None:
    conn.execute(
        """INSERT INTO entries
           (id, kind, source, external_id, content_hash, body, ingested_at)
           VALUES (?,'message','manual',?,?,?,?)""",
        (entry_id, f"external-{entry_id}", content_hash, f"body-{entry_id}", NOW.isoformat()),
    )
    conn.commit()


def add_claim(conn: sqlite3.Connection, claim_id: str = "claim-1") -> None:
    conn.execute(
        """INSERT INTO entities
           (id, entity_type, canonical_label, status, created_at, updated_at,
            graph_generation_id, generation_version)
           VALUES ('graph-person','person','Owner','active',?,?, 'graph-1','1')""",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """INSERT INTO claims
           (id, subject_entity_id, predicate, predicate_version, object_type,
            object_value_json, object_surface, polarity, modality,
            temporal_precision, confidence, sensitivity, extractor_name,
            graph_generation_id, fingerprint, lifecycle_state, origin,
            created_at, updated_at)
           VALUES (?,'graph-person','mentioned','1','text','"sushi"','sushi',
                   'positive','fact','unknown',0.9,'personal','test','graph-1',?,
                   'active','deterministic',?,?)""",
        (claim_id, f"fingerprint-{claim_id}", NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def generation(repo: PersonalModelRepository, generation_id: str):
    return repo.create_generation(
        "graph-1",
        compiler_version="compiler-1",
        policy_version="policy-1",
        input_content_hash=f"input-{generation_id}",
        generation_id=generation_id,
    )


def base(generation_id: str, record_id: str) -> dict[str, object]:
    return {
        "id": record_id,
        "generation_id": generation_id,
        "confidence": 1.0,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.USER_CONFIRMED,
        "producer": ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1"),
        "readiness": Readiness.READY,
        "created_at": NOW,
    }


def person(generation_id: str, record_id: str = "person-owner") -> Person:
    return Person(
        **base(generation_id, record_id),
        canonical_key=f"canonical:{record_id}",
        display_name="Owner" if record_id == "person-owner" else "Example Person",
        is_archive_owner=record_id == "person-owner",
    )


def evidenced_person(generation_id: str) -> Person:
    values = base(generation_id, "person-owner")
    values.update(
        evidence=(
            EvidenceReference(
                entry_id="entry-1",
                claim_id="claim-1",
                role=EvidenceRole.SUPPORTS,
            ),
        ),
        claim_ids=("claim-1",),
        derivation=DerivationKind.EXPLICIT,
        producer=ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="fixture",
            version="1",
            rule_version="1",
        ),
    )
    return Person(
        **values,
        canonical_key="canonical:person-owner",
        display_name="Owner",
        is_archive_owner=True,
    )


def episode(
    generation_id: str,
    *,
    episode_id: str = "episode-1",
    entry_ids: tuple[str, str] = ("entry-1", "entry-2"),
) -> ConversationEpisode:
    values = base(generation_id, episode_id)
    values.update(
        evidence=(
            EvidenceReference(entry_id=entry_ids[0], role=EvidenceRole.SUPPORTS),
            EvidenceReference(entry_id=entry_ids[1], role=EvidenceRole.SUPPORTS),
        ),
        derivation=DerivationKind.EXPLICIT,
        producer=ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="fixture",
            version="1",
            rule_version="1",
        ),
    )
    return ConversationEpisode(
        **values,
        source="manual",
        source_thread_id="thread-1",
        turns=(
            SourceTurn(entry_id=entry_ids[0], order=0, occurred_at=NOW),
            SourceTurn(entry_id=entry_ids[1], order=1, occurred_at=NOW),
        ),
        initiating_entry_id=entry_ids[0],
        target_entry_id=entry_ids[1],
        completeness=ContextCompleteness.COMPLETE,
        boundary_policy_version="1",
    )


def observation(
    generation_id: str,
    *,
    entry_id: str = "entry-2",
    episode_id: str = "episode-1",
    observation_id: str = "observation-1",
) -> Observation:
    literal = f"body-{entry_id}"
    values = base(generation_id, observation_id)
    values.update(
        evidence=(
            EvidenceReference(
                entry_id=entry_id,
                episode_id=episode_id,
                role=EvidenceRole.SUPPORTS,
            ),
        ),
        derivation=DerivationKind.EXPLICIT,
        producer=ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="fixture",
            version="1",
            rule_version="1",
        ),
        readiness=Readiness.LIMITED,
    )
    return Observation(
        **values,
        episode_id=episode_id,
        kind=ObservationKind.STATEMENT,
        proposition="An attributable literal observation.",
        spans=(TextSpan(entry_id=entry_id, start=0, end=len(literal), exact_literal=literal),),
        speaker_attribution=AttributionState.UNKNOWN,
        subject_attribution=AttributionState.UNKNOWN,
        dialogue_context=DialogueContext.DIRECT_ANSWER,
        polarity=Polarity.POSITIVE,
        modality=ObservationModality.ASSERTED,
    )


def fact(generation_id: str, record_id: str = "fact-food") -> Fact:
    return Fact(
        **base(generation_id, record_id),
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-owner"),
        predicate="likes.food",
        value=TextFactValue(
            kind=FactValueKind.TEXT,
            exact_literal="sushi",
            normalized="sushi",
        ),
        asserted_by_person_id="person-owner",
    )


def test_migration_0020_creates_personal_model_storage(conn) -> None:
    version = conn.execute(
        "SELECT version FROM schema_migrations WHERE version=20"
    ).fetchone()
    tables = {
        row["name"]
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name LIKE 'personal_model_%'"""
        )
    }

    assert version is not None
    assert {
        "personal_model_generations",
        "personal_model_active_generation",
        "personal_model_records",
        "personal_model_record_evidence",
        "personal_model_record_claims",
        "personal_model_record_dependencies",
        "personal_model_quarantines",
        "personal_model_run_metrics",
        "personal_model_overrides",
        "personal_model_override_replays",
    } <= tables


def test_atomic_activation_and_failure_preserve_last_readable_model(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))

    active = repo.activate_generation("model-1")

    assert active.state == GenerationState.CURRENT
    assert repo.active_generation_id() == "model-1"
    assert repo.get_record("person-owner") == person("model-1")

    generation(repo, "model-failed")
    repo.stage_record(person("model-failed"))
    failed = repo.fail_generation("model-failed", "annotator unavailable")

    assert failed.state == GenerationState.FAILED
    assert repo.active_generation_id() == "model-1"
    with pytest.raises(ValueError, match="not staged"):
        repo.activate_generation("model-failed")


def test_integrity_failure_rolls_back_without_superseding_current(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.activate_generation("model-1")

    generation(repo, "model-incomplete")
    repo.stage_record(fact("model-incomplete"))

    with pytest.raises(ValueError, match="lacks model dependency membership"):
        repo.activate_generation("model-incomplete")

    assert repo.active_generation_id() == "model-1"
    assert repo.get_generation("model-1").state == GenerationState.CURRENT
    assert repo.get_generation("model-incomplete").state == GenerationState.STAGED


def test_activated_records_and_membership_are_immutable(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.activate_generation("model-1")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """UPDATE personal_model_records SET readiness='limited'
               WHERE generation_id='model-1' AND record_id='person-owner'"""
        )
    conn.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="quarantines are immutable"):
        conn.execute(
            """INSERT INTO personal_model_quarantines
               (id, generation_id, stage, reason_code, detail_json, created_at)
               VALUES ('late','model-1','late','late','{}',?)""",
            (NOW.isoformat(),),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="metrics are immutable"):
        conn.execute(
            """INSERT INTO personal_model_run_metrics
               (generation_id, stage, metric_name, metric_json, recorded_at)
               VALUES ('model-1','late','late','1',?)""",
            (NOW.isoformat(),),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """DELETE FROM personal_model_records
               WHERE generation_id='model-1' AND record_id='person-owner'"""
        )
    conn.rollback()


def test_dependency_hash_reuse_clones_only_unchanged_records(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    dependency = compiler_input_dependency("fixture", {"content": "a"})
    original_hash = repo.stage_record(person("model-1"), dependencies=(dependency,))
    repo.activate_generation("model-1")

    generation(repo, "model-2")
    assert repo.clone_record_if_unchanged(
        source_generation_id="model-1",
        target_generation_id="model-2",
        record_id="person-owner",
        expected_dependency_hash=original_hash,
    )
    repo.activate_generation("model-2")

    assert repo.active_generation_id() == "model-2"
    assert repo.get_generation("model-1").state == GenerationState.SUPERSEDED
    assert repo.get_generation("model-2").reused_record_count == 1
    assert repo.get_record("person-owner").generation_id == "model-2"

    generation(repo, "model-3")
    assert not repo.clone_record_if_unchanged(
        source_generation_id="model-2",
        target_generation_id="model-3",
        record_id="person-owner",
        expected_dependency_hash="changed-dependency-hash",
    )


def test_record_dependencies_and_normalized_query_columns_are_checked(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    person_hash = repo.stage_record(person("model-1"))
    repo.stage_record(
        fact("model-1"),
        dependencies=(
            model_record_dependency("person", "person-owner", person_hash),
        ),
    )
    repo.record_metric("model-1", stage="facts", name="accepted", value=1)
    repo.quarantine(
        "model-1",
        stage="facts",
        reason_code="ambiguous_date",
        record_kind="fact",
        candidate_id="candidate-date",
        detail={"literal": "next Friday"},
    )
    repo.activate_generation("model-1")

    row = conn.execute(
        """SELECT entity_kind, entity_id, normalized_predicate, readiness,
                  sensitivity
           FROM personal_model_records
           WHERE generation_id='model-1' AND record_id='fact-food'"""
    ).fetchone()
    active = repo.active_generation()

    assert tuple(row) == ("person", "person-owner", "likes.food", "ready", "personal")
    assert active is not None
    assert active.metrics == {"facts": {"accepted": 1}}
    assert active.quarantine_count == 1


def test_record_writes_do_not_read_generation_metrics_but_status_still_does(conn) -> None:
    """Large diagnostic payloads must never be fetched per staged/reused record."""
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-source")
    original_hash = repo.stage_record(person("model-source"))
    metrics = {"rows": [{"reason": "fixture", "index": index} for index in range(100)]}
    repo.record_metric("model-source", stage="observations", name="audit", value=metrics)
    repo.activate_generation("model-source")
    generation(repo, "model-target")
    repo.record_metric("model-target", stage="observations", name="audit", value=metrics)

    def refuse_metrics(action, table, column, _database, _trigger):
        if (
            action == sqlite3.SQLITE_READ
            and table == "personal_model_generations"
            and column == "metrics_json"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(refuse_metrics)
    try:
        assert repo.clone_record_if_unchanged(
            source_generation_id="model-source",
            target_generation_id="model-target",
            record_id="person-owner",
            expected_dependency_hash=original_hash,
        )
        repo.stage_record(person("model-target", "person-other"))
        repo.quarantine("model-target", stage="fixture", reason_code="uncertain")
    finally:
        conn.set_authorizer(None)

    target = repo.get_generation("model-target")
    assert target.metrics == {"observations": {"audit": metrics}}
    assert (target.record_count, target.reused_record_count, target.quarantine_count) == (2, 1, 1)
    assert repo.active_generation().metrics == target.metrics
    assert repo.get_record("person-owner", generation_id="model-target").generation_id == (
        "model-target"
    )


def test_generation_write_guard_observes_a_later_failure(conn) -> None:
    """Avoiding diagnostics must not turn a state guard into a stale cached decision."""
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-target")
    repo.stage_record(person("model-target"))
    repo.fail_generation("model-target", "fixture failure")

    with pytest.raises(ValueError, match="not staged"):
        repo.stage_record(person("model-target", "person-other"))
    with pytest.raises(ValueError, match="not staged"):
        repo.quarantine("model-target", stage="fixture", reason_code="uncertain")
    assert repo.get_generation("model-target").record_count == 1


def test_override_prefix_must_replay_in_order_and_orphans_are_quarantined(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    first = repo.append_override(
        target_kind=OverrideTargetKind.PERSON,
        target_record_id="person-old",
        action=OverrideAction.MERGE,
        payload={"into_id": "person-owner"},
        struck_value={"display_name": "Old Alias"},
    )
    generation(repo, "model-1")
    second = repo.append_override(
        target_kind=OverrideTargetKind.INTERPRETATION,
        target_record_id="interpretation-later",
        action=OverrideAction.DELETE,
        payload={"reason": "Owner rejected this interpretation."},
    )

    assert [item.id for item in repo.overrides_for_generation("model-1")] == [first.id]
    with pytest.raises(ValueError, match="not replayed"):
        repo.activate_generation("model-1")

    repo.record_override_replay(
        "model-1",
        first.id,
        outcome=ReplayOutcome.ORPHANED,
        detail="The corrected alias is absent from this graph generation.",
    )
    repo.activate_generation("model-1")

    assert repo.active_generation_id() == "model-1"
    assert repo.get_generation("model-1").quarantine_count == 1
    assert (
        conn.execute(
            """SELECT reason_code FROM personal_model_quarantines
               WHERE generation_id='model-1'"""
        ).fetchone()["reason_code"]
        == "override_orphaned"
    )
    assert second.id not in {
        row["override_id"]
        for row in conn.execute(
            """SELECT override_id FROM personal_model_override_replays
               WHERE generation_id='model-1'"""
        )
    }


def test_override_ledger_is_append_only_and_action_scoped(conn) -> None:
    repo = PersonalModelRepository(conn)
    override = repo.append_override(
        target_kind="fact",
        target_record_id="fact-1",
        action="edit",
        payload={
            "value": {
                "kind": "text",
                "exact_literal": "corrected",
                "normalized": "corrected",
            }
        },
    )

    with pytest.raises(ValueError, match="not valid"):
        repo.append_override(
            target_kind="person",
            target_record_id="person-1",
            action="confirm",
            payload={"value": {"kind": "text", "exact_literal": "owner"}},
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE personal_model_overrides SET actor='other' WHERE id=?", (override.id,)
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM personal_model_overrides WHERE id=?", (override.id,))
    conn.rollback()


def test_interpretation_verdicts_append_and_supersede_in_ledger_order(conn) -> None:
    repo = PersonalModelRepository(conn)
    struck = {"thesis": "The owner protects chosen commitments."}
    first = repo.append_override(
        target_kind="interpretation",
        target_record_id="interpretation-1",
        action="verdict",
        payload={"verdict": "confirm", "kept_part": None},
        struck_value=struck,
        override_id="verdict-first",
    )
    second = repo.append_override(
        target_kind="interpretation",
        target_record_id="interpretation-1",
        action="verdict",
        payload={"verdict": "reject", "kept_part": None},
        struck_value={"thesis": "currently displayed text"},
        override_id="verdict-second",
    )

    rows = repo.conn.execute(
        """SELECT id,sequence,supersedes_override_id,struck_value_json
           FROM personal_model_overrides ORDER BY sequence"""
    ).fetchall()
    assert [row["id"] for row in rows] == [first.id, second.id]
    assert rows[1]["sequence"] > rows[0]["sequence"]
    assert second.supersedes_override_id == first.id
    assert json.loads(rows[1]["struck_value_json"]) == struck


@pytest.mark.parametrize(
    "payload",
    [
        {"verdict": "partial", "kept_part": None},
        {"verdict": "partial", "kept_part": " "},
        {"verdict": "confirm", "kept_part": "some fragment"},
        {"verdict": "reject", "kept_part": "some fragment"},
    ],
)
def test_interpretation_verdict_payload_names_only_an_exact_partial(payload, conn) -> None:
    with pytest.raises(ValueError):
        PersonalModelRepository(conn).append_override(
            target_kind="interpretation",
            target_record_id="interpretation-1",
            action="verdict",
            payload=payload,
            struck_value={"thesis": "Original thesis"},
        )


def test_active_pointer_rejects_a_staged_generation(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.activate_generation("model-1")
    generation(repo, "model-staged")

    with pytest.raises(sqlite3.IntegrityError, match="cannot name a non-current"):
        conn.execute(
            """UPDATE personal_model_generations SET state='superseded'
               WHERE id='model-1'"""
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="requires a current generation"):
        conn.execute(
            """UPDATE personal_model_active_generation SET generation_id='model-staged'
               WHERE singleton=1"""
        )
    conn.rollback()

    assert repo.active_generation_id() == "model-1"


@pytest.mark.parametrize(
    ("target_kind", "action", "payload"),
    [
        ("person", "merge", {"into_id": " "}),
        ("person", "split", {"resulting_record_ids": ["only-one"]}),
        ("fact", "confirm", {"value": {}}),
        ("fact", "edit", {"value": {}}),
        ("fact", "reject", {"reason": " "}),
        ("preference", "supersede", {"replacement_record_id": " "}),
        ("goal", "reparent", {"parent_goal_ids": ["goal-1", "goal-1"]}),
        ("goal", "reclassify", {"kind": "not-a-goal-kind"}),
        ("interpretation", "dispute", {"reason": ""}),
        ("interpretation", "delete", {}),
        ("interpretation", "restore", {"reason": "ok", "unexpected": True}),
    ],
)
def test_override_payload_contracts_reject_malformed_or_extra_fields(
    conn, target_kind: str, action: str, payload: dict
) -> None:
    repo = PersonalModelRepository(conn)

    with pytest.raises(ValueError):
        repo.append_override(
            target_kind=target_kind,
            target_record_id="target-1",
            action=action,
            payload=payload,
        )


def test_applied_replay_requires_a_matching_record_of_the_target_kind(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    override = repo.append_override(
        target_kind="fact",
        target_record_id="fact-food",
        action="edit",
        payload={
            "value": {
                "kind": "text",
                "exact_literal": "ramen",
                "normalized": "ramen",
            }
        },
    )
    generation(repo, "model-1")
    person_hash = repo.stage_record(person("model-1"))
    repo.stage_record(
        fact("model-1"),
        dependencies=(
            model_record_dependency("person", "person-owner", person_hash),
        ),
    )

    with pytest.raises(ValueError, match="wrong target kind"):
        repo.record_override_replay(
            "model-1",
            override.id,
            outcome="applied",
            matched_record_id="person-owner",
        )
    with pytest.raises(ValueError, match="requires a matched record"):
        repo.record_override_replay("model-1", override.id, outcome="applied")

    repo.record_override_replay(
        "model-1",
        override.id,
        outcome="applied",
        matched_record_id="fact-food",
    )
    repo.activate_generation("model-1")

    assert repo.active_generation_id() == "model-1"


def test_conflicting_override_blocks_activation(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    override = repo.append_override(
        target_kind="interpretation",
        target_record_id="interpretation-1",
        action="delete",
        payload={"reason": "Owner rejected it."},
    )
    generation(repo, "model-1")
    repo.record_override_replay(
        "model-1",
        override.id,
        outcome="conflict",
        detail="Two compiler candidates claim the corrected stable identity.",
    )

    with pytest.raises(ValueError, match="blocks activation"):
        repo.activate_generation("model-1")

    assert repo.active_generation_id() is None


def test_episode_and_observation_membership_is_validated(conn) -> None:
    add_active_graph(conn)
    for entry_id in ("entry-1", "entry-2", "entry-3"):
        add_entry(conn, entry_id, f"hash-{entry_id}")
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    episode_record = episode("model-1")
    episode_hash = repo.stage_record(
        episode_record,
        dependencies=required_record_dependencies(conn, "graph-1", episode_record),
    )
    outside = observation("model-1", entry_id="entry-3")
    repo.stage_record(
        outside,
        dependencies=(
            *required_record_dependencies(conn, "graph-1", outside),
            model_record_dependency("conversation_episode", "episode-1", episode_hash),
        ),
    )

    with pytest.raises(ValueError, match="outside episode"):
        repo.activate_generation("model-1")


def test_interpretation_observations_must_belong_to_the_assigned_episode(conn) -> None:
    add_active_graph(conn)
    for entry_id in ("entry-1", "entry-2", "entry-3", "entry-4"):
        add_entry(conn, entry_id, f"hash-{entry_id}")
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    episode_one = episode("model-1")
    episode_two = episode(
        "model-1", episode_id="episode-2", entry_ids=("entry-3", "entry-4")
    )
    observation_one = observation("model-1")
    episode_one_hash = repo.stage_record(
        episode_one,
        dependencies=required_record_dependencies(conn, "graph-1", episode_one),
    )
    episode_two_hash = repo.stage_record(
        episode_two,
        dependencies=required_record_dependencies(conn, "graph-1", episode_two),
    )
    observation_hash = repo.stage_record(
        observation_one,
        dependencies=(
            *required_record_dependencies(conn, "graph-1", observation_one),
            model_record_dependency(
                "conversation_episode", "episode-1", episode_one_hash
            ),
        ),
    )
    candidate_fields = base("model-1", "candidate-1")
    candidate_fields["readiness"] = Readiness.STAGED
    candidate = InterpretationCandidate(
        **candidate_fields,
        concept_key="contextual_value",
        hypothesis="A deliberately mismatched episode assignment.",
        kind=InterpretationKind.VALUE,
        scope=InterpretationScope(kind=ScopeKind.GLOBAL),
        evidence_assignments=(
            InterpretationEvidence(
                episode_id="episode-2",
                observation_ids=("observation-1",),
                role=EvidenceRole.SUPPORTS,
                independence_group_id="decision-1",
            ),
        ),
        alternatives=(
            AlternativeExplanation(
                thesis="A different context explains it.",
                distinguishing_evidence="Check the episode actually containing the reply.",
            ),
        ),
    )
    repo.stage_record(
        candidate,
        dependencies=(
            model_record_dependency(
                "conversation_episode", "episode-2", episode_two_hash
            ),
            model_record_dependency("observation", "observation-1", observation_hash),
        ),
    )

    with pytest.raises(ValueError, match="wrong episode"):
        repo.activate_generation("model-1")


def test_entry_and_claim_content_dependencies_prevent_stale_activation_and_reuse(conn) -> None:
    add_active_graph(conn)
    add_entry(conn, "entry-1", "entry-hash-a")
    add_claim(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    record = evidenced_person("model-1")
    dependencies = required_record_dependencies(conn, "graph-1", record)
    dependency_hash = repo.stage_record(record, dependencies=dependencies)

    conn.execute("UPDATE entries SET content_hash='entry-hash-b' WHERE id='entry-1'")
    conn.commit()
    with pytest.raises(ValueError, match="stale"):
        repo.activate_generation("model-1")

    conn.execute("UPDATE entries SET content_hash='entry-hash-a' WHERE id='entry-1'")
    conn.commit()
    repo.activate_generation("model-1")
    generation(repo, "model-2")
    conn.execute("UPDATE claims SET object_surface='ramen' WHERE id='claim-1'")
    conn.commit()

    assert not repo.clone_record_if_unchanged(
        source_generation_id="model-1",
        target_generation_id="model-2",
        record_id="person-owner",
        expected_dependency_hash=dependency_hash,
    )
    with pytest.raises(ValueError, match="stale"):
        repo.stage_record(evidenced_person("model-2"), dependencies=dependencies)

    generation(repo, "model-3")
    current_record = evidenced_person("model-3")
    current_dependencies = required_record_dependencies(conn, "graph-1", current_record)
    repo.stage_record(current_record, dependencies=current_dependencies)
    conn.execute("UPDATE claims SET object_surface='pizza' WHERE id='claim-1'")
    conn.commit()

    with pytest.raises(ValueError, match="stale"):
        repo.activate_generation("model-3")


def test_contradicting_facts_do_not_depend_on_each_other() -> None:
    """A contradiction is mutual, so it cannot be a dependency of either fact.

    Two facts that disagree each name the other.  Asking staging to write a
    dependency row for that asks each record's hash to be computed from the
    other's, which is not computable in either order, and the compile refused
    itself with "depends on unstaged fact" -- after two hours of reading.
    """

    first = fact("model-1", "fact-a").model_copy(
        update={"contradicted_by_fact_ids": ("fact-b",)}
    )
    second = fact("model-1", "fact-b").model_copy(
        update={"contradicted_by_fact_ids": ("fact-a",)}
    )

    assert ("fact", "fact-b") not in required_model_references(first)
    assert ("fact", "fact-a") not in required_model_references(second)
    assert [item.id for item in _staging_order([first, second])] == ["fact-a", "fact-b"]


def test_a_fact_is_staged_after_the_fact_it_supersedes() -> None:
    """Supersession is a dependency, and record ids do not encode it.

    Ids are content hashes.  Sorting by them put a fact before the fact it
    supersedes roughly half the time, which is the other half of the same
    refusal.
    """

    superseded = fact("model-1", "fact-z")
    later = fact("model-1", "fact-a").model_copy(
        update={"supersedes_fact_ids": ("fact-z",)}
    )

    assert ("fact", "fact-z") in required_model_references(later)
    assert [item.id for item in _staging_order([later, superseded])] == [
        "fact-z",
        "fact-a",
    ]


def test_an_observation_is_staged_after_the_project_it_is_about() -> None:
    """The kind table alone was wrong across kinds too.

    It read observations before projects, and an observation may take a
    project as its subject.
    """

    from varyz.personal_model.models import Project

    project = Project(
        **base("model-1", "project-1"),
        canonical_key="canonical:project-1",
        display_name="A project",
    )
    about = observation("model-1").model_copy(
        update={"subject_kind": EntityKind.PROJECT, "subject_id": "project-1"}
    )

    assert ("project", "project-1") in required_model_references(about)
    assert [item.id for item in _staging_order([about, project])] == [
        "project-1",
        "observation-1",
    ]


def test_a_reference_cycle_is_named_rather_than_staged_wrong() -> None:
    """A cycle in the acyclic references is corrupt data, and says so.

    Nothing should build two goals that depend on each other.  If something
    does, the order does not exist, and saying which records are in the knot
    is more use than staging one of them first and failing later on a hash.
    """

    from varyz.personal_model.models import Goal, GoalKind, GoalStatus

    def goal(record_id: str, depends_on: str) -> Goal:
        return Goal(
            **base("model-1", record_id),
            canonical_key=f"canonical:{record_id}",
            title=record_id,
            kind=GoalKind.GOAL,
            status=GoalStatus.ACTIVE,
            dependency_goal_ids=(depends_on,),
        )

    with pytest.raises(ValueError, match="cycle") as refused:
        _staging_order([goal("goal-a", "goal-b"), goal("goal-b", "goal-a")])
    assert "goal-a" in str(refused.value)

def test_a_failed_generation_is_discardable_and_takes_its_rows_with_it(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-dead")
    repo.stage_record(person("model-dead"))
    repo.fail_generation("model-dead", "annotator unavailable")

    assert repo.discard_generation("model-dead") == 1

    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_generations WHERE id='model-dead'"
    ).fetchone()[0] == 0
    # The cascade is the point: a real failure leaves millions of dependency
    # rows behind, and they are only reachable through the generation row.
    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_records WHERE generation_id='model-dead'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_record_dependencies"
        " WHERE generation_id='model-dead'"
    ).fetchone()[0] == 0


def test_discarding_records_is_checkpointed_in_bounded_windows(conn, monkeypatch) -> None:
    """A large failed compile must not put its whole cascade in one WAL transaction."""
    from varyz.personal_model import repository as repository_module

    monkeypatch.setattr(repository_module, "_DISCARD_RECORD_ROWID_SPAN", 1)
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-dead")
    for number in range(3):
        repo.stage_record(person("model-dead", f"person-dead-{number}"))
    repo.fail_generation("model-dead", "annotator unavailable")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    try:
        assert repo.discard_generation("model-dead") == 3
    finally:
        conn.set_trace_callback(None)

    record_deletes = [
        statement
        for statement in statements
        if statement.startswith("DELETE FROM personal_model_records ")
    ]
    checkpoints = [
        statement
        for statement in statements
        if statement == "PRAGMA wal_checkpoint(TRUNCATE)"
    ]
    # SQLite repeats the originating statement in its trace for each foreign-key
    # trigger it executes, so distinct SQL texts are the actual record windows.
    assert len(set(record_deletes)) == 3
    assert len(checkpoints) == 3


def test_an_activated_generation_is_not_discardable(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.activate_generation("model-1")

    with pytest.raises(ValueError, match="history"):
        repo.discard_generation("model-1")

    assert repo.active_generation_id() == "model-1"


def test_discarding_a_failed_generation_leaves_the_active_one_readable(conn) -> None:
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.activate_generation("model-1")
    generation(repo, "model-dead")
    repo.stage_record(person("model-dead"))
    repo.fail_generation("model-dead", "annotator unavailable")

    repo.discard_generation("model-dead")

    assert repo.active_generation_id() == "model-1"
    assert repo.get_record("person-owner") == person("model-1")


def test_activation_survives_more_evidence_entries_than_sqlite_will_bind(
    conn, monkeypatch
) -> None:
    """The integrity check named every cited entry in one ``IN (...)``.

    A real generation cites 45,601 of them, past what SQLite will bind, so the
    pointer swap raised "too many SQL variables" after the compile had done all
    eight hours of its work -- and nothing read the failure, because the
    activate checkpoint is written before the swap and said "complete" either
    way. The cap is lowered here rather than staging forty thousand records:
    what has to hold is that the question is asked in pieces and still answered
    whole.
    """
    from varyz.personal_model import repository as repository_module

    monkeypatch.setattr(repository_module, "_IN_CHUNK", 2)
    add_active_graph(conn)
    entry_ids = tuple(f"entry-{number}" for number in range(1, 7))
    for entry_id in entry_ids:
        add_entry(conn, entry_id, f"hash-{entry_id}")
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    for number in range(3):
        pair = entry_ids[number * 2 : number * 2 + 2]
        record = episode("model-1", episode_id=f"episode-{number}", entry_ids=pair)
        repo.stage_record(
            record, dependencies=required_record_dependencies(conn, "graph-1", record)
        )

    repo.activate_generation("model-1")

    assert repo.active_generation_id() == "model-1"


def test_chunking_a_lookup_leaves_nothing_out(monkeypatch) -> None:
    """Chunking must not turn a check of everything into a check of the first chunk."""
    from varyz.personal_model import repository as repository_module
    from varyz.personal_model.repository import in_chunks

    monkeypatch.setattr(repository_module, "_IN_CHUNK", 3)
    values = {f"id-{number:03d}" for number in range(7)}

    chunks = in_chunks(values)

    assert [len(chunk) for chunk in chunks] == [3, 3, 1]
    assert {item for chunk in chunks for item in chunk} == values


def test_reading_many_records_asks_for_the_kinds_the_caller_named(conn) -> None:
    """One sweep, filtered in SQL: the Mirror reads five kinds of eleven."""
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.stage_record(person("model-1", "person-other"))
    repo.activate_generation("model-1")

    people = repo.read_records(("person",), generation_id="model-1")

    assert [record_id for record_id, _, _ in people] == [
        "person-other",
        "person-owner",
    ]
    assert {kind for _, kind, _ in people} == {"person"}
    assert [record for _, _, record in people] == [
        person("model-1", "person-other"),
        person("model-1"),
    ]
    assert repo.read_records(("fact",), generation_id="model-1") == []
    assert len(repo.read_records(generation_id="model-1")) == 2


def test_reading_many_records_keeps_the_guards_a_single_read_has(conn) -> None:
    """Readiness, the kind vocabulary and the hash check all still apply.

    Staged rather than activated, because an activated record cannot be
    altered and both guards here are about reading an altered one.
    """
    add_active_graph(conn)
    repo = PersonalModelRepository(conn)
    generation(repo, "model-1")
    repo.stage_record(person("model-1"))
    repo.stage_record(person("model-1", "person-other"))
    conn.execute(
        "UPDATE personal_model_records SET readiness='quarantined' WHERE record_id=?",
        ("person-other",),
    )

    ready = repo.read_records(
        ("person",), generation_id="model-1", readiness={"ready", "limited"}
    )

    assert [record_id for record_id, _, _ in ready] == ["person-owner"]
    assert repo.read_records(
        ("person",), generation_id="model-1", readiness=set()
    ) == []
    with pytest.raises(ValueError, match="unknown personal-model record kind"):
        repo.read_records(("nonsense",), generation_id="model-1")

    conn.execute(
        "UPDATE personal_model_records SET record_json=? WHERE record_id=?",
        ('{"tampered": true}', "person-owner"),
    )

    with pytest.raises(RuntimeError, match="failed hash check"):
        repo.read_records(("person",), generation_id="model-1")
