from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.models import (
    AttributionState,
    DerivationKind,
    DialogueContext,
    EntityKind,
    Event,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValueKind,
    Observation,
    ObservationKind,
    ObservationModality,
    Person,
    Polarity,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    SubjectReference,
    TemporalScope,
    TextFactValue,
    TextSpan,
)
from varyz.personal_model.relationships import (
    ApprovedRelationshipRecord,
    CorrelationAssignment,
    ObservationRelationshipContext,
    RelationshipDiagnosticCode,
    RelationshipHistoryCompiler,
    RelationshipPeriod,
    RelationshipRecordKind,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
PERIOD = RelationshipPeriod(start=NOW - timedelta(days=30), end=NOW + timedelta(days=1))
OWNER_ID = "person-owner"
ANNA_ID = "person-anna"


def producer() -> ProducerMetadata:
    return ProducerMetadata(
        kind=ProducerKind.DETERMINISTIC,
        name="test",
        version="1",
        rule_version="1",
    )


def person(
    person_id: str,
    name: str,
    *,
    owner: bool = False,
) -> Person:
    return Person(
        id=person_id,
        generation_id="source-generation",
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.USER_CONFIRMED,
        producer=producer(),
        readiness=Readiness.STAGED,
        created_at=NOW,
        canonical_key=person_id,
        display_name=name,
        is_archive_owner=owner,
    )


def evidence(record_id: str, literal: str) -> EvidenceReference:
    return EvidenceReference(
        entry_id=f"entry-{record_id}",
        role=EvidenceRole.SUPPORTS,
        span=TextSpan(
            entry_id=f"entry-{record_id}",
            start=0,
            end=len(literal),
            exact_literal=literal,
        ),
        sensitivity=Sensitivity.PERSONAL,
    )


def observation(
    record_id: str,
    literal: str,
    *,
    speaker: str = OWNER_ID,
    subject: str = ANNA_ID,
    addressees: tuple[str, ...] = (ANNA_ID,),
    kind: ObservationKind = ObservationKind.STATEMENT,
    modality: ObservationModality = ObservationModality.ASSERTED,
    dialogue: DialogueContext = DialogueContext.VOLUNTEERED_REASONING,
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
) -> Observation:
    ref = evidence(record_id, literal).model_copy(update={"sensitivity": sensitivity})
    return Observation(
        id=record_id,
        generation_id="source-generation",
        evidence=(ref,),
        temporal=TemporalScope(observed_at=NOW, time_precision=TimePrecision.EXACT),
        confidence=1.0,
        sensitivity=sensitivity,
        derivation=DerivationKind.EXPLICIT,
        producer=producer(),
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id=f"episode-{record_id}",
        kind=kind,
        proposition=literal,
        spans=(ref.span,),
        speaker_person_id=speaker,
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PERSON,
        subject_id=subject,
        subject_attribution=AttributionState.RESOLVED,
        addressee_person_ids=addressees,
        dialogue_context=dialogue,
        polarity=Polarity.POSITIVE,
        modality=modality,
    )


def context(
    observation_id: str,
    *,
    other: str = ANNA_ID,
    conversation: str = "conversation-1",
    decision: str | None = None,
    correlation: str | None = None,
    role: bool = False,
    attribution: AttributionState = AttributionState.RESOLVED,
) -> ObservationRelationshipContext:
    return ObservationRelationshipContext(
        observation_id=observation_id,
        other_person_ids=(other,) if attribution == AttributionState.RESOLVED else (),
        participants_attribution=attribution,
        occurred_at=NOW,
        conversation_group_id=conversation,
        decision_group_id=decision,
        correlation_group_id=correlation,
        explicit_relationship_role_statement=role,
    )


def event(
    record_id: str = "event-dinner",
    *,
    other: str = ANNA_ID,
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
) -> Event:
    literal = "Dinner together"
    return Event(
        id=record_id,
        generation_id="source-generation",
        evidence=(evidence(record_id, literal),),
        temporal=TemporalScope(valid_from=NOW, time_precision=TimePrecision.EXACT),
        confidence=1.0,
        sensitivity=sensitivity,
        derivation=DerivationKind.EXPLICIT,
        producer=producer(),
        readiness=Readiness.STAGED,
        created_at=NOW,
        canonical_key=record_id,
        title=literal,
        event_kind="meal",
        participant_ids=(OWNER_ID, other),
    )


def preference(
    record_id: str = "preference-peonies",
    *,
    other: str = ANNA_ID,
    literal: str = "peonies",
) -> Preference:
    return Preference(
        id=record_id,
        generation_id="source-generation",
        evidence=(evidence(record_id, literal),),
        temporal=TemporalScope(observed_at=NOW, time_precision=TimePrecision.EXACT),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.EXPLICIT,
        producer=producer(),
        readiness=Readiness.STAGED,
        created_at=NOW,
        subject=SubjectReference(kind=EntityKind.PERSON, id=other),
        predicate="preference.flower",
        object=TextFactValue(kind=FactValueKind.TEXT, exact_literal=literal),
        valence=PreferenceValence.LIKE,
        asserted_by_person_id=other,
        status=FactStatus.ACTIVE,
    )


def fact(record_id: str = "fact-role", *, other: str = ANNA_ID) -> Fact:
    literal = "friend"
    return Fact(
        id=record_id,
        generation_id="source-generation",
        evidence=(evidence(record_id, literal),),
        temporal=TemporalScope(observed_at=NOW, time_precision=TimePrecision.EXACT),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.EXPLICIT,
        producer=producer(),
        readiness=Readiness.STAGED,
        created_at=NOW,
        subject=SubjectReference(kind=EntityKind.PERSON, id=other),
        predicate="relationship.role",
        value=TextFactValue(kind=FactValueKind.TEXT, exact_literal=literal),
        asserted_by_person_id=OWNER_ID,
    )


def approval(kind: RelationshipRecordKind, record_id: str, other: str = ANNA_ID):
    return ApprovedRelationshipRecord(
        record_kind=kind,
        record_id=record_id,
        other_person_ids=(other,),
    )


def compile_history(**updates):
    kwargs = {
        "generation_id": "generation-relationship",
        "archive_owner_person_id": OWNER_ID,
        "significant_person_ids": (ANNA_ID,),
        "periods": (PERIOD,),
        "people": (person(OWNER_ID, "Egor", owner=True), person(ANNA_ID, "Anna")),
        "created_at": NOW,
    }
    kwargs.update(updates)
    return RelationshipHistoryCompiler().compile(**kwargs)


def counts(result) -> dict[str, int]:
    return {item.key: item.count for item in result.snapshots[0].measurements}


def test_anna_shared_event_preference_fact_and_commitment_are_structured_history() -> None:
    shared = event()
    liked = preference()
    known = fact()
    promise = observation(
        "commitment-flowers",
        "I will bring peonies on Friday",
        kind=ObservationKind.COMMITMENT,
    )

    result = compile_history(
        observations=(promise,),
        events=(shared,),
        facts=(known,),
        preferences=(liked,),
        observation_contexts=(context(promise.id, decision="decision-flowers"),),
        approvals=(
            approval(RelationshipRecordKind.EVENT, shared.id),
            approval(RelationshipRecordKind.PREFERENCE, liked.id),
            approval(RelationshipRecordKind.FACT, known.id),
            approval(RelationshipRecordKind.OBSERVATION, promise.id),
        ),
    )

    snapshot = result.snapshots[0]
    history = result.histories[0]
    assert snapshot.shared_event_ids == (shared.id,)
    assert snapshot.commitment_observation_ids == (promise.id,)
    assert history.preference_ids == (liked.id,)
    assert history.fact_ids == (known.id,)
    assert counts(result)["independent.commitments"] == 1
    assert {(item.record_kind, item.record_id) for item in result.dependencies} == {
        (RelationshipRecordKind.EVENT, shared.id),
        (RelationshipRecordKind.PREFERENCE, liked.id),
        (RelationshipRecordKind.FACT, known.id),
        (RelationshipRecordKind.OBSERVATION, promise.id),
    }


def test_twenty_turns_about_one_decision_count_once() -> None:
    items = tuple(
        observation(
            f"decision-turn-{index}",
            f"Decision detail {index}",
            speaker=OWNER_ID if index % 2 == 0 else ANNA_ID,
            addressees=(ANNA_ID,) if index % 2 == 0 else (OWNER_ID,),
            kind=ObservationKind.CHOICE,
        )
        for index in range(20)
    )
    result = compile_history(
        observations=items,
        observation_contexts=tuple(
            context(item.id, decision="one-underlying-decision") for item in items
        ),
        approvals=tuple(
            approval(RelationshipRecordKind.OBSERVATION, item.id) for item in items
        ),
    )

    measured = counts(result)
    assert measured["independent.decision_groups"] == 1
    assert measured["diagnostic.owner_authored_turns"] == 10
    assert measured["diagnostic.other_authored_turns"] == 10
    assert measured["mutual.conversation_groups"] == 1


def test_overlapping_episode_observations_share_supplied_correlation() -> None:
    first = observation("overlap-a", "We chose Friday", kind=ObservationKind.CHOICE)
    second = observation("overlap-b", "Friday is the choice", kind=ObservationKind.CHOICE)
    result = compile_history(
        observations=(first, second),
        observation_contexts=(
            context(first.id, decision="decision-one", correlation="episode-overlap"),
            context(second.id, decision="decision-one", correlation="episode-overlap"),
        ),
        approvals=(
            approval(RelationshipRecordKind.OBSERVATION, first.id),
            approval(RelationshipRecordKind.OBSERVATION, second.id),
        ),
    )

    assert counts(result)["independent.decision_groups"] == 1


def test_quoted_third_party_turn_is_not_attributed_or_counted_as_commitment() -> None:
    direct = observation("direct-turn", "Anna and I planned dinner")
    quoted = observation(
        "quoted-third-party",
        "Maya said she promises to help",
        speaker="person-maya",
        kind=ObservationKind.COMMITMENT,
        modality=ObservationModality.QUOTED,
        dialogue=DialogueContext.QUOTATION,
    )
    result = compile_history(
        people=(
            person(OWNER_ID, "Egor", owner=True),
            person(ANNA_ID, "Anna"),
            person("person-maya", "Maya"),
        ),
        observations=(direct, quoted),
        observation_contexts=(
            context(direct.id),
            context(quoted.id, decision="quoted-promise"),
        ),
        approvals=(
            approval(RelationshipRecordKind.OBSERVATION, direct.id),
            approval(RelationshipRecordKind.OBSERVATION, quoted.id),
        ),
    )

    assert result.snapshots[0].commitment_observation_ids == ()
    measured = counts(result)
    assert measured["independent.commitments"] == 0
    assert measured["diagnostic.owner_authored_turns"] == 1
    assert measured["diagnostic.other_authored_turns"] == 0
    assert any(
        item.code == RelationshipDiagnosticCode.UNRELATED_RECORD
        and item.record_ids == (quoted.id,)
        for item in result.quarantines
    )


def test_same_display_name_people_are_never_combined() -> None:
    second_anna = "person-anna-two"
    first_preference = preference("anna-one-sushi", literal="sushi")
    second_preference = preference(
        "anna-two-pizza", other=second_anna, literal="pizza"
    )
    result = compile_history(
        significant_person_ids=(ANNA_ID, second_anna),
        people=(
            person(OWNER_ID, "Egor", owner=True),
            person(ANNA_ID, "Anna"),
            person(second_anna, "Anna"),
        ),
        preferences=(first_preference, second_preference),
        approvals=(
            approval(RelationshipRecordKind.PREFERENCE, first_preference.id, ANNA_ID),
            approval(RelationshipRecordKind.PREFERENCE, second_preference.id, second_anna),
        ),
    )

    histories = {item.other_person_id: item.preference_ids for item in result.histories}
    assert histories == {
        ANNA_ID: (first_preference.id,),
        second_anna: (second_preference.id,),
    }


def test_changing_period_produces_a_new_snapshot_identity() -> None:
    shared = event()
    common = {
        "events": (shared,),
        "approvals": (approval(RelationshipRecordKind.EVENT, shared.id),),
    }
    first = compile_history(**common)
    shifted = compile_history(
        **common,
        periods=(
            RelationshipPeriod(
                start=PERIOD.start - timedelta(days=1), end=PERIOD.end
            ),
        ),
    )

    assert first.snapshots[0].id != shifted.snapshots[0].id


def test_self_relationship_is_rejected() -> None:
    result = compile_history(significant_person_ids=(OWNER_ID,))

    assert result.snapshots == ()
    assert result.quarantines[0].code == RelationshipDiagnosticCode.INVALID_PERSON


def test_missing_or_non_owner_person_blocks_compilation() -> None:
    result = compile_history(
        people=(person(OWNER_ID, "Egor", owner=False), person(ANNA_ID, "Anna")),
    )

    assert result.snapshots == ()
    assert result.quarantines[0].code == RelationshipDiagnosticCode.MISSING_OWNER


def test_deleted_source_evidence_excludes_the_dependent_record() -> None:
    shared = event()
    result = compile_history(
        events=(shared,),
        approvals=(approval(RelationshipRecordKind.EVENT, shared.id),),
        deleted_entry_ids=(f"entry-{shared.id}",),
    )

    assert result.snapshots == ()
    assert any(
        item.code == RelationshipDiagnosticCode.DELETED_EVIDENCE
        for item in result.quarantines
    )


def test_input_order_does_not_change_compilation() -> None:
    shared = event()
    liked = preference()
    promise = observation("promise", "I promise to call", kind=ObservationKind.COMMITMENT)
    kwargs = {
        "observations": (promise,),
        "events": (shared,),
        "preferences": (liked,),
        "observation_contexts": (context(promise.id, decision="call"),),
        "approvals": (
            approval(RelationshipRecordKind.OBSERVATION, promise.id),
            approval(RelationshipRecordKind.EVENT, shared.id),
            approval(RelationshipRecordKind.PREFERENCE, liked.id),
        ),
    }
    forward = compile_history(**kwargs)
    reverse = compile_history(
        observations=tuple(reversed(kwargs["observations"])),
        events=tuple(reversed(kwargs["events"])),
        preferences=tuple(reversed(kwargs["preferences"])),
        observation_contexts=tuple(reversed(kwargs["observation_contexts"])),
        approvals=tuple(reversed(kwargs["approvals"])),
    )

    assert forward == reverse


def test_snapshot_uses_most_restrictive_source_sensitivity() -> None:
    ordinary = preference()
    restricted = event(sensitivity=Sensitivity.PERSONAL)
    restricted = restricted.model_copy(
        update={
            "evidence": tuple(
                reference.model_copy(update={"sensitivity": Sensitivity.UNCLASSIFIED})
                for reference in restricted.evidence
            )
        }
    )
    result = compile_history(
        events=(restricted,),
        preferences=(ordinary,),
        approvals=(
            approval(RelationshipRecordKind.EVENT, restricted.id),
            approval(RelationshipRecordKind.PREFERENCE, ordinary.id),
        ),
    )

    assert result.snapshots[0].sensitivity == Sensitivity.UNCLASSIFIED
    assert not result.snapshots[0].sensitivity.cloud_ok


def test_repeated_shared_event_records_use_one_correlation_group() -> None:
    original = event("event-original")
    cross_post = event("event-cross-post")
    result = compile_history(
        events=(original, cross_post),
        approvals=(
            approval(RelationshipRecordKind.EVENT, original.id),
            approval(RelationshipRecordKind.EVENT, cross_post.id),
        ),
        correlations=(
            CorrelationAssignment(
                record_kind=RelationshipRecordKind.EVENT,
                record_id=original.id,
                group_id="one-dinner",
            ),
            CorrelationAssignment(
                record_kind=RelationshipRecordKind.EVENT,
                record_id=cross_post.id,
                group_id="one-dinner",
            ),
        ),
    )

    assert result.snapshots[0].shared_event_ids == tuple(
        sorted((original.id, cross_post.id))
    )
    assert counts(result)["independent.shared_events"] == 1


def test_unknown_participant_context_remains_quarantined() -> None:
    statement = observation("unknown-context", "Anna likes tea")
    result = compile_history(
        observations=(statement,),
        observation_contexts=(
            context(statement.id, attribution=AttributionState.AMBIGUOUS),
        ),
        approvals=(approval(RelationshipRecordKind.OBSERVATION, statement.id),),
    )

    assert result.snapshots == ()
    assert any(
        item.code == RelationshipDiagnosticCode.UNRESOLVED_CONTEXT
        for item in result.quarantines
    )


def test_role_statement_is_exact_structured_history_not_relationship_judgment() -> None:
    role = observation("role-statement", "Anna is my sister")
    result = compile_history(
        observations=(role,),
        observation_contexts=(context(role.id, role=True),),
        approvals=(approval(RelationshipRecordKind.OBSERVATION, role.id),),
    )

    assert result.histories[0].relationship_role_observation_ids == (role.id,)
    assert counts(result)["explicit.relationship_role_statements"] == 1
    serialized = result.model_dump_json().lower()
    for forbidden in ("trustworthy", "compatible", "close relationship", "personality trait"):
        assert forbidden not in serialized


def test_dependency_hash_changes_when_source_record_changes_under_same_id() -> None:
    original = preference(literal="peonies")
    changed = preference(literal="sushi")
    original_result = compile_history(
        preferences=(original,),
        approvals=(approval(RelationshipRecordKind.PREFERENCE, original.id),),
    )
    changed_result = compile_history(
        preferences=(changed,),
        approvals=(approval(RelationshipRecordKind.PREFERENCE, changed.id),),
    )

    assert original_result.dependencies[0].record_id == changed_result.dependencies[0].record_id
    assert original_result.dependencies[0].record_hash != changed_result.dependencies[0].record_hash


def test_unapproved_archive_record_is_not_used() -> None:
    liked = preference()
    result = compile_history(preferences=(liked,))

    assert result.snapshots == ()
    assert result.dependencies == ()
