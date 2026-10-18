"""Focused contract tests for the canonical personal-model domain."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from varyz.domain import Sensitivity
from varyz.personal_model.models import (
    AlternativeExplanation,
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
    CountersearchLane,
    CountersearchManifest,
    CountersearchState,
    DateAmbiguity,
    DateAmbiguityKind,
    DateFactValue,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValueKind,
    Interpretation,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    MonthDayFactValue,
    Observation,
    ObservationKind,
    ObservationModality,
    OmittedRange,
    Person,
    Polarity,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    RecordState,
    ReplyEdge,
    ScopeKind,
    SourceTurn,
    SubjectReference,
    TemporalScope,
    TextFactValue,
    TextSpan,
    conversation_episode_id,
    derived_record_id,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def producer() -> ProducerMetadata:
    return ProducerMetadata(
        kind=ProducerKind.DETERMINISTIC,
        name="contract-fixture",
        version="1",
        rule_version="1",
    )


def span(entry_id: str = "entry-1", literal: str = "I like sample-item") -> TextSpan:
    return TextSpan(entry_id=entry_id, start=0, end=len(literal), exact_literal=literal)


def support(entry_id: str = "entry-1") -> EvidenceReference:
    return EvidenceReference(
        entry_id=entry_id,
        role=EvidenceRole.SUPPORTS,
        span=span(entry_id),
        independence_group_id="decision-1",
        sensitivity=Sensitivity.PERSONAL,
    )


def derived_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": "record-1",
        "generation_id": "generation-1",
        "evidence": (support(),),
        "claim_ids": ("claim-1",),
        "confidence": 0.8,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.EXPLICIT,
        "producer": producer(),
        "readiness": Readiness.STAGED,
        "created_at": NOW,
    }
    fields.update(overrides)
    return fields


def test_derived_identity_ignores_display_wording_but_tracks_membership() -> None:
    first = derived_record_id(
        "fact",
        canonical_subject="person:example",
        canonical_predicate="likes",
        evidence_membership=("entry-2", "entry-1"),
        identity_version="1",
    )
    rephrased = derived_record_id(
        "fact",
        canonical_subject="person:example",
        canonical_predicate="likes",
        evidence_membership=("entry-1", "entry-2"),
        identity_version="1",
    )
    changed_evidence = derived_record_id(
        "fact",
        canonical_subject="person:example",
        canonical_predicate="likes",
        evidence_membership=("entry-3",),
        identity_version="1",
    )

    assert first == rephrased
    assert first != changed_evidence


def test_episode_identity_preserves_source_order_and_policy_version() -> None:
    first = conversation_episode_id(("entry-1", "entry-2"), boundary_policy_version="1")

    assert first != conversation_episode_id(
        ("entry-2", "entry-1"), boundary_policy_version="1"
    )
    assert first != conversation_episode_id(
        ("entry-1", "entry-2"), boundary_policy_version="2"
    )
    with pytest.raises(ValueError, match="unique"):
        conversation_episode_id(("entry-1", "entry-1"), boundary_policy_version="1")


def test_episode_requires_real_membership_and_declared_incompleteness() -> None:
    episode = ConversationEpisode(
        **derived_fields(id="episode-1"),
        source="example-source",
        source_thread_id="example-thread",
        turns=(
            SourceTurn(entry_id="entry-1", order=0, occurred_at=NOW),
            SourceTurn(entry_id="entry-2", order=1, occurred_at=NOW),
        ),
        participant_source_keys=("source-person:owner", "source-person:example"),
        reply_edges=(ReplyEdge(from_entry_id="entry-2", to_entry_id="entry-1"),),
        initiating_entry_id="entry-1",
        target_entry_id="entry-2",
        completeness=ContextCompleteness.INCOMPLETE,
        boundary_policy_version="1",
        omitted_ranges=(
            OmittedRange(
                after_entry_id="entry-1",
                before_entry_id="entry-2",
                omitted_count=3,
                reason="annotator budget",
            ),
        ),
    )

    assert episode.turns[1].entry_id == "entry-2"
    with pytest.raises(ValidationError, match="complete episode"):
        ConversationEpisode.model_validate(
            {**episode.model_dump(), "completeness": ContextCompleteness.COMPLETE},
            strict=True,
        )


def test_episode_rejects_duplicate_source_order_values() -> None:
    with pytest.raises(ValidationError, match="unique source-order"):
        ConversationEpisode(
            **derived_fields(id="episode-duplicate-order"),
            source="example-source",
            turns=(
                SourceTurn(entry_id="entry-1", order=0, occurred_at=NOW),
                SourceTurn(entry_id="entry-2", order=0, occurred_at=NOW),
            ),
            initiating_entry_id="entry-1",
            target_entry_id="entry-2",
            completeness=ContextCompleteness.COMPLETE,
            boundary_policy_version="1",
        )


def test_exact_spans_preserve_literals_and_reject_fluent_replacements() -> None:
    exact = TextSpan(entry_id="entry-1", start=4, end=15, exact_literal="next Friday")

    assert exact.exact_literal == "next Friday"
    with pytest.raises(ValidationError, match="length"):
        TextSpan(entry_id="entry-1", start=4, end=14, exact_literal="next Friday")


def test_ready_observation_requires_resolved_attribution() -> None:
    fields = derived_fields(id="observation-1", readiness=Readiness.READY)

    with pytest.raises(ValidationError, match="resolved speaker"):
        Observation(
            **fields,
            episode_id="episode-1",
            kind=ObservationKind.STATEMENT,
            proposition="Example Person likes sample-item",
            spans=(span(),),
            speaker_source_key="source-person:example",
            speaker_attribution=AttributionState.UNKNOWN,
            subject_kind=EntityKind.PERSON,
            subject_id="person-example",
            subject_attribution=AttributionState.RESOLVED,
            dialogue_context=DialogueContext.DIRECT_ANSWER,
            polarity=Polarity.POSITIVE,
            modality=ObservationModality.ASSERTED,
        )


def test_dates_are_exact_or_explicitly_ambiguous() -> None:
    leap_day = MonthDayFactValue(
        kind=FactValueKind.MONTH_DAY,
        exact_literal="29 February",
        month=2,
        day=29,
    )
    unresolved = DateFactValue(
        kind=FactValueKind.DATE,
        exact_literal="next Friday",
        ambiguity=DateAmbiguity(
            kind=DateAmbiguityKind.RELATIVE,
            candidates=(date(2026, 8, 14), date(2026, 8, 21)),
            source_timestamp=NOW,
            locale="en-GB",
            timezone="Europe/Madrid",
            explanation="The source timestamp is known but conversational anchoring is unclear.",
        ),
    )

    assert leap_day.day == 29
    assert unresolved.value is None
    assert unresolved.exact_literal == "next Friday"
    with pytest.raises(ValidationError, match="either exact or explicitly ambiguous"):
        DateFactValue(
            kind=FactValueKind.DATE,
            exact_literal="03/04/26",
            value=date(2026, 4, 3),
            ambiguity=DateAmbiguity(
                kind=DateAmbiguityKind.NUMERIC_ORDER,
                candidates=(date(2026, 4, 3), date(2026, 3, 4)),
                explanation="Day/month order is unknown.",
            ),
        )


def test_fact_keeps_typed_value_literal_and_temporal_conflict() -> None:
    fact = Fact(
        **derived_fields(id="fact-example-date", readiness=Readiness.LIMITED),
        temporal=TemporalScope(original_time_literal="this year"),
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-example"),
        predicate="important_date",
        value=MonthDayFactValue(
            kind=FactValueKind.MONTH_DAY,
            exact_literal="April 5th",
            month=4,
            day=5,
        ),
        asserted_by_person_id="person-example",
        status=FactStatus.CONTRADICTED,
        contradicted_by_fact_ids=("fact-example-date-other",),
    )

    assert fact.value.exact_literal == "April 5th"
    assert fact.status == FactStatus.CONTRADICTED


def test_mention_only_evidence_cannot_become_a_preference() -> None:
    mention = EvidenceReference(entry_id="entry-1", role=EvidenceRole.MENTIONS)

    with pytest.raises(ValidationError, match="supporting evidence"):
        Preference(
            **derived_fields(id="preference-1", evidence=(mention,)),
            subject=SubjectReference(kind=EntityKind.PERSON, id="person-example"),
            predicate="likes.flower",
            object=TextFactValue(
                kind=FactValueKind.TEXT,
                exact_literal="sample-item",
                normalized="sample-item",
            ),
            valence=PreferenceValence.LIKE,
        )


def test_person_merge_requires_an_explicit_override() -> None:
    with pytest.raises(ValidationError, match="user override"):
        Person(
            **derived_fields(id="person-duplicate"),
            canonical_key="source:123",
            display_name="Example Person",
            state=RecordState.MERGED,
            merged_into_person_id="person-example",
        )


@pytest.mark.parametrize("record_kind", ["person", "project"])
def test_split_identity_requires_an_explicit_override(record_kind: str) -> None:
    model = Person if record_kind == "person" else Project
    identity_fields = (
        {"canonical_key": "source:example", "display_name": "Example Person"}
        if record_kind == "person"
        else {"canonical_key": "project:example", "display_name": "Example Project"}
    )

    with pytest.raises(ValidationError, match="user override"):
        model(
            **derived_fields(id=f"{record_kind}-split"),
            **identity_fields,
            state=RecordState.SPLIT,
        )

    split = model(
        **derived_fields(id=f"{record_kind}-split"),
        **identity_fields,
        state=RecordState.SPLIT,
        identity_override_id="override-split-example",
    )
    assert split.identity_override_id == "override-split-example"


def countersearch_manifest(*, completed: bool = True) -> CountersearchManifest:
    return CountersearchManifest(
        support=CountersearchLane(
            query_fingerprint="support-query-hash",
            query_variants=2,
            max_candidates=20,
            candidates_considered=8,
            candidate_pool_hash="support-pool-hash",
            result_hash="support-result-hash",
            retrieval_version="1",
        ),
        counter=CountersearchLane(
            query_fingerprint="counter-query-hash",
            query_variants=2,
            max_candidates=20,
            candidates_considered=7,
            candidate_pool_hash="counter-pool-hash",
            result_hash="counter-result-hash",
            retrieval_version="1",
        ),
        evidence_window_from=datetime(2025, 1, 1, tzinfo=UTC),
        evidence_window_to=datetime(2026, 8, 11, tzinfo=UTC),
        source_scopes=("approved-archive",),
        started_at=datetime(2026, 8, 11, 11, tzinfo=UTC),
        completed_at=datetime(2026, 8, 11, 12, tzinfo=UTC) if completed else None,
        completed=completed,
    )


def test_ready_interpretation_requires_countersearch_and_alternatives() -> None:
    assignment = InterpretationEvidence(
        episode_id="episode-1",
        observation_ids=("observation-1",),
        role=EvidenceRole.SUPPORTS,
        independence_group_id="decision-1",
    )
    fields = derived_fields(
        id="interpretation-1",
        derivation=DerivationKind.INFERRED,
        readiness=Readiness.READY,
    )

    with pytest.raises(ValidationError, match="completed countersearch"):
        Interpretation(
            **fields,
            concept_key="example_scoped_pattern",
            thesis="A scoped example hypothesis.",
            kind=InterpretationKind.VALUE,
            scope=InterpretationScope(kind=ScopeKind.DOMAIN, domain="example-domain"),
            evidence_assignments=(assignment,),
            alternatives=(
                AlternativeExplanation(
                    thesis="An example alternative.",
                    distinguishing_evidence="A different independent context.",
                ),
            ),
            counterevidence_summary="No decisive counterexample found in the bounded search.",
            countersearch_state=CountersearchState.INCOMPLETE,
            derivation_summary="One independent product decision supports the scoped hypothesis.",
            candidate_id="candidate-1",
        )


def test_ready_interpretation_requires_auditable_countersearch_manifest() -> None:
    assignment = InterpretationEvidence(
        episode_id="episode-1",
        observation_ids=("observation-1",),
        role=EvidenceRole.SUPPORTS,
        independence_group_id="decision-1",
    )

    fields = {
        **derived_fields(
            id="interpretation-audit",
            derivation=DerivationKind.INFERRED,
            readiness=Readiness.READY,
        ),
        "concept_key": "example_scoped_pattern",
        "thesis": "A scoped example hypothesis.",
        "kind": InterpretationKind.DECISION_PATTERN,
        "scope": InterpretationScope(kind=ScopeKind.DOMAIN, domain="example-domain"),
        "evidence_assignments": (assignment,),
        "alternatives": (
            AlternativeExplanation(
                thesis="An example alternative.",
                distinguishing_evidence="A different independent context.",
            ),
        ),
        "counterevidence_summary": "No decisive counterexample found.",
        "countersearch_state": CountersearchState.COMPLETE,
        "derivation_summary": "One scoped independent context supports the hypothesis.",
        "candidate_id": "candidate-audit",
    }

    with pytest.raises(ValidationError, match="auditable completed manifest"):
        Interpretation(**fields)

    ready = Interpretation(**fields, countersearch_manifest=countersearch_manifest())
    assert ready.readiness == Readiness.READY


def test_countersearch_manifest_enforces_symmetric_breadth() -> None:
    payload = countersearch_manifest().model_dump()
    payload["counter"]["max_candidates"] = 10

    with pytest.raises(ValidationError, match="candidate breadth must be symmetric"):
        CountersearchManifest.model_validate(payload, strict=True)


def test_models_forbid_hidden_reasoning_and_unknown_fields() -> None:
    payload = {
        **derived_fields(id="person-example"),
        "canonical_key": "source:example-123",
        "display_name": "Example Person",
        "chain_of_thought": "private scratch reasoning",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Person.model_validate(payload, strict=True)
