"""Typed, active-generation-only life-model lookup acceptance tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from varyz.db.connection import _iter_migrations, connect, migrate, open_db
from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.memory import FactCandidate, MemoryCompiler
from varyz.personal_model.models import (
    AliasKind,
    AttributionState,
    CountersearchState,
    DerivationKind,
    DialogueContext,
    EntityAlias,
    EntityKind,
    Event,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValueKind,
    Goal,
    GoalKind,
    GoalStatus,
    Interpretation,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    LocationFactValue,
    MonthDayFactValue,
    Observation,
    ObservationKind,
    ObservationModality,
    Person,
    Polarity,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    RecordState,
    RelationshipMeasurement,
    RelationshipSnapshot,
    ScopeKind,
    SubjectReference,
    TemporalScope,
    TextFactValue,
    TextSpan,
    VerificationState,
)
from varyz.personal_model.query import (
    EntitySelector,
    LifeModelQueryService,
    LifeQuery,
    LifeQueryResult,
    LifeRelation,
    QueryDisposition,
    QueryStatus,
    TrustedLocalSurface,
    UncompiledEvidenceCandidate,
    issue_trusted_local_sensitivity_capability,
    parse_life_query,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
PRODUCER = ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1")


def _base(generation_id: str, record_id: str, *, readiness: Readiness = Readiness.READY):
    return {
        "id": record_id,
        "generation_id": generation_id,
        "confidence": 0.95,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.USER_CONFIRMED,
        "producer": PRODUCER,
        "readiness": readiness,
        "created_at": NOW,
    }


def _person(
    generation_id: str,
    record_id: str,
    display_name: str,
    *,
    alias: str | None = None,
    owner: bool = False,
) -> Person:
    aliases = ()
    if alias:
        aliases = (
            EntityAlias(
                kind=AliasKind.USERNAME,
                exact_value=alias,
                normalized_value=alias.casefold(),
                source="telegram",
                source_account="main",
                verification=VerificationState.AUTOMATIC,
            ),
        )
    return Person(
        **_base(generation_id, record_id),
        canonical_key=f"person:{record_id}",
        display_name=display_name,
        aliases=aliases,
        is_archive_owner=owner,
        state=RecordState.ACTIVE,
    )


def _insert_generation(conn: sqlite3.Connection, generation_id: str) -> None:
    conn.execute(
        """INSERT INTO personal_model_generations
           (id, graph_generation_id, compiler_version, policy_version,
            input_content_hash, state, created_at)
           VALUES (?, 'graph-1', 'test', 'test', ?, 'staged', ?)""",
        (generation_id, f"input-{generation_id}", NOW.isoformat()),
    )


def _insert_record(conn: sqlite3.Connection, record) -> None:
    payload = record.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    normalized_predicate = getattr(record, "predicate", None)
    normalized_concept = getattr(record, "concept_key", None)
    conn.execute(
        """INSERT INTO personal_model_records
           (generation_id, record_id, record_kind, record_json,
            canonical_json_hash, dependency_hash, entity_kind, entity_id,
            normalized_predicate, normalized_concept, readiness, valid_from,
            valid_to, sensitivity, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record.generation_id,
            record.id,
            _kind(record),
            encoded,
            hashlib.sha256(encoded.encode()).hexdigest(),
            f"dependency-{record.id}",
            None,
            None,
            normalized_predicate,
            normalized_concept,
            str(record.readiness),
            record.temporal.valid_from.isoformat() if record.temporal.valid_from else None,
            record.temporal.valid_to.isoformat() if record.temporal.valid_to else None,
            str(record.sensitivity),
            record.created_at.isoformat(),
        ),
    )


def _kind(record) -> str:
    names = {
        Person: "person",
        Project: "project",
        Goal: "goal",
        Fact: "fact",
        Preference: "preference",
        Event: "event",
        RelationshipSnapshot: "relationship_snapshot",
        Interpretation: "interpretation",
        Observation: "observation",
    }
    return names[type(record)]


def _activate(conn: sqlite3.Connection, generation_id: str) -> None:
    current = conn.execute(
        "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
    ).fetchone()
    if current:
        conn.execute("DELETE FROM personal_model_active_generation WHERE singleton=1")
        conn.execute(
            "UPDATE personal_model_generations SET state='superseded' WHERE id=?",
            (current[0],),
        )
    conn.execute(
        """UPDATE personal_model_generations
           SET state='current', activated_at=? WHERE id=?""",
        (NOW.isoformat(), generation_id),
    )
    conn.execute(
        """INSERT INTO personal_model_active_generation
           (singleton, generation_id, activated_at) VALUES (1,?,?)""",
        (generation_id, NOW.isoformat()),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    database = open_db(tmp_path / "query.db")
    database.execute(
        """INSERT INTO graph_runs
           (id, generation_key, extractor_name, extractor_version, predicate_version,
            identity_version, input_content_hash, state, started_at, completed_at, activated_at)
           VALUES ('graph-1','key','test','1','1','1','hash','active',?,?,?)""",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    _insert_generation(database, "model-1")
    owner = _person("model-1", "person-egor", "Egor", owner=True)
    anna = _person("model-1", "person-anna", "Anna", alias="@anna")
    for record in (owner, anna):
        _insert_record(database, record)
    for record_id, value, predicate in (
        ("pref-peonies", "peonies", "preference.flower"),
        ("pref-sushi", "sushi", "preference.food"),
    ):
        _insert_record(
            database,
            Preference(
                **_base("model-1", record_id),
                subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
                predicate=predicate,
                object=TextFactValue(kind="text", exact_literal=value, normalized=value),
                valence=PreferenceValence.LIKE,
                status=FactStatus.ACTIVE,
            ),
        )
    _insert_record(
        database,
        Fact(
            **_base("model-1", "fact-birthday"),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
            predicate="person.birth_date",
            value=MonthDayFactValue(kind="month_day", exact_literal="14 May", month=5, day=14),
            status=FactStatus.ACTIVE,
        ),
    )
    _insert_record(
        database,
        Fact(
            **_base("model-1", "fact-city-old"),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
            predicate="person.current_location",
            value=LocationFactValue(kind="location", exact_literal="London", label="London"),
            status=FactStatus.CONTRADICTED,
            contradicted_by_fact_ids=("fact-city-new",),
        ),
    )
    _insert_record(
        database,
        Fact(
            **_base("model-1", "fact-city-new"),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
            predicate="person.current_location",
            value=LocationFactValue(kind="location", exact_literal="Paris", label="Paris"),
            status=FactStatus.ACTIVE,
            supersedes_fact_ids=("fact-city-old",),
        ),
    )
    _insert_record(
        database,
        Project(
            **_base("model-1", "project-varyz"),
            canonical_key="project:varyz",
            display_name="Varyz",
            participant_person_ids=("person-egor",),
            purpose="Build a personal model",
        ),
    )
    _insert_record(
        database,
        Goal(
            **_base("model-1", "goal-query"),
            canonical_key="goal:varyz:query",
            title="Make life knowledge searchable",
            kind=GoalKind.GOAL,
            status=GoalStatus.ACTIVE,
            project_ids=("project-varyz",),
        ),
    )
    _insert_record(
        database,
        Event(
            **_base("model-1", "event-dinner"),
            canonical_key="event:dinner",
            title="Dinner at Koya",
            event_kind="shared_meal",
            participant_ids=("person-egor", "person-anna"),
            temporal=TemporalScope(
                observed_at=NOW, time_precision=TimePrecision.EXACT
            ),
        ),
    )
    _insert_record(
        database,
        RelationshipSnapshot(
            **_base("model-1", "relationship-anna"),
            archive_owner_person_id="person-egor",
            other_person_id="person-anna",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=NOW,
            measurements=(RelationshipMeasurement(key="shared_events", count=1),),
            shared_event_ids=("event-dinner",),
        ),
    )
    _insert_record(
        database,
        Observation(
            **_base("model-1", "commitment-flowers"),
            episode_id="episode-commitment",
            kind=ObservationKind.COMMITMENT,
            proposition="Bring flowers to Anna on Friday",
            spans=(
                TextSpan(
                    entry_id="entry-commitment",
                    start=0,
                    end=13,
                    exact_literal="Bring flowers",
                ),
            ),
            speaker_person_id="person-egor",
            speaker_attribution=AttributionState.RESOLVED,
            subject_kind=EntityKind.PERSON,
            subject_id="person-anna",
            subject_attribution=AttributionState.RESOLVED,
            addressee_person_ids=("person-anna",),
        ),
    )
    _insert_record(
        database,
        Interpretation(
            **_base("model-1", "interpretation-care", readiness=Readiness.LIMITED),
            concept_key="mutual_care",
            thesis="Care is often expressed through practical help.",
            kind=InterpretationKind.RELATIONSHIP,
            scope=InterpretationScope(
                kind=ScopeKind.RELATIONSHIP,
                relationship_person_ids=("person-egor", "person-anna"),
            ),
            evidence_assignments=(
                InterpretationEvidence(
                    episode_id="episode-1",
                    observation_ids=("observation-1",),
                    role=EvidenceRole.SUPPORTS,
                    independence_group_id="decision-1",
                ),
            ),
            alternatives=(),
            counterevidence_summary="Not enough counterevidence yet.",
            countersearch_state=CountersearchState.NOT_STARTED,
            derivation_summary="One supported relationship-scoped pattern.",
            candidate_id="candidate-care",
        ),
    )
    database.execute(
        """INSERT INTO personal_model_action_handoffs
           (generation_id,candidate_id,title,node_kind,target_layer,
            observation_ids_json,claim_ids_json,source_entry_ids_json,
            project_ids_json,parent_goal_ids_json)
           VALUES ('model-1','action-review','Review the life lookup','action',
                   'observation_commitment_action','[]','["claim-action"]','[]',
                   '["project-varyz"]','["goal-query"]')"""
    )
    _activate(database, "model-1")
    return database


def test_same_name_is_ambiguous_and_never_merged(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    _insert_record(conn, _person("model-1", "person-alex-1", "Alex"))
    _insert_record(conn, _person("model-1", "person-alex-2", "Alex"))
    _activate(conn, "model-1")
    result = LifeModelQueryService(conn).query(
        LifeQuery(entities=(EntitySelector(value="Alex"),), relation=LifeRelation.PERSON)
    )
    assert result.disposition == QueryDisposition.AMBIGUOUS
    assert {item.entity_id for item in result.ambiguity_choices} == {
        "person-alex-1",
        "person-alex-2",
    }


def test_exact_source_alias_resolves_before_normalized_name(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).query(
        LifeQuery(entities=(EntitySelector(value="@anna"),), relation=LifeRelation.PERSON)
    )
    assert result.disposition == QueryDisposition.ANSWERED
    assert result.resolved_entities[0].entity_id == "person-anna"
    assert result.resolved_entities[0].resolution_tier == 1


def test_what_does_anna_like_returns_normalized_preferences(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).ask_text("What does Anna like?")
    assert result.disposition == QueryDisposition.ANSWERED
    assert [item.normalized_value for item in result.answers] == ["peonies", "sushi"]
    assert all(item.basis == "user_confirmed" for item in result.answers)
    assert all(item.provenance.generation_id == "model-1" for item in result.answers)


def test_scoped_owner_preferences_keep_direction_and_scope_in_agent_result(
    conn: sqlite3.Connection,
) -> None:
    _insert_generation(conn, "model-2")
    _insert_record(conn, _person("model-2", "person-egor", "Egor", owner=True))
    _insert_record(
        conn,
        Preference(
            **_base("model-2", "pref-no-images"),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-egor"),
            predicate="preference.visual",
            object=TextFactValue(
                kind="text", exact_literal="random background images",
                normalized="random background images",
            ),
            valence=PreferenceValence.AVOID,
            scope_kind="project",
            scope_label="marketing",
            assertion_kind="project_directive",
        ),
    )
    _activate(conn, "model-2")
    result = LifeModelQueryService(conn).ask_text("What are my preferences for marketing?")
    assert result.disposition == QueryDisposition.ANSWERED
    assert len(result.answers) == 1
    answer = result.answers[0]
    assert answer.preference_valence == PreferenceValence.AVOID
    assert answer.scope_kind == "project"
    assert answer.scope_label == "marketing"
    assert answer.assertion_kind == "project_directive"
    assert result.model_dump(mode="json")["answers"][0]["preference_valence"] == "avoid"
    assert LifeModelQueryService(conn).ask_text(
        "What are my design preferences?"
    ).answers[0].record_kind == "preference"
    assert LifeModelQueryService(conn).ask_text(
        "What are my workflow preferences?"
    ).disposition == QueryDisposition.NOT_KNOWN
    assert LifeModelQueryService(conn).ask_text(
        "What do I prefer for SAN7?"
    ).disposition == QueryDisposition.NOT_KNOWN


def test_project_action_handoffs_are_bounded_typed_active_generation_reads(
    conn: sqlite3.Connection,
) -> None:
    result = LifeModelQueryService(conn).ask_text("What are the actions for Varyz?")
    assert result.disposition == QueryDisposition.ANSWERED
    assert result.generation_id == "model-1"
    assert len(result.answers) == 1
    answer = result.answers[0]
    assert answer.relation == LifeRelation.ACTION_HANDOFF
    assert answer.record_kind == "action_handoff"
    assert answer.display_value == "Review the life lookup"
    assert answer.value_kind == "action"
    assert answer.subject_entity_ids == ("project-varyz", "goal-query")
    assert answer.provenance.record_id == "action-review"
    assert answer.provenance.claim_ids == ("claim-action",)

    limited = LifeModelQueryService(conn).query(
        LifeQuery(relation=LifeRelation.ACTION_HANDOFF, limit=1)
    )
    assert len(limited.answers) == 1


def test_action_handoff_sensitivity_is_derived_and_fail_closed(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    conn.execute(
        "UPDATE personal_model_records SET sensitivity='sensitive' "
        "WHERE generation_id='model-1' AND record_id='project-varyz'"
    )
    _activate(conn, "model-1")
    query = LifeQuery(
        entities=(EntitySelector(value="project-varyz", kind="project"),),
        relation=LifeRelation.ACTION_HANDOFF,
    )
    withheld = LifeModelQueryService(conn).query(query)
    assert withheld.disposition == QueryDisposition.NOT_KNOWN
    assert withheld.sensitive_records_withheld
    capability = issue_trusted_local_sensitivity_capability(TrustedLocalSurface.CLI)
    allowed = LifeModelQueryService(conn).query(
        query, sensitivity_capability=capability
    )
    assert allowed.answers[0].display_value == "Review the life lookup"
    assert allowed.answers[0].sensitivity == "sensitive"


def test_birthday_is_a_typed_month_day(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).ask_text("When is Anna's birthday?")
    assert result.answers[0].value_kind == "month_day"
    assert result.answers[0].display_value == "05-14"


def test_birthday_alias_normalizes_to_the_durable_predicate() -> None:
    query = LifeQuery(
        entities=(EntitySelector(value="Anna"),),
        relation=LifeRelation.FACT,
        predicate="birthday",
    )
    assert query.predicate == "person.birth_date"
    event_query = LifeQuery(relation=LifeRelation.EVENT, predicate="birthday")
    assert event_query.predicate == "birthday"


def test_real_memory_compiler_birthday_is_queryable_after_staging(
    conn: sqlite3.Connection,
) -> None:
    text = "Anna's birthday is 14 May 1994."
    observation = Observation(
        id="observation-memory-birthday",
        generation_id="observations-memory",
        claim_ids=("source-claim-memory-birthday",),
        temporal=TemporalScope(stated_at=NOW, time_precision=TimePrecision.EXACT),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.OBSERVED,
        producer=PRODUCER,
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id="episode-memory-birthday",
        kind=ObservationKind.STATEMENT,
        proposition=text,
        spans=(
            TextSpan(
                entry_id="entry-memory-birthday",
                start=0,
                end=len(text),
                exact_literal=text,
                source_revision="revision-1",
            ),
        ),
        speaker_person_id="person-anna",
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PERSON,
        subject_id="person-anna",
        subject_attribution=AttributionState.RESOLVED,
        dialogue_context=DialogueContext.VOLUNTEERED_REASONING,
        modality=ObservationModality.ASSERTED,
        polarity=Polarity.POSITIVE,
    )
    compilation = MemoryCompiler().compile(
        generation_id="model-memory",
        observations=(observation,),
        candidates=(
            FactCandidate(
                kind="fact",
                subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
                predicate="person.birth_date",
                value_kind=FactValueKind.DATE,
                exact_value_literal="14 May 1994",
                observation_ids=(observation.id,),
                asserted_by_person_id="person-anna",
                locale="en-GB",
                timezone="Europe/Madrid",
            ),
        ),
        created_at=NOW,
    )
    assert not compilation.quarantines
    compiled_fact = compilation.facts[0].model_copy(update={"readiness": Readiness.READY})

    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='superseded' WHERE id='model-1'")
    _insert_generation(conn, "model-memory")
    _insert_record(conn, _person("model-memory", "person-anna", "Anna"))
    _insert_record(conn, compiled_fact)
    _activate(conn, "model-memory")

    result = LifeModelQueryService(conn).ask_text("When is Anna's birthday?")
    assert result.disposition == QueryDisposition.ANSWERED
    assert result.answers[0].predicate_or_concept == "person.birth_date"
    assert result.answers[0].display_value == "1994-05-14"


def test_shared_event_requires_both_memberships(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).query(
        LifeQuery(
            entities=(EntitySelector(value="Egor"), EntitySelector(value="Anna")),
            relation=LifeRelation.EVENT,
        )
    )
    assert [item.display_value for item in result.answers] == ["Dinner at Koya"]
    assert set(result.answers[0].subject_entity_ids) == {"person-egor", "person-anna"}
    assert result.answers[0].observed_at == NOW


def test_person_project_query_includes_project_and_its_current_goal(
    conn: sqlite3.Connection,
) -> None:
    result = LifeModelQueryService(conn).ask_text("What is Egor working on?")
    assert {item.record_kind for item in result.answers} == {"project", "goal"}
    assert {item.display_value for item in result.answers} == {
        "Varyz",
        "Make life knowledge searchable",
    }


def test_commitment_lookup_returns_the_stored_proposition(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).ask_text(
        "What commitments do I have to Anna?"
    )
    assert [item.display_value for item in result.answers] == [
        "Bring flowers to Anna on Friday"
    ]
    assert result.answers[0].predicate_or_concept == "commitment"


def test_conflicting_and_outdated_facts_remain_visible(conn: sqlite3.Connection) -> None:
    result = LifeModelQueryService(conn).query(
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.FACT,
            predicate="person.current_location",
        )
    )
    assert {item.display_value for item in result.answers} == {"London", "Paris"}
    old = next(item for item in result.answers if item.display_value == "London")
    assert old.status == "contradicted"
    assert old.possibly_outdated
    assert old.conflict_record_ids == ("fact-city-new",)
    current_only = LifeModelQueryService(conn).query(
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.FACT,
            predicate="person.current_location",
            statuses=(QueryStatus.ACTIVE,),
        )
    )
    assert [item.display_value for item in current_only.answers] == ["Paris"]


def test_existing_interpretation_is_returned_but_parser_cannot_synthesize(
    conn: sqlite3.Connection,
) -> None:
    result = LifeModelQueryService(conn).ask_text("What do you understand about Anna?")
    assert result.answers[0].display_value == "Care is often expressed through practical help."
    unsupported = LifeModelQueryService(conn).ask_text("Why is Anna secretly afraid of me?")
    assert unsupported.disposition == QueryDisposition.CLARIFICATION
    assert not unsupported.answers


class _Fallback:
    def search(self, query, *, resolved_entities, limit):
        return [
            UncompiledEvidenceCandidate(
                entry_id="entry-raw",
                excerpt="Anna mentioned ramen once.",
                source="telegram",
                score=0.8,
                sensitivity=Sensitivity.PERSONAL,
            )
        ]


class _UnclassifiedFallback:
    def search(self, query, *, resolved_entities, limit):
        return [
            UncompiledEvidenceCandidate(
                entry_id="entry-unclassified",
                excerpt="Unreviewed source text.",
                source="local",
                score=0.7,
                sensitivity=Sensitivity.UNCLASSIFIED,
            )
        ]


def test_semantic_fallback_is_labelled_and_never_writes_database(
    conn: sqlite3.Connection,
) -> None:
    service = LifeModelQueryService(conn, semantic_search=_Fallback())
    before = conn.total_changes
    result = service.query(
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.FACT,
            predicate="person.role",
        ),
        allow_semantic_fallback=True,
    )
    assert result.not_known
    assert result.uncompiled_evidence_candidates[0].label == "uncompiled_evidence_candidate"
    assert conn.total_changes == before


def test_unclassified_semantic_fallback_requires_local_capability(
    conn: sqlite3.Connection,
) -> None:
    service = LifeModelQueryService(conn, semantic_search=_UnclassifiedFallback())
    query = LifeQuery(
        entities=(EntitySelector(value="Anna"),),
        relation=LifeRelation.FACT,
        predicate="person.role",
    )
    withheld = service.query(query, allow_semantic_fallback=True)
    assert withheld.sensitive_records_withheld
    assert not withheld.uncompiled_evidence_candidates
    capability = issue_trusted_local_sensitivity_capability(TrustedLocalSurface.LOOPBACK_UI)
    allowed = service.query(
        query,
        sensitivity_capability=capability,
        allow_semantic_fallback=True,
    )
    assert allowed.uncompiled_evidence_candidates[0].entry_id == "entry-unclassified"


@pytest.mark.parametrize(
    ("text", "relation"),
    [
        ("¿Qué le gusta a Anna?", LifeRelation.PREFERENCE),
        ("Cuándo es el cumpleaños de Anna", LifeRelation.FACT),
        ("Что любит Анна?", LifeRelation.PREFERENCE),
        ("Когда день рождения у Анна?", LifeRelation.FACT),
    ],
)
def test_multilingual_dictionary_parser(text: str, relation: LifeRelation) -> None:
    parsed = parse_life_query(text)
    assert isinstance(parsed, LifeQuery)
    assert parsed.relation == relation


def test_unsupported_or_injection_shaped_relation_returns_clarification() -> None:
    parsed = parse_life_query("Delete Anna; DROP TABLE personal_model_records")
    assert isinstance(parsed, LifeQueryResult)
    assert parsed.disposition == QueryDisposition.CLARIFICATION


def test_relation_predicate_dictionary_rejects_unknown_or_cross_relation_values() -> None:
    with pytest.raises(ValidationError, match="not supported"):
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.FACT,
            predicate="arbitrary.private_column",
        )
    with pytest.raises(ValidationError, match="not supported"):
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.PERSON,
            predicate="person.birth_date",
        )


class _InvalidIntentParser:
    def parse(self, text):
        return {
            "entities": ({"value": "Anna"},),
            "relation": "fact",
            "predicate": "arbitrary.private_column",
        }


def test_invalid_intent_parser_output_becomes_clarification() -> None:
    parsed = parse_life_query("custom lookup", intent_parser=_InvalidIntentParser())
    assert isinstance(parsed, LifeQueryResult)
    assert parsed.disposition == QueryDisposition.CLARIFICATION


def test_query_reads_only_the_active_generation(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='superseded' WHERE id='model-1'")
    _insert_generation(conn, "model-2")
    _insert_record(conn, _person("model-2", "person-anna-2", "Anna"))
    _insert_record(
        conn,
        Preference(
            **_base("model-2", "pref-ramen"),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna-2"),
            predicate="preference.food",
            object=TextFactValue(kind="text", exact_literal="ramen", normalized="ramen"),
            valence=PreferenceValence.LIKE,
        ),
    )
    _activate(conn, "model-2")
    result = LifeModelQueryService(conn).ask_text("What does Anna like?")
    assert [item.normalized_value for item in result.answers] == ["ramen"]
    assert result.generation_id == "model-2"


def test_relationship_and_event_are_indexed_for_every_person(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT record_id, entity_id FROM personal_model_entity_memberships
           WHERE generation_id='model-1'
             AND record_id IN ('event-dinner','relationship-anna')
           ORDER BY record_id, entity_id"""
    ).fetchall()
    assert {(row[0], row[1]) for row in rows} >= {
        ("event-dinner", "person-egor"),
        ("event-dinner", "person-anna"),
        ("relationship-anna", "person-egor"),
        ("relationship-anna", "person-anna"),
    }


def test_membership_and_alias_query_plans_use_indexes(conn: sqlite3.Connection) -> None:
    membership = conn.execute(
        """EXPLAIN QUERY PLAN SELECT record_id
           FROM personal_model_entity_memberships
           WHERE generation_id=? AND entity_kind=? AND entity_id=?""",
        ("model-1", "person", "person-anna"),
    ).fetchall()
    alias = conn.execute(
        """EXPLAIN QUERY PLAN SELECT entity_id FROM personal_model_entity_aliases
           WHERE generation_id=? AND normalized_value=?""",
        ("model-1", "anna"),
    ).fetchall()
    assert any("idx_personal_model_membership_lookup" in row[3] for row in membership)
    assert any("idx_personal_model_alias_normalized" in row[3] for row in alias)


def test_telemetry_contains_shape_not_question_name_or_result(
    conn: sqlite3.Connection,
) -> None:
    captured = []
    result = LifeModelQueryService(conn, telemetry_sink=captured.append).ask_text(
        "What does Anna like?"
    )
    assert result.answers
    encoded = captured[0].model_dump_json()
    assert "Anna" not in encoded
    assert "peonies" not in encoded
    assert "sushi" not in encoded
    assert "preference" in encoded


def test_sensitive_records_require_an_explicit_capability(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    secret = Fact(
        **{
            **_base("model-1", "fact-secret"),
            "sensitivity": Sensitivity.SENSITIVE,
        },
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
        predicate="person.constraint",
        value=TextFactValue(kind="text", exact_literal="private", normalized="private"),
    )
    _insert_record(conn, secret)
    unclassified = Fact(
        **{
            **_base("model-1", "fact-unclassified"),
            "sensitivity": Sensitivity.UNCLASSIFIED,
        },
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
        predicate="person.role",
        value=TextFactValue(kind="text", exact_literal="Editor", normalized="editor"),
    )
    _insert_record(conn, unclassified)
    hidden_person = _person("model-1", "person-hidden", "Hidden Person").model_copy(
        update={"sensitivity": Sensitivity.UNCLASSIFIED}
    )
    _insert_record(conn, hidden_person)
    _activate(conn, "model-1")
    query = LifeQuery(
        entities=(EntitySelector(value="Anna"),),
        relation=LifeRelation.FACT,
        predicate="person.constraint",
    )
    withheld = LifeModelQueryService(conn).query(query)
    assert withheld.not_known and withheld.sensitive_records_withheld
    unclassified_query = LifeQuery(
        entities=(EntitySelector(value="Anna"),),
        relation=LifeRelation.FACT,
        predicate="person.role",
    )
    unclassified_withheld = LifeModelQueryService(conn).query(unclassified_query)
    assert unclassified_withheld.not_known
    assert unclassified_withheld.sensitive_records_withheld
    hidden_query = LifeQuery(
        entities=(EntitySelector(value="Hidden Person"),),
        relation=LifeRelation.PERSON,
    )
    hidden_withheld = LifeModelQueryService(conn).query(hidden_query)
    assert hidden_withheld.not_known
    assert not hidden_withheld.resolved_entities
    assert hidden_withheld.sensitive_records_withheld
    with pytest.raises(TypeError, match="trusted local capability"):
        LifeModelQueryService(conn).query(
            query,
            sensitivity_capability=True,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="trusted local surfaces"):
        issue_trusted_local_sensitivity_capability("mcp")  # type: ignore[arg-type]
    capability = issue_trusted_local_sensitivity_capability(TrustedLocalSurface.CLI)
    allowed = LifeModelQueryService(conn).query(
        query, sensitivity_capability=capability
    )
    assert allowed.answers[0].display_value == "private"
    unclassified_allowed = LifeModelQueryService(conn).query(
        unclassified_query, sensitivity_capability=capability
    )
    assert unclassified_allowed.answers[0].display_value == "editor"
    hidden_allowed = LifeModelQueryService(conn).query(
        hidden_query, sensitivity_capability=capability
    )
    assert hidden_allowed.answers[0].display_value == "Hidden Person"


def test_migration_is_forward_only_and_idempotent(conn: sqlite3.Connection) -> None:
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == list(range(1, max(versions) + 1))
    assert max(versions) >= 21
    assert versions == sorted(set(versions))


def test_migration_backfills_an_already_active_generation(tmp_path: Path) -> None:
    database = connect(tmp_path / "pre-query.db")
    database.execute(
        """CREATE TABLE schema_migrations
           (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
    )
    for version, _name, sql in _iter_migrations():
        if version >= 21:
            continue
        database.executescript(sql)
        database.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)",
            (version, NOW.isoformat()),
        )
    database.execute(
        """INSERT INTO graph_runs
           (id, generation_key, extractor_name, extractor_version, predicate_version,
            identity_version, input_content_hash, state, started_at, completed_at, activated_at)
           VALUES ('graph-1','key','test','1','1','1','hash','active',?,?,?)""",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    _insert_generation(database, "model-before-query")
    _insert_record(
        database,
        _person("model-before-query", "person-before-query", "Preexisting Person"),
    )
    _activate(database, "model-before-query")

    applied = migrate(database)
    assert applied[0] == "0021_personal_model_query.sql"
    assert applied == sorted(applied)
    membership = database.execute(
        """SELECT entity_id FROM personal_model_entity_memberships
           WHERE generation_id='model-before-query' AND role='self'"""
    ).fetchone()
    alias = database.execute(
        """SELECT entity_id FROM personal_model_entity_aliases
           WHERE generation_id='model-before-query' AND normalized_value='preexisting person'"""
    ).fetchone()
    assert membership[0] == "person-before-query"
    assert alias[0] == "person-before-query"
    assert migrate(database) == []


def test_activated_query_projections_are_immutable(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="memberships are immutable"):
        conn.execute(
            """DELETE FROM personal_model_entity_memberships
               WHERE generation_id='model-1' AND record_id='event-dinner'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="aliases are immutable"):
        conn.execute(
            """UPDATE personal_model_entity_aliases SET exact_value='tampered'
               WHERE generation_id='model-1' AND entity_id='person-anna'
                 AND alias_kind='display_name'"""
        )


def test_the_owner_can_ask_about_themselves_in_the_first_person(
    conn: sqlite3.Connection,
) -> None:
    """The question the tool exists for, which it could not previously parse.

    Nobody writes "me" in an alias table, so a first-person selector matched no
    row and every question an owner would actually ask -- what am I working on,
    where do I live -- came back as a request for clarification. The owner is
    known structurally, by the flag the compiler sets on their person record, so
    it is resolvable without inference.
    """
    service = LifeModelQueryService(conn)

    working = service.ask_text("What am I working on?")
    assert working.disposition == QueryDisposition.ANSWERED, working.clarification
    assert [item.entity_id for item in working.resolved_entities] == ["person-egor"]
    assert "Varyz" in [item.display_value for item in working.answers]
    assert service.ask_text("What are my goals?").disposition == QueryDisposition.ANSWERED

    # Third person still resolves by alias, and a stranger still resolves to
    # nobody: the owner clause widens no other match.
    assert service.ask_text("Where does Anna live?").answers[0].display_value == "Paris"
    assert service.ask_text("Where does Quernwood live?").disposition == (
        QueryDisposition.NOT_KNOWN
    )


def test_a_sensitive_owner_resolves_under_the_word_they_used_for_themselves(
    conn: sqlite3.Connection,
) -> None:
    """A person record takes the highest sensitivity of the observations under it.

    The owner appears in every private message they ever sent, so their own
    record is ``sensitive`` on any real archive -- it is on the live one.
    Dropping it the way a third party is dropped made the tool's own documented
    example, "What am I working on?", answer ``not_known`` for exactly the
    person the tool is for, and protected nothing: the caller is the owner
    asking about themselves. What the gate does buy is the name.
    """
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    conn.execute(
        "UPDATE personal_model_records SET sensitivity='sensitive' "
        "WHERE generation_id='model-1' AND record_id='person-egor'"
    )
    _activate(conn, "model-1")
    service = LifeModelQueryService(conn)
    result = service.ask_text("What am I working on?")

    assert result.disposition == QueryDisposition.ANSWERED, result.clarification
    assert [item.entity_id for item in result.resolved_entities] == ["person-egor"]
    assert "Varyz" in [item.display_value for item in result.answers]
    # The stored name stays behind; the entity comes back under the caller's own word.
    owner = result.resolved_entities[0]
    assert owner.display_name == owner.matched_alias != "Egor"
    # A name that stayed behind is not a record that was withheld. Reporting it
    # as one ended every first-person question with "add --include-sensitive to
    # read them here", and the flag added nothing to read: this answer never
    # printed the name. The entity that lost it says so instead.
    assert owner.display_name_withheld is True
    assert result.sensitive_records_withheld is False

    capability = issue_trusted_local_sensitivity_capability(TrustedLocalSurface.CLI)
    named = service.query(
        LifeQuery(
            entities=(EntitySelector(value="I"),), relation=LifeRelation.PROJECT_GOAL
        ),
        sensitivity_capability=capability,
    )
    assert [item.display_name for item in named.resolved_entities] == ["Egor"]
    assert named.resolved_entities[0].display_name_withheld is False
    # Nothing is held back once the capability is presented, so nothing is
    # reported as held back. The owner's own person record is sensitive on
    # every real archive, and reporting it unconditionally made every
    # first-person question end in "records were withheld as sensitive; add
    # --include-sensitive" -- advice that changed nothing when followed.
    assert named.sensitive_records_withheld is False


def test_a_project_nobody_is_named_on_answers_for_the_owner(
    conn: sqlite3.Connection,
) -> None:
    """Participants are whatever the extraction proposed, which is often nobody.

    On generation ``01M1Z23M5463KQY4T0BZ04QBRH`` none of the 312 project and
    goal records carry a person membership, so a first-person question matched
    by membership matched nothing even once the owner resolved. The Mirror
    lists those same 312 under the user's own goals, so the two surfaces
    disagreed about one question. A record that does name somebody else still
    answers only for them.
    """
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    _insert_record(
        conn,
        Project(
            **_base("model-1", "project-unnamed"),
            canonical_key="project:unnamed",
            display_name="Nobody is named on this",
            purpose="A project the extraction listed no participants for",
        ),
    )
    _insert_record(
        conn,
        Project(
            **_base("model-1", "project-anna"),
            canonical_key="project:anna",
            display_name="A project of Anna's",
            participant_person_ids=("person-anna",),
            purpose="A project that names somebody else",
        ),
    )
    _activate(conn, "model-1")
    service = LifeModelQueryService(conn)
    mine = [item.display_value for item in service.ask_text("What am I working on?").answers]

    assert "Nobody is named on this" in mine
    assert "Varyz" in mine
    assert "A project of Anna's" not in mine
    hers = [
        item.display_value
        for item in service.ask_text("What is Anna working on?").answers
    ]
    assert "A project of Anna's" in hers
    assert "Nobody is named on this" not in hers


def test_a_project_named_only_by_an_account_is_not_an_answer(
    conn: sqlite3.Connection,
) -> None:
    """A generation compiled before the naming guard is full of these.

    Forty-four of the 192 projects and seventeen of the 120 goals on generation
    ``01M1Z23M5463KQY4T0BZ04QBRH`` are named by the account they were read out
    of, and with ``--include-sensitive`` they filled the answer to "what am I
    working on?" and pushed Varyz, Adastra and SAN7 past the limit. The
    compiler no longer emits them; this is what an archive that already holds
    them answers, since recompiling one is hours of work.
    """
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    for record_id, name in (
        ("project-jid", "34900000001@s.whatsapp.net"),
        ("project-phone", "34900000002"),
        ("project-relationship", "Relationship with 34900000003"),
        ("project-placeholder", "No specific name 5"),
        ("project-real", "Adastra"),
    ):
        _insert_record(
            conn,
            Project(
                **_base("model-1", record_id),
                canonical_key=f"project:{record_id}",
                display_name=name,
                purpose="Something the archive read out of a chat",
            ),
        )
    _activate(conn, "model-1")

    mine = [
        item.display_value
        for item in LifeModelQueryService(conn).ask_text("What am I working on?").answers
    ]

    assert "Adastra" in mine
    assert "Varyz" in mine
    assert not [item for item in mine if "34" in item or item.startswith("No specific")]


def test_a_person_and_a_relationship_are_not_what_the_owner_is_working_on(
    conn: sqlite3.Connection,
) -> None:
    """"What am I working on?" answered `Aarni Koskela`, `Relationship with Nuullll`.

    The compiler proposes a project wherever a conversation keeps returning to
    one subject, and a subject is often a person: 56 of the 312 projects and
    goals on generation ``01M1Z23M5463KQY4T0BZ04QBRH`` are also people on it,
    and seventeen more are a relationship. The Mirror refuses both; this is the
    same two rules on the tool an agent asks.
    """
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    _insert_record(conn, _person("model-1", "person-aarni", "Aarni Koskela"))
    for record_id, name in (
        ("project-person", "Aarni Koskela"),
        ("project-person-case", "aarni koskela"),
        ("project-relationship", "Relationship with Nuullll"),
        ("project-relationship-snake", "relationship_with_nuullll"),
        ("project-real", "Adastra"),
    ):
        _insert_record(
            conn,
            Project(
                **_base("model-1", record_id),
                canonical_key=f"project:{record_id}",
                display_name=name,
                purpose="Something the archive read out of a chat",
            ),
        )
    _activate(conn, "model-1")

    mine = [
        item.display_value
        for item in LifeModelQueryService(conn).ask_text("What am I working on?").answers
    ]

    assert "Adastra" in mine
    assert "Varyz" in mine
    assert not [
        item
        for item in mine
        if item.casefold().startswith(("aarni", "relationship"))
    ]


def _supported(record_id: str, display_name: str, entries: int) -> Project:
    """A project standing on `entries` distinct entries of the archive."""
    return Project(
        **_base("model-1", record_id),
        canonical_key=f"project:{record_id}",
        display_name=display_name,
        participant_person_ids=("person-egor",),
        purpose="Something the owner works on",
        evidence=tuple(
            EvidenceReference(entry_id=f"{record_id}-entry-{index}", role=EvidenceRole.SUPPORTS)
            for index in range(entries)
        ),
    )


def _two_projects(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")
    # The ids are chosen so that sorting by record id and sorting by support
    # disagree, which is the whole question.
    _insert_record(conn, _supported("project-aaa", "One passing mention", 1))
    _insert_record(conn, _supported("project-zzz", "Years of work", 12))
    _activate(conn, "model-1")


def test_the_answers_lead_with_what_the_archive_stands_behind(
    conn: sqlite3.Connection,
) -> None:
    """Confidence grades whether a record is real, not how much it accounts for.

    On generation ``01M1Z23M5463KQY4T0BZ04QBRH`` every goal scored 0.75 and
    every project 0.7, so ranking on confidence left the record id -- a sha256
    -- deciding which twenty of the projects a caller was shown.
    """
    _two_projects(conn)
    answers = [
        item.display_value
        for item in LifeModelQueryService(conn).ask_text("What am I working on?").answers
    ]

    assert answers.index("Years of work") < answers.index("One passing mention")


def test_the_candidate_window_keeps_the_best_supported_record(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window truncates before anything is ranked, so its order decides
    which records can be answered at all. Ordered by id, 112 of the 312 live
    projects and goals could never come back -- including the one carrying
    2,112 entries across 187 days."""
    _two_projects(conn)
    monkeypatch.setattr("varyz.personal_model.query.MAX_CANDIDATE_ROWS", 1)
    result = LifeModelQueryService(conn).ask_text("What am I working on?")

    assert [item.display_value for item in result.answers] == ["Years of work"]


def _restage(conn: sqlite3.Connection) -> None:
    """Put the generation back in staging, because activated records cannot be
    altered and both tests below are about altering one."""

    conn.execute("DELETE FROM personal_model_active_generation")
    conn.execute("UPDATE personal_model_generations SET state='staged' WHERE id='model-1'")


def test_a_staged_only_match_is_not_reported_as_the_archive_not_knowing(
    conn: sqlite3.Connection,
) -> None:
    """"Not compiled yet" and "not known" are different answers.

    Every observation the compiler has ever written is `staged`, and nothing in
    it promotes one -- 9,455 on the owner's own archive, 503 of them
    commitments. The commitment relation reads observations, so asking what the
    owner committed to returns nothing on every archive there is, and returned
    it as a bare `not_known`, which an agent passes on as "you have not
    committed to anything". The records are there. They are unfinished.
    """

    _restage(conn)
    conn.execute(
        "UPDATE personal_model_records SET readiness='staged' "
        "WHERE record_id='commitment-flowers'"
    )
    _activate(conn, "model-1")
    result = LifeModelQueryService(conn).ask_text("What are my commitments?")

    assert result.disposition == QueryDisposition.NOT_KNOWN
    assert result.staged_records_withheld is True


def test_nothing_matching_at_all_still_reports_a_plain_not_known(
    conn: sqlite3.Connection,
) -> None:
    """The flag says "compiled but unpromoted", so it must stay off otherwise."""

    _restage(conn)
    conn.execute("DELETE FROM personal_model_records WHERE record_kind='observation'")
    _activate(conn, "model-1")
    result = LifeModelQueryService(conn).ask_text("What are my commitments?")

    assert result.disposition == QueryDisposition.NOT_KNOWN
    assert result.staged_records_withheld is False
