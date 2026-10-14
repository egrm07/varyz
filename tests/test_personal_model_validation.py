"""Adversarial tests for deterministic interpretation readiness gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from varyz.domain import Sensitivity
from varyz.personal_model.models import (
    AlternativeExplanation,
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
    CountersearchLane,
    CountersearchManifest,
    CountersearchState,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    InterpretationCandidate,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    ScopeKind,
    SourceTurn,
    TemporalScope,
    TextSpan,
)
from varyz.personal_model.synthesis import EpisodeCorrelation
from varyz.personal_model.validation import (
    InterpretationValidator,
    OwnerInterpretationReview,
    OwnerJudgment,
    ValidationPolicy,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PRODUCER = ProducerMetadata(
    kind=ProducerKind.LOCAL_MODEL,
    name="fixture-proposer",
    version="1",
    model_name="fixture-model",
    prompt_version="fixture-prompt-v1",
)


def evidence_set(
    count: int,
    *,
    group_ids: tuple[str, ...] | None = None,
    dialogues: tuple[DialogueContext, ...] | None = None,
    modalities: tuple[ObservationModality, ...] | None = None,
    propositions: tuple[str, ...] | None = None,
    domains: tuple[str, ...] | None = None,
    complete: bool = True,
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
) -> tuple[
    tuple[ConversationEpisode, ...],
    tuple[Observation, ...],
    tuple[EpisodeCorrelation, ...],
]:
    episodes: list[ConversationEpisode] = []
    observations: list[Observation] = []
    correlations: list[EpisodeCorrelation] = []
    for index in range(count):
        entry_id = f"entry-{index}"
        episode_id = f"episode-{index}"
        observation_id = f"observation-{index}"
        literal = (
            propositions[index]
            if propositions
            else f"I protected important commitment {index} despite a competing cost."
        )
        at = NOW + timedelta(days=index * 45)
        span = TextSpan(
            entry_id=entry_id,
            start=0,
            end=len(literal),
            exact_literal=literal,
            source_revision=f"revision-{index}",
        )
        episode_evidence = EvidenceReference(
            entry_id=entry_id,
            role=EvidenceRole.DEFINES_SCOPE,
            episode_id=episode_id,
            span=span,
            sensitivity=sensitivity,
        )
        episodes.append(
            ConversationEpisode(
                id=episode_id,
                generation_id="generation-1",
                evidence=(episode_evidence,),
                confidence=1.0,
                sensitivity=sensitivity,
                derivation=DerivationKind.OBSERVED,
                producer=PRODUCER,
                readiness=Readiness.STAGED,
                created_at=NOW,
                source="fixture-chat",
                source_thread_id=f"thread-{index}",
                turns=(SourceTurn(entry_id=entry_id, order=0, occurred_at=at),),
                initiating_entry_id=entry_id,
                target_entry_id=entry_id,
                completeness=(
                    ContextCompleteness.COMPLETE if complete else ContextCompleteness.UNKNOWN
                ),
                boundary_policy_version="fixture-v1",
            )
        )
        observations.append(
            Observation(
                id=observation_id,
                generation_id="generation-1",
                evidence=(
                    episode_evidence.model_copy(
                        update={
                            "role": EvidenceRole.SUPPORTS,
                            "observation_id": observation_id,
                        }
                    ),
                ),
                temporal=TemporalScope(stated_at=at),
                confidence=1.0,
                sensitivity=sensitivity,
                derivation=DerivationKind.OBSERVED,
                producer=PRODUCER,
                readiness=Readiness.STAGED,
                created_at=NOW,
                episode_id=episode_id,
                kind=ObservationKind.BOUNDARY,
                proposition=literal,
                spans=(span,),
                speaker_person_id="person-owner",
                speaker_attribution=AttributionState.RESOLVED,
                subject_kind=EntityKind.PERSON,
                subject_id="person-owner",
                subject_attribution=AttributionState.RESOLVED,
                dialogue_context=(
                    dialogues[index] if dialogues else DialogueContext.VOLUNTEERED_REASONING
                ),
                polarity=Polarity.POSITIVE,
                modality=(modalities[index] if modalities else ObservationModality.ASSERTED),
                cost_or_stakes_literal="gave up a competing commitment",
            )
        )
        correlations.append(
            EpisodeCorrelation(
                episode_id=episode_id,
                independence_group_id=(group_ids[index] if group_ids else f"decision-{index}"),
                domain=(
                    domains[index] if domains else ("work", "relationships", "health")[index % 3]
                ),
                decision_at=at,
            )
        )
    return tuple(episodes), tuple(observations), tuple(correlations)


def manifest() -> CountersearchManifest:
    support = CountersearchLane(
        query_fingerprint="support-query",
        query_variants=3,
        max_candidates=50,
        candidates_considered=10,
        candidate_pool_hash="shared-pool",
        result_hash="support-result",
        retrieval_version="retrieval-v1",
    )
    counter = support.model_copy(
        update={
            "query_fingerprint": "counter-query",
            "result_hash": "counter-result",
        }
    )
    return CountersearchManifest(
        support=support,
        counter=counter,
        source_scopes=("fixture-chat",),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        completed=True,
    )


def candidate(
    observations: tuple[Observation, ...],
    correlations: tuple[EpisodeCorrelation, ...],
    *,
    support: tuple[int, ...] = (0, 1, 2),
    contradict: tuple[int, ...] = (),
    scope: InterpretationScope | None = None,
    kind: InterpretationKind = InterpretationKind.VALUE,
    hypothesis: str = "Across distinct decisions, the owner protects chosen commitments.",
    countersearch_manifest: CountersearchManifest | None = None,
) -> InterpretationCandidate:
    role_indexes = (
        *((EvidenceRole.SUPPORTS, index) for index in support),
        *((EvidenceRole.CONTRADICTS, index) for index in contradict),
    )
    assignments = tuple(
        InterpretationEvidence(
            episode_id=observations[index].episode_id,
            observation_ids=(observations[index].id,),
            role=role,
            independence_group_id=correlations[index].independence_group_id,
            strength_features=("maintained_boundary", "costly_choice"),
        )
        for role, index in role_indexes
    )
    refs = tuple(
        EvidenceReference(
            entry_id=observations[index].spans[0].entry_id,
            role=role,
            episode_id=observations[index].episode_id,
            observation_id=observations[index].id,
            span=observations[index].spans[0],
            independence_group_id=correlations[index].independence_group_id,
            sensitivity=observations[index].sensitivity,
        )
        for role, index in role_indexes
    )
    return InterpretationCandidate(
        id="candidate-1",
        generation_id="generation-1",
        evidence=refs,
        temporal=TemporalScope(
            valid_from=observations[min(support)].temporal.stated_at,
            valid_to=observations[max(support)].temporal.stated_at,
        ),
        confidence=1.0,
        sensitivity=max(
            (observations[index].sensitivity for _, index in role_indexes),
            key=lambda item: list(Sensitivity).index(item),
        ),
        derivation=DerivationKind.INFERRED,
        producer=PRODUCER,
        readiness=Readiness.STAGED,
        created_at=NOW,
        concept_key="protected_commitments",
        hypothesis=hypothesis,
        kind=kind,
        scope=scope or InterpretationScope(kind=ScopeKind.GLOBAL),
        evidence_assignments=assignments,
        alternatives=(
            AlternativeExplanation(
                thesis="Temporary external pressure could explain the choices.",
                distinguishing_evidence="Compare equally costly choices without pressure.",
            ),
        ),
        missing_evidence=("A comparable unpressured decision remains unknown.",),
        countersearch_state=CountersearchState.COMPLETE,
        countersearch_manifest=countersearch_manifest or manifest(),
    )


def validate(
    built: InterpretationCandidate,
    episodes: tuple[ConversationEpisode, ...],
    observations: tuple[Observation, ...],
    correlations: tuple[EpisodeCorrelation, ...],
    **kwargs: object,
):
    return InterpretationValidator(
        ValidationPolicy(calibration_manifest_hash="fixture-calibration-sha256")
    ).validate(
        built,
        generation_id="generation-1",
        observations=observations,
        episodes=episodes,
        correlations=correlations,
        validated_at=NOW + timedelta(days=400),
        **kwargs,
    )


def issue_codes(result) -> set[str]:
    return {item.code for item in result.issues}


def test_independent_costly_choices_and_maintained_boundaries_can_be_ready() -> None:
    episodes, observations, correlations = evidence_set(3)
    result = validate(candidate(observations, correlations), episodes, observations, correlations)
    assert result.passed
    assert result.interpretation is not None
    assert result.interpretation.readiness == Readiness.READY
    assert result.interpretation.confidence != 1.0
    assert "Validated 3 independent support groups" in result.interpretation.derivation_summary


def test_unfrozen_calibration_cannot_activate_an_interpretation() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(observations, correlations)
    result = InterpretationValidator().validate(
        built,
        generation_id="generation-1",
        observations=observations,
        episodes=episodes,
        correlations=correlations,
        validated_at=NOW + timedelta(days=400),
    )
    assert result.interpretation is None
    assert "calibration_not_frozen" in issue_codes(result)


def test_twenty_turns_about_one_decision_are_one_support_group() -> None:
    groups = ("one-underlying-decision",) * 20
    episodes, observations, correlations = evidence_set(20, group_ids=groups)
    built = candidate(
        observations,
        correlations,
        support=tuple(range(20)),
    )
    result = validate(built, episodes, observations, correlations)
    assert result.features.independent_support_groups == 1
    assert "insufficient_independence" in issue_codes(result)
    assert result.interpretation is None


def test_same_short_answer_under_different_initiating_turns_is_not_a_trait() -> None:
    episodes, observations, correlations = evidence_set(
        2,
        propositions=("Sure.", "Sure."),
        dialogues=(
            DialogueContext.PROMPTED_AGREEMENT,
            DialogueContext.HYPOTHETICAL,
        ),
        modalities=(
            ObservationModality.UNCERTAIN,
            ObservationModality.HYPOTHETICAL,
        ),
    )
    built = candidate(observations, correlations, support=(0, 1))
    result = validate(built, episodes, observations, correlations)
    assert result.features.qualifying_support_groups == 0
    assert "weak_dialogue_evidence" in issue_codes(result)


@pytest.mark.parametrize(
    ("dialogue", "modality"),
    [
        (DialogueContext.QUOTATION, ObservationModality.QUOTED),
        (DialogueContext.HYPOTHETICAL, ObservationModality.HYPOTHETICAL),
        (
            DialogueContext.HUMOUR_OR_SARCASM,
            ObservationModality.HUMOROUS_OR_SARCASTIC,
        ),
        (DialogueContext.INSTRUCTION, ObservationModality.ASSERTED),
        (DialogueContext.PROMPTED_AGREEMENT, ObservationModality.UNCERTAIN),
    ],
)
def test_context_traps_cannot_license_durable_values(
    dialogue: DialogueContext, modality: ObservationModality
) -> None:
    episodes, observations, correlations = evidence_set(
        3,
        dialogues=(dialogue,) * 3,
        modalities=(modality,) * 3,
    )
    result = validate(candidate(observations, correlations), episodes, observations, correlations)
    assert "weak_dialogue_evidence" in issue_codes(result)


def test_strongest_counterexample_is_retained_not_averaged_away() -> None:
    episodes, observations, correlations = evidence_set(
        4,
        propositions=(
            "I protected the commitment at a personal cost.",
            "I protected the boundary despite pressure.",
            "I declined easier work to keep the promise.",
            "I abandoned that commitment as soon as it became inconvenient.",
        ),
    )
    result = validate(
        candidate(observations, correlations, contradict=(3,)),
        episodes,
        observations,
        correlations,
    )
    assert result.interpretation is not None
    assert result.interpretation.strongest_counterexample == observations[3].proposition
    assert "1 independent counterevidence" in result.interpretation.counterevidence_summary


def test_explicit_strongest_counterexample_rank_is_preserved() -> None:
    episodes, observations, correlations = evidence_set(5)
    built = candidate(observations, correlations, contradict=(3, 4))
    assignments = tuple(
        item.model_copy(
            update={
                "strength_features": (
                    (*item.strength_features, "strongest_counterexample")
                    if observations[4].id in item.observation_ids
                    else item.strength_features
                )
            }
        )
        for item in built.evidence_assignments
    )
    built = built.model_copy(update={"evidence_assignments": assignments})
    result = validate(built, episodes, observations, correlations)
    assert result.interpretation is not None
    assert result.interpretation.strongest_counterexample == observations[4].proposition


def test_deleted_or_missing_evidence_blocks_activation() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(observations, correlations)
    result = validate(
        built,
        episodes,
        observations,
        correlations,
        deleted_observation_ids=frozenset({observations[1].id}),
    )
    assert "missing_observation" in issue_codes(result)
    assert result.interpretation is None
    assert result.candidate.readiness == Readiness.STAGED


def test_alternative_cannot_cite_absent_evidence() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(observations, correlations)
    alternatives = (built.alternatives[0].model_copy(update={"evidence_ids": ("invented",)}),)
    built = built.model_copy(update={"alternatives": alternatives})
    result = validate(built, episodes, observations, correlations)
    assert "alternative_evidence_missing" in issue_codes(result)


def test_corrupt_asymmetric_countersearch_is_rejected_deterministically() -> None:
    episodes, observations, correlations = evidence_set(3)
    good = manifest()
    bad_counter = CountersearchLane.model_construct(
        **{
            **good.counter.model_dump(),
            "query_variants": 1,
            "max_candidates": 5,
        }
    )
    corrupt = CountersearchManifest.model_construct(
        **{**good.model_dump(), "support": good.support, "counter": bad_counter}
    )
    valid_candidate = candidate(observations, correlations)
    raw_candidate = dict(valid_candidate.__dict__)
    raw_candidate["countersearch_manifest"] = corrupt
    built = InterpretationCandidate.model_construct(**raw_candidate)
    result = validate(built, episodes, observations, correlations)
    assert "countersearch_asymmetric" in issue_codes(result)


def test_work_only_evidence_cannot_become_global() -> None:
    episodes, observations, correlations = evidence_set(3, domains=("work", "work", "work"))
    result = validate(candidate(observations, correlations), episodes, observations, correlations)
    assert "global_scope_without_cross_domain_support" in issue_codes(result)


def test_domain_scope_rejects_evidence_from_another_domain() -> None:
    episodes, observations, correlations = evidence_set(
        3, domains=("work", "work", "relationships")
    )
    built = candidate(
        observations,
        correlations,
        scope=InterpretationScope(kind=ScopeKind.DOMAIN, domain="work"),
    )
    result = validate(built, episodes, observations, correlations)
    assert "domain_scope_mismatch" in issue_codes(result)


def test_relationship_read_requires_named_relationship_scope() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(
        observations,
        correlations,
        kind=InterpretationKind.RELATIONSHIP,
    )
    result = validate(built, episodes, observations, correlations)
    assert "relationship_read_requires_relationship_scope" in issue_codes(result)


def test_change_over_time_requires_counterperiod_and_elapsed_time() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(
        observations,
        correlations,
        support=(0, 1),
        contradict=(2,),
        kind=InterpretationKind.CHANGE_OVER_TIME,
        scope=InterpretationScope(
            kind=ScopeKind.TIME_PERIOD,
            valid_from=NOW,
            valid_to=NOW + timedelta(days=180),
        ),
    )
    result = validate(built, episodes, observations, correlations)
    assert "insufficient_change_interval" not in issue_codes(result)
    assert "change_without_counterperiod" not in issue_codes(result)


def test_scope_limited_period_rejects_out_of_period_support() -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(
        observations,
        correlations,
        scope=InterpretationScope(
            kind=ScopeKind.TIME_PERIOD,
            valid_from=NOW,
            valid_to=NOW + timedelta(days=50),
        ),
    )
    result = validate(built, episodes, observations, correlations)
    assert "time_scope_mismatch" in issue_codes(result)


def test_near_source_paraphrase_concentration_is_blocked() -> None:
    thesis = "I protect commitments despite competing costs"
    episodes, observations, correlations = evidence_set(
        3,
        propositions=(thesis, thesis, thesis),
    )
    built = candidate(
        observations,
        correlations,
        hypothesis=thesis,
    )
    result = validate(built, episodes, observations, correlations)
    assert "near_source_paraphrase_concentration" in issue_codes(result)


def test_incomplete_episode_context_blocks_activation() -> None:
    episodes, observations, correlations = evidence_set(3, complete=False)
    result = validate(candidate(observations, correlations), episodes, observations, correlations)
    assert "incomplete_context" in issue_codes(result)


@pytest.mark.parametrize(
    ("judgment", "expected_readiness", "passes"),
    [
        (OwnerJudgment.CONFIRM, Readiness.READY, True),
        (OwnerJudgment.PARTIAL, Readiness.LIMITED, True),
        (OwnerJudgment.REJECT, None, False),
    ],
)
def test_owner_confirm_partial_and_reject_are_honoured(
    judgment: OwnerJudgment,
    expected_readiness: Readiness | None,
    passes: bool,
) -> None:
    episodes, observations, correlations = evidence_set(3)
    built = candidate(observations, correlations)
    result = validate(
        built,
        episodes,
        observations,
        correlations,
        owner_review=OwnerInterpretationReview(
            candidate_id=built.id,
            judgment=judgment,
            note="Owner-reviewed fixture.",
        ),
    )
    assert result.passed is passes
    if expected_readiness is None:
        assert result.interpretation is None
        assert "owner_rejected" in issue_codes(result)
    else:
        assert result.interpretation is not None
        assert result.interpretation.readiness == expected_readiness


def test_confirming_an_interpretation_cannot_change_confidence_bytes() -> None:
    """Owner agreement is display state, not another unit of evidence."""
    episodes, observations, correlations = evidence_set(3)
    built = candidate(observations, correlations)
    unreviewed = validate(built, episodes, observations, correlations)
    confirmed = validate(
        built,
        episodes,
        observations,
        correlations,
        owner_review=OwnerInterpretationReview(
            candidate_id=built.id,
            judgment=OwnerJudgment.CONFIRM,
            note="Owner confirms the reading.",
        ),
    )
    assert unreviewed.interpretation is not None
    assert confirmed.interpretation is not None
    def encode(value: float) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()

    assert encode(unreviewed.interpretation.confidence) == encode(
        confirmed.interpretation.confidence
    )
    assert confirmed.interpretation.derivation == DerivationKind.INFERRED


def test_provider_self_confidence_never_controls_validation_confidence() -> None:
    episodes, observations, correlations = evidence_set(3)
    high = candidate(observations, correlations)
    low = high.model_copy(update={"confidence": 0.0})
    high_result = validate(high, episodes, observations, correlations)
    low_result = validate(low, episodes, observations, correlations)
    assert high_result.interpretation is not None
    assert low_result.interpretation is not None
    assert high_result.interpretation.confidence == low_result.interpretation.confidence


def test_unclassified_sensitive_evidence_fails_closed() -> None:
    episodes, observations, correlations = evidence_set(3, sensitivity=Sensitivity.UNCLASSIFIED)
    result = validate(candidate(observations, correlations), episodes, observations, correlations)
    assert "sensitivity_not_allowed" in issue_codes(result)
