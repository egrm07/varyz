"""Offline contract tests for independent personal-model readiness gates."""

from __future__ import annotations

from datetime import UTC, datetime

from varyz.domain import (
    ConversationMetadata,
    Entry,
    Kind,
    Sensitivity,
    TimePrecision,
)
from varyz.personal_model.episodes import EpisodeReconstructor
from varyz.personal_model.identity import PersonResolver, SourceAlias, SourcePerson
from varyz.personal_model.memory import AtomicClaimInput, atomic_claim_content_hash
from varyz.personal_model.models import (
    AliasKind,
    AlternativeExplanation,
    AttributionState,
    CountersearchState,
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
    MonthDayFactValue,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    ScopeKind,
    SubjectReference,
    TemporalScope,
    TextSpan,
)
from varyz.personal_model.projects import ProjectCompilation
from varyz.personal_model.readiness import (
    GATE_DEFINITIONS,
    GATE_KEYS,
    GateVerdict,
    LifeLookupFixture,
    PersonalModelReadinessEvaluator,
    ReadinessInputs,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
GENERATION = "readiness-generation"


def _entry() -> Entry:
    body = "My birthday is 14 May because family rituals matter"
    return Entry(
        id="entry-birthday",
        kind=Kind.MESSAGE,
        source="fixture-chat",
        source_account="owner-account",
        external_id="birthday-turn",
        content_hash="revision-birthday",
        body=body,
        occurred_start=NOW.isoformat(),
        time_precision=TimePrecision.EXACT,
        ingested_at=NOW.isoformat(),
        thread_id="family-thread",
        conversation=ConversationMetadata(
            conversation_id="family-conversation",
            participant_ids=("anna",),
            participants_complete=True,
            sender_id="anna",
            sender_label="Anna",
            source_order=1,
        ),
        sensitivity=Sensitivity.PERSONAL,
    )


def _empty_projects() -> ProjectCompilation:
    return ProjectCompilation(
        projects=(),
        goals=(),
        review_candidates=(),
        quarantines=(),
        rejections=(),
        action_handoffs=(),
        history_facts=(),
        purpose_changes=(),
        status_changes=(),
        project_candidate_to_project_id={},
        goal_candidate_to_goal_id={},
    )


def _rich_inputs() -> tuple[ReadinessInputs, str]:
    entry = _entry()
    source_person = SourcePerson(
        source=entry.source,
        source_account=entry.source_account,
        source_key="id:anna",
        display_name="Anna",
        aliases=(
            SourceAlias(
                kind=AliasKind.SOURCE_ACCOUNT_ID,
                exact_value="anna",
                evidence_entry_ids=(entry.id,),
                verified_durable=True,
            ),
        ),
        source_key_is_durable=True,
        sensitivity=Sensitivity.PERSONAL,
    )
    identity = PersonResolver().resolve(
        (source_person,), generation_id=GENERATION, created_at=NOW
    )
    person = identity.persons[0].model_copy(update={"readiness": Readiness.LIMITED})
    episodes = EpisodeReconstructor().build(
        (entry,), generation_id=GENERATION, created_at=NOW
    )
    episode = episodes.episodes[0]
    observation_literal = "family rituals matter"
    observation_start = entry.body.index(observation_literal)
    observation_span = TextSpan(
        entry_id=entry.id,
        start=observation_start,
        end=observation_start + len(observation_literal),
        exact_literal=observation_literal,
        source_revision=entry.content_hash,
    )
    producer = ProducerMetadata(
        kind=ProducerKind.DETERMINISTIC,
        name="readiness-fixture",
        version="1",
        rule_version="1",
    )
    observation = Observation(
        id="observation-family",
        generation_id=GENERATION,
        evidence=(
            EvidenceReference(
                entry_id=entry.id,
                role=EvidenceRole.SUPPORTS,
                episode_id=episode.id,
                span=observation_span,
            ),
        ),
        temporal=TemporalScope(stated_at=NOW),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.OBSERVED,
        producer=producer,
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id=episode.id,
        kind=ObservationKind.REASON,
        proposition=observation_literal,
        spans=(observation_span,),
        speaker_person_id=person.id,
        speaker_source_key="id:anna",
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PERSON,
        subject_id=person.id,
        subject_attribution=AttributionState.RESOLVED,
        dialogue_context=DialogueContext.VOLUNTEERED_REASONING,
        polarity=Polarity.POSITIVE,
        modality=ObservationModality.ASSERTED,
    )
    birthday_literal = "14 May"
    birthday_start = entry.body.index(birthday_literal)
    birthday_span = TextSpan(
        entry_id=entry.id,
        start=birthday_start,
        end=birthday_start + len(birthday_literal),
        exact_literal=birthday_literal,
        source_revision=entry.content_hash,
    )
    subject = SubjectReference(kind=EntityKind.PERSON, id=person.id)
    claim = AtomicClaimInput(
        id="claim-birthday",
        subject=subject,
        asserted_by_person_id=person.id,
        span=birthday_span,
        content_hash=atomic_claim_content_hash(
            birthday_span.exact_literal, birthday_span.source_revision
        ),
        occurred_at=NOW,
        sensitivity=Sensitivity.PERSONAL,
        dialogue_context=DialogueContext.DIRECT_ANSWER,
        modality=ObservationModality.ASSERTED,
        polarity=Polarity.POSITIVE,
    )
    fact = Fact(
        id="fact-birthday",
        generation_id=GENERATION,
        evidence=(
            EvidenceReference(
                entry_id=entry.id,
                role=EvidenceRole.SUPPORTS,
                claim_id=claim.id,
                span=birthday_span,
            ),
        ),
        claim_ids=(claim.id,),
        temporal=TemporalScope(stated_at=NOW),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.EXPLICIT,
        producer=producer,
        readiness=Readiness.READY,
        created_at=NOW,
        subject=subject,
        predicate="person.birth_date",
        value=MonthDayFactValue(
            kind=FactValueKind.MONTH_DAY,
            exact_literal=birthday_literal,
            month=5,
            day=14,
        ),
        asserted_by_person_id=person.id,
    )
    assignment = InterpretationEvidence(
        episode_id=episode.id,
        observation_ids=(observation.id,),
        role=EvidenceRole.SUPPORTS,
        independence_group_id="one-conversation",
    )
    candidate = InterpretationCandidate(
        id="candidate-family",
        generation_id=GENERATION,
        evidence=(
            EvidenceReference(
                entry_id=entry.id,
                role=EvidenceRole.SUPPORTS,
                episode_id=episode.id,
                observation_id=observation.id,
                span=observation_span,
                independence_group_id="one-conversation",
            ),
        ),
        temporal=TemporalScope(),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.INFERRED,
        producer=producer,
        readiness=Readiness.STAGED,
        created_at=NOW,
        concept_key="family.rituals",
        hypothesis=observation_literal,
        kind=InterpretationKind.VALUE,
        scope=InterpretationScope(kind=ScopeKind.GLOBAL),
        evidence_assignments=(assignment,),
        alternatives=(
            AlternativeExplanation(
                thesis="It may describe one occasion.",
                distinguishing_evidence="A different family context would distinguish it.",
            ),
        ),
        countersearch_state=CountersearchState.NOT_STARTED,
    )
    fixture = LifeLookupFixture(
        fixture_id="anna-birthday",
        record_kind="fact",
        entity_kind=EntityKind.PERSON,
        entity_id=person.id,
        predicate="person.birth_date",
        expected_record_id=fact.id,
        expected_evidence_entry_id=entry.id,
    )
    records = (episode, person, observation, fact, candidate)
    return (
        ReadinessInputs(
            entries=(entry,),
            episodes=episodes,
            observations=(observation,),
            identity=identity,
            source_people=(source_person,),
            projects=_empty_projects(),
            project_candidates=(),
            atomic_claims=(claim,),
            records=records,
            lookup_fixtures=(fixture,),
        ),
        person.id,
    )


def test_threshold_contract_states_each_number_reason_and_failure_once() -> None:
    assert tuple(item.key for item in GATE_DEFINITIONS) == GATE_KEYS
    assert len(GATE_DEFINITIONS) == len(set(GATE_KEYS)) == 12
    for definition in GATE_DEFINITIONS:
        assert 0 <= definition.threshold <= 1
        assert definition.rationale.endswith(".")
        assert definition.failure.endswith(".")
        assert definition.measured_as.endswith(".")


def test_gates_report_independently_and_fact_is_ready_inside_limited_person() -> None:
    inputs, person_id = _rich_inputs()

    report = PersonalModelReadinessEvaluator().evaluate(inputs)

    assert tuple(item.gate for item in report.gates) == GATE_KEYS
    assert report.by_gate["episode_context_completeness"].verdict == GateVerdict.READY
    assert report.by_gate["observation_attribution"].verdict == GateVerdict.READY
    assert report.by_gate["person_resolution_coverage"].verdict == GateVerdict.READY
    assert report.by_gate["fact_attribution"].verdict == GateVerdict.READY
    assert report.by_gate["life_lookup_accuracy"].verdict == GateVerdict.READY
    assert report.fact_records[0].verdict == GateVerdict.READY
    person = next(item for item in inputs.records if item.id == person_id)
    assert person.readiness == Readiness.LIMITED

    # The single thin, global, support-only paraphrase fails five gates at once;
    # every other verdict is still present and compilation policy never sees it.
    for key in (
        "higher_order_independent_support",
        "countersearch_completion",
        "scope_correctness",
        "confidence_support_gap",
        "near_source_paraphrase_concentration",
    ):
        assert report.by_gate[key].verdict == GateVerdict.LIMITED


def test_absent_samples_are_unavailable_without_masking_other_gates() -> None:
    identity = PersonResolver().resolve((), generation_id=GENERATION, created_at=NOW)
    episodes = EpisodeReconstructor().build((), generation_id=GENERATION, created_at=NOW)
    report = PersonalModelReadinessEvaluator().evaluate(
        ReadinessInputs(
            entries=(),
            episodes=episodes,
            observations=(),
            identity=identity,
            source_people=(),
            projects=_empty_projects(),
            project_candidates=(),
            atomic_claims=(),
            records=(),
        )
    )

    assert len(report.gates) == 12
    assert {item.verdict for item in report.gates} == {GateVerdict.UNAVAILABLE}
    assert all(item.measured_value is None and item.denominator == 0 for item in report.gates)
