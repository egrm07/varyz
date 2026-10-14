"""Deterministic validation gates for higher-order personal interpretations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from varyz.domain import Sensitivity
from varyz.personal_model.models import (
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
    CountersearchState,
    DerivationKind,
    DialogueContext,
    EvidenceRole,
    Interpretation,
    InterpretationCandidate,
    InterpretationKind,
    Observation,
    ObservationKind,
    ObservationModality,
    Person,
    Project,
    Readiness,
    ScopeKind,
    derived_record_id,
)
from varyz.personal_model.synthesis import (
    EpisodeCorrelation,
    interpretation_identity_membership,
)

VALIDATION_POLICY_VERSION = "interpretation-validation-v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class OwnerJudgment(StrEnum):
    CONFIRM = "confirm"
    PARTIAL = "partial"
    REJECT = "reject"


class OwnerInterpretationReview(_FrozenModel):
    candidate_id: str = Field(min_length=1)
    judgment: OwnerJudgment
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class ValidationPolicy(_FrozenModel):
    version: str = VALIDATION_POLICY_VERSION
    calibration_manifest_hash: str | None = Field(default=None, min_length=16)
    min_independent_support: int = Field(default=2, ge=2)
    min_global_independent_support: int = Field(default=3, ge=2)
    min_contexts: int = Field(default=2, ge=2)
    min_global_domains: int = Field(default=2, ge=2)
    min_change_days: int = Field(default=30, ge=1)
    max_contradiction_ratio: float = Field(default=0.75, ge=0, le=1)
    near_paraphrase_similarity: float = Field(default=0.92, ge=0, le=1)
    max_near_paraphrase_concentration: float = Field(default=0.8, ge=0, le=1)
    ready_confidence_floor: float = Field(default=0.62, ge=0, le=1)
    base_weight: float = Field(default=0.18, ge=0, le=1)
    independent_group_weight: float = Field(default=0.10, ge=0, le=1)
    qualifying_group_weight: float = Field(default=0.06, ge=0, le=1)
    context_weight: float = Field(default=0.05, ge=0, le=1)
    domain_weight: float = Field(default=0.025, ge=0, le=1)
    temporal_span_weight: float = Field(default=0.075, ge=0, le=1)
    contradiction_penalty: float = Field(default=0.08, ge=0, le=1)
    paraphrase_penalty: float = Field(default=0.10, ge=0, le=1)
    allowed_sensitivities: tuple[Sensitivity, ...] = (
        Sensitivity.PUBLIC,
        Sensitivity.PERSONAL,
        Sensitivity.SENSITIVE,
    )


class ValidationIssue(_FrozenModel):
    code: str
    detail: str
    blocking: bool = True


class ValidationFeatures(_FrozenModel):
    independent_support_groups: int = Field(ge=0)
    qualifying_support_groups: int = Field(ge=0)
    contradiction_groups: int = Field(ge=0)
    context_count: int = Field(ge=0)
    domain_count: int = Field(ge=0)
    temporal_span_days: int = Field(ge=0)
    near_paraphrase_concentration: float = Field(ge=0, le=1)
    owner_judgment: OwnerJudgment | None = None


class ValidationResult(_FrozenModel):
    candidate: InterpretationCandidate
    interpretation: Interpretation | None
    issues: tuple[ValidationIssue, ...]
    features: ValidationFeatures

    @property
    def passed(self) -> bool:
        return self.interpretation is not None and not any(issue.blocking for issue in self.issues)


_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)
_DISALLOWED_CONTEXTS = {
    DialogueContext.PROMPTED_AGREEMENT,
    DialogueContext.QUOTATION,
    DialogueContext.BRAINSTORMING,
    DialogueContext.INSTRUCTION,
    DialogueContext.HUMOUR_OR_SARCASM,
    DialogueContext.HYPOTHETICAL,
    DialogueContext.UNKNOWN,
}
_DISALLOWED_MODALITIES = {
    ObservationModality.HYPOTHETICAL,
    ObservationModality.QUOTED,
    ObservationModality.HUMOROUS_OR_SARCASTIC,
    ObservationModality.UNKNOWN,
}


class InterpretationValidator:
    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()

    def validate(
        self,
        candidate: InterpretationCandidate,
        *,
        generation_id: str,
        observations: Iterable[Observation],
        episodes: Iterable[ConversationEpisode],
        correlations: Iterable[EpisodeCorrelation],
        people: Iterable[Person] = (),
        projects: Iterable[Project] = (),
        deleted_observation_ids: frozenset[str] = frozenset(),
        owner_review: OwnerInterpretationReview | None = None,
        validated_at: datetime,
    ) -> ValidationResult:
        if validated_at.tzinfo is None or validated_at.utcoffset() is None:
            raise ValueError("validated_at must be timezone-aware")
        observation_by_id = self._index(observations, "observation")
        episode_by_id = self._index(episodes, "episode")
        correlation_by_episode: dict[str, EpisodeCorrelation] = {}
        group_by_correlation: dict[str, str] = {}
        person_ids = {person.id for person in people}
        project_ids = {project.id for project in projects}
        issues: list[ValidationIssue] = []
        for correlation in correlations:
            prior = correlation_by_episode.get(correlation.episode_id)
            if prior is not None and prior != correlation:
                issues.append(
                    self._issue(
                        "conflicting_correlation",
                        "one episode has conflicting independence metadata",
                    )
                )
            correlation_by_episode[correlation.episode_id] = correlation
            if correlation.correlation_id is not None:
                prior_group = group_by_correlation.get(correlation.correlation_id)
                if prior_group is not None and prior_group != correlation.independence_group_id:
                    issues.append(
                        self._issue(
                            "correlation_split",
                            "one correlated decision was split into multiple support groups",
                        )
                    )
                group_by_correlation[correlation.correlation_id] = correlation.independence_group_id

        if candidate.generation_id != generation_id:
            issues.append(
                self._issue("wrong_generation", "candidate is not in the pinned generation")
            )
        if self.policy.calibration_manifest_hash is None:
            issues.append(
                self._issue(
                    "calibration_not_frozen",
                    "interpretation readiness requires a frozen owner-reviewed calibration",
                )
            )
        if owner_review is not None and owner_review.candidate_id != candidate.id:
            issues.append(
                self._issue("wrong_owner_review", "owner review targets another candidate")
            )
            owner_review = None
        owner_judgment = owner_review.judgment if owner_review else None
        if owner_judgment == OwnerJudgment.REJECT:
            issues.append(self._issue("owner_rejected", "the archive owner rejected this reading"))
        for prior in candidate.validation_issues:
            issues.append(self._issue("upstream_validation_issue", prior))

        support_assignments = tuple(
            item for item in candidate.evidence_assignments if item.role == EvidenceRole.SUPPORTS
        )
        contradiction_assignments = tuple(
            item for item in candidate.evidence_assignments if item.role == EvidenceRole.CONTRADICTS
        )
        selected_ids = tuple(
            observation_id
            for assignment in candidate.evidence_assignments
            for observation_id in assignment.observation_ids
        )
        if len(set(selected_ids)) != len(selected_ids):
            issues.append(
                self._issue(
                    "duplicate_evidence_role", "one observation has multiple evidence assignments"
                )
            )

        valid_support: list[Observation] = []
        valid_contradictions: list[Observation] = []
        group_observations: dict[str, list[Observation]] = {}
        for assignment in candidate.evidence_assignments:
            episode = episode_by_id.get(assignment.episode_id)
            correlation = correlation_by_episode.get(assignment.episode_id)
            if episode is None or episode.generation_id != generation_id:
                issues.append(
                    self._issue(
                        "missing_episode",
                        f"episode {assignment.episode_id} is absent from the pinned generation",
                    )
                )
                continue
            if correlation is None:
                issues.append(
                    self._issue(
                        "missing_correlation",
                        f"episode {assignment.episode_id} lacks independence metadata",
                    )
                )
                continue
            if assignment.independence_group_id != correlation.independence_group_id:
                issues.append(
                    self._issue(
                        "independence_mismatch",
                        "evidence assignment conflicts with the compiler's decision grouping",
                    )
                )
            if (
                episode.correlation_id is not None
                and episode.correlation_id != correlation.correlation_id
            ):
                issues.append(
                    self._issue(
                        "episode_correlation_mismatch",
                        "episode and compiler correlation identifiers disagree",
                    )
                )
            if episode.completeness != ContextCompleteness.COMPLETE:
                issues.append(
                    self._issue(
                        "incomplete_context",
                        "durable interpretation evidence has omitted or unknown episode context",
                    )
                )
            episode_entries = {turn.entry_id for turn in episode.turns}
            for observation_id in assignment.observation_ids:
                observation = observation_by_id.get(observation_id)
                if observation is None or observation_id in deleted_observation_ids:
                    issues.append(
                        self._issue(
                            "missing_observation",
                            f"observation {observation_id} is missing or deleted",
                        )
                    )
                    continue
                if observation.generation_id != generation_id:
                    issues.append(
                        self._issue(
                            "wrong_observation_generation",
                            f"observation {observation_id} is outside the pinned generation",
                        )
                    )
                if observation.episode_id != assignment.episode_id:
                    issues.append(
                        self._issue(
                            "episode_membership_mismatch",
                            f"observation {observation_id} is assigned to another episode",
                        )
                    )
                if any(span.entry_id not in episode_entries for span in observation.spans):
                    issues.append(
                        self._issue(
                            "span_outside_episode",
                            f"observation {observation_id} cites a turn outside its episode",
                        )
                    )
                if (
                    observation.speaker_attribution != AttributionState.RESOLVED
                    or observation.subject_attribution != AttributionState.RESOLVED
                ):
                    issues.append(
                        self._issue(
                            "unresolved_attribution",
                            f"observation {observation_id} has unresolved speaker or subject",
                        )
                    )
                if observation.sensitivity not in self.policy.allowed_sensitivities:
                    issues.append(
                        self._issue(
                            "sensitivity_not_allowed",
                            f"observation {observation_id} has unclassified or "
                            "disallowed sensitivity",
                        )
                    )
                group_observations.setdefault(assignment.independence_group_id, []).append(
                    observation
                )
                if assignment.role == EvidenceRole.SUPPORTS:
                    valid_support.append(observation)
                elif assignment.role == EvidenceRole.CONTRADICTS:
                    valid_contradictions.append(observation)

        evidence_ref_ids = {
            reference.observation_id for reference in candidate.evidence if reference.observation_id
        }
        if evidence_ref_ids != set(selected_ids):
            issues.append(
                self._issue(
                    "evidence_reference_mismatch",
                    "candidate evidence references do not exactly cover assigned observations",
                )
            )
        expected_assignment = {
            observation_id: (
                assignment.role,
                assignment.episode_id,
                assignment.independence_group_id,
            )
            for assignment in candidate.evidence_assignments
            for observation_id in assignment.observation_ids
        }
        for reference in candidate.evidence:
            if reference.observation_id is None:
                continue
            if expected_assignment.get(reference.observation_id) != (
                reference.role,
                reference.episode_id,
                reference.independence_group_id,
            ):
                issues.append(
                    self._issue(
                        "evidence_role_mismatch",
                        "evidence reference role, episode, or independence group differs",
                    )
                )

        support_groups = {item.independence_group_id for item in support_assignments}
        contradiction_groups = {item.independence_group_id for item in contradiction_assignments}
        minimum = (
            self.policy.min_global_independent_support
            if candidate.scope.kind == ScopeKind.GLOBAL
            else self.policy.min_independent_support
        )
        if len(support_groups) < minimum:
            issues.append(
                self._issue(
                    "insufficient_independence",
                    f"requires {minimum} independent support groups, found {len(support_groups)}",
                )
            )

        qualifying_groups = {
            group_id
            for group_id in support_groups
            if any(self._qualifies(item) for item in group_observations.get(group_id, ()))
        }
        if len(qualifying_groups) < minimum:
            issues.append(
                self._issue(
                    "weak_dialogue_evidence",
                    "prompted, quoted, hypothetical, joking, instructional, or "
                    "low-stakes evidence cannot license a durable reading",
                )
            )

        support_correlations = tuple(
            correlation_by_episode[item.episode_id]
            for item in support_assignments
            if item.episode_id in correlation_by_episode
        )
        contexts = {
            (
                episode_by_id[item.episode_id].source,
                episode_by_id[item.episode_id].source_thread_id,
                item.domain,
                item.project_id,
                item.relationship_person_ids,
            )
            for item in support_correlations
            if item.episode_id in episode_by_id
        }
        if len(contexts) < self.policy.min_contexts:
            issues.append(
                self._issue(
                    "insufficient_context_diversity",
                    f"requires {self.policy.min_contexts} distinct contexts, found {len(contexts)}",
                )
            )
        domains = {item.domain for item in support_correlations if item.domain}
        self._validate_scope(
            candidate, support_correlations, domains, person_ids, project_ids, issues
        )

        if candidate.countersearch_state != CountersearchState.COMPLETE or (
            candidate.countersearch_manifest is None
            or not candidate.countersearch_manifest.completed
        ):
            issues.append(
                self._issue("countersearch_incomplete", "symmetric countersearch is not complete")
            )
        else:
            support_lane = candidate.countersearch_manifest.support
            counter_lane = candidate.countersearch_manifest.counter
            if support_lane.query_fingerprint == counter_lane.query_fingerprint:
                issues.append(
                    self._issue(
                        "countersearch_lanes_collapsed",
                        "support and contradiction searches have the same query fingerprint",
                    )
                )
            if (
                support_lane.query_variants != counter_lane.query_variants
                or support_lane.max_candidates != counter_lane.max_candidates
                or support_lane.retrieval_version != counter_lane.retrieval_version
                or support_lane.candidates_considered != counter_lane.candidates_considered
                or support_lane.candidate_pool_hash != counter_lane.candidate_pool_hash
            ):
                issues.append(
                    self._issue(
                        "countersearch_asymmetric",
                        "support and contradiction search breadth or version differs",
                    )
                )
        if not candidate.alternatives:
            issues.append(
                self._issue("missing_alternatives", "a credible rival explanation is required")
            )
        for alternative in candidate.alternatives:
            if any(
                evidence_id not in observation_by_id
                or evidence_id in deleted_observation_ids
                or evidence_id not in set(selected_ids)
                for evidence_id in alternative.evidence_ids
            ):
                issues.append(
                    self._issue(
                        "alternative_evidence_missing",
                        "an alternative cites absent, deleted, or unselected evidence",
                    )
                )

        contradiction_ratio = len(contradiction_groups) / max(1, len(support_groups))
        if contradiction_ratio > self.policy.max_contradiction_ratio:
            issues.append(
                self._issue(
                    "unresolved_contradiction",
                    "counterevidence is too extensive for this one-sided reading",
                )
            )

        times = sorted(
            value
            for observation in valid_support
            for value in (
                observation.temporal.stated_at,
                observation.temporal.observed_at,
                observation.temporal.valid_from,
            )
            if value is not None
        )
        temporal_days = (times[-1] - times[0]).days if len(times) > 1 else 0
        self._validate_time(
            candidate, support_correlations, times, temporal_days, contradiction_groups, issues
        )

        paraphrase_groups = {
            group_id
            for group_id in support_groups
            if any(
                self._similarity(candidate.hypothesis, item.proposition)
                >= self.policy.near_paraphrase_similarity
                for item in group_observations.get(group_id, ())
            )
        }
        paraphrase_concentration = len(paraphrase_groups) / max(1, len(support_groups))
        if paraphrase_concentration > self.policy.max_near_paraphrase_concentration:
            issues.append(
                self._issue(
                    "near_source_paraphrase_concentration",
                    "the proposed higher-order reading mostly repeats source wording",
                )
            )

        features = ValidationFeatures(
            independent_support_groups=len(support_groups),
            qualifying_support_groups=len(qualifying_groups),
            contradiction_groups=len(contradiction_groups),
            context_count=len(contexts),
            domain_count=len(domains),
            temporal_span_days=max(0, temporal_days),
            near_paraphrase_concentration=paraphrase_concentration,
            owner_judgment=owner_judgment,
        )
        confidence = self._confidence(features)
        if confidence < self.policy.ready_confidence_floor:
            issues.append(
                self._issue(
                    "calibrated_confidence_below_floor",
                    f"calibrated confidence {confidence:.3f} is below the ready floor",
                )
            )
        blocking = any(issue.blocking for issue in issues)
        if blocking:
            staged = candidate.model_copy(
                update={"validation_issues": tuple(dict.fromkeys(issue.code for issue in issues))}
            )
            return ValidationResult(
                candidate=staged,
                interpretation=None,
                issues=tuple(issues),
                features=features,
            )

        readiness = (
            Readiness.LIMITED if owner_judgment == OwnerJudgment.PARTIAL else Readiness.READY
        )
        # A judgment changes presentation state, never the evidential basis.
        derivation = DerivationKind.INFERRED
        strongest_id = next(
            (
                assignment.observation_ids[0]
                for assignment in contradiction_assignments
                if "strongest_counterexample" in assignment.strength_features
            ),
            None,
        )
        strongest_observation = (
            observation_by_id.get(strongest_id) if strongest_id is not None else None
        )
        if strongest_observation is None and valid_contradictions:
            strongest_observation = valid_contradictions[0]
        strongest = strongest_observation.proposition if strongest_observation is not None else None
        counter_summary = (
            f"{len(contradiction_groups)} independent counterevidence group(s) were retained."
            if valid_contradictions
            else "No contradicting observation was selected by the completed symmetric search."
        )
        derivation_summary = (
            f"Validated {len(support_groups)} independent support groups across "
            f"{len(contexts)} contexts; retained {len(contradiction_groups)} counterevidence "
            f"groups and {len(candidate.alternatives)} alternative explanation(s)."
        )
        interpretation_id = derived_record_id(
            "interpretation",
            canonical_subject=candidate.id,
            canonical_predicate=f"{candidate.kind.value}:{candidate.concept_key}",
            evidence_membership=interpretation_identity_membership(
                candidate.evidence_assignments,
                observation_by_id,
                episode_by_id,
            ),
            identity_version=self.policy.version,
        )
        interpretation = Interpretation(
            id=interpretation_id,
            generation_id=generation_id,
            evidence=candidate.evidence,
            claim_ids=candidate.claim_ids,
            temporal=candidate.temporal,
            confidence=confidence,
            sensitivity=candidate.sensitivity,
            derivation=derivation,
            producer=candidate.producer,
            readiness=readiness,
            created_at=validated_at,
            concept_key=candidate.concept_key,
            thesis=candidate.hypothesis,
            kind=candidate.kind,
            scope=candidate.scope,
            evidence_assignments=candidate.evidence_assignments,
            alternatives=candidate.alternatives,
            missing_evidence=candidate.missing_evidence,
            counterevidence_summary=counter_summary,
            strongest_counterexample=strongest,
            countersearch_state=candidate.countersearch_state,
            countersearch_manifest=candidate.countersearch_manifest,
            derivation_summary=derivation_summary,
            candidate_id=candidate.id,
        )
        return ValidationResult(
            candidate=candidate,
            interpretation=interpretation,
            issues=tuple(issues),
            features=features,
        )

    def _validate_scope(
        self,
        candidate: InterpretationCandidate,
        contexts: tuple[EpisodeCorrelation, ...],
        domains: set[str],
        person_ids: set[str],
        project_ids: set[str],
        issues: list[ValidationIssue],
    ) -> None:
        scope = candidate.scope
        if (
            candidate.kind == InterpretationKind.RELATIONSHIP
            and scope.kind != ScopeKind.RELATIONSHIP
        ):
            issues.append(
                self._issue(
                    "relationship_read_requires_relationship_scope",
                    "a relationship interpretation must name the relationship it describes",
                )
            )
        if scope.kind == ScopeKind.GLOBAL:
            if len(domains) < self.policy.min_global_domains:
                issues.append(
                    self._issue(
                        "global_scope_without_cross_domain_support",
                        f"global scope requires {self.policy.min_global_domains} "
                        "represented domains",
                    )
                )
            if any(item.domain is None for item in contexts):
                issues.append(
                    self._issue(
                        "global_scope_unknown_domain",
                        "global evidence must have explicit domain context",
                    )
                )
        elif scope.kind == ScopeKind.DOMAIN:
            if any(item.domain != scope.domain for item in contexts):
                issues.append(
                    self._issue(
                        "domain_scope_mismatch", "support evidence leaves the proposed domain"
                    )
                )
        elif scope.kind == ScopeKind.PROJECT:
            if scope.project_id not in project_ids:
                issues.append(
                    self._issue(
                        "unknown_scope_project",
                        "project scope does not resolve in the pinned generation",
                    )
                )
            if any(item.project_id != scope.project_id for item in contexts):
                issues.append(
                    self._issue(
                        "project_scope_mismatch", "support evidence leaves the proposed project"
                    )
                )
        elif scope.kind == ScopeKind.RELATIONSHIP:
            assert scope.relationship_person_ids is not None
            if not set(scope.relationship_person_ids).issubset(person_ids):
                issues.append(
                    self._issue(
                        "unknown_scope_person",
                        "relationship scope does not resolve in the pinned generation",
                    )
                )
            expected = frozenset(scope.relationship_person_ids)
            if any(
                item.relationship_person_ids is None
                or frozenset(item.relationship_person_ids) != expected
                for item in contexts
            ):
                issues.append(
                    self._issue(
                        "relationship_scope_mismatch",
                        "support evidence leaves the proposed relationship",
                    )
                )

    def _validate_time(
        self,
        candidate: InterpretationCandidate,
        contexts: tuple[EpisodeCorrelation, ...],
        times: list[datetime],
        temporal_days: int,
        contradiction_groups: set[str],
        issues: list[ValidationIssue],
    ) -> None:
        scope = candidate.scope
        if scope.kind == ScopeKind.TIME_PERIOD:
            for value in times:
                if scope.valid_from and value < scope.valid_from:
                    issues.append(
                        self._issue("time_scope_mismatch", "support predates the proposed period")
                    )
                    break
                if scope.valid_to and value > scope.valid_to:
                    issues.append(
                        self._issue("time_scope_mismatch", "support follows the proposed period")
                    )
                    break
        if candidate.kind == InterpretationKind.CHANGE_OVER_TIME:
            if temporal_days < self.policy.min_change_days:
                issues.append(
                    self._issue(
                        "insufficient_change_interval",
                        f"change-over-time requires at least {self.policy.min_change_days} days",
                    )
                )
            if not contradiction_groups:
                issues.append(
                    self._issue(
                        "change_without_counterperiod",
                        "change-over-time requires evidence of a different period",
                    )
                )
        elif scope.kind != ScopeKind.TIME_PERIOD and contexts:
            dated = {item.decision_at.date() for item in contexts if item.decision_at}
            if len(dated) == 1 and candidate.kind in {
                InterpretationKind.VALUE,
                InterpretationKind.PERSISTENT_OBJECTIVE,
            }:
                issues.append(
                    self._issue(
                        "single_period_durable_claim",
                        "a durable value or objective requires support over time",
                    )
                )

    @staticmethod
    def _qualifies(observation: Observation) -> bool:
        if observation.dialogue_context in _DISALLOWED_CONTEXTS:
            return False
        if observation.modality in _DISALLOWED_MODALITIES:
            return False
        if observation.kind in {
            ObservationKind.CHOICE,
            ObservationKind.TRADEOFF,
            ObservationKind.BOUNDARY,
        }:
            return True
        if (
            observation.kind == ObservationKind.REASON
            and observation.dialogue_context == DialogueContext.VOLUNTEERED_REASONING
        ):
            return True
        return (
            observation.kind == ObservationKind.COMMITMENT
            and observation.cost_or_stakes_literal is not None
        )

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens = {token.casefold() for token in _TOKEN.findall(left)}
        right_tokens = {token.casefold() for token in _TOKEN.findall(right)}
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @staticmethod
    def _index(items: Iterable[object], label: str) -> dict[str, object]:
        result: dict[str, object] = {}
        for item in items:
            item_id = item.id
            if item_id in result and result[item_id] != item:
                raise ValueError(f"conflicting {label} records for id {item_id}")
            result[item_id] = item
        return result

    @staticmethod
    def _issue(code: str, detail: str) -> ValidationIssue:
        return ValidationIssue(code=code, detail=detail)

    def _confidence(self, features: ValidationFeatures) -> float:
        score = self.policy.base_weight
        score += min(features.independent_support_groups, 5) * self.policy.independent_group_weight
        score += min(features.qualifying_support_groups, 4) * self.policy.qualifying_group_weight
        score += min(features.context_count, 4) * self.policy.context_weight
        score += min(features.domain_count, 3) * self.policy.domain_weight
        score += min(features.temporal_span_days, 365) / 365 * self.policy.temporal_span_weight
        score -= min(features.contradiction_groups, 3) * self.policy.contradiction_penalty
        score -= features.near_paraphrase_concentration * self.policy.paraphrase_penalty
        return round(max(0.0, min(1.0, score)), 3)


__all__ = [
    "InterpretationValidator",
    "OwnerInterpretationReview",
    "OwnerJudgment",
    "VALIDATION_POLICY_VERSION",
    "ValidationFeatures",
    "ValidationIssue",
    "ValidationPolicy",
    "ValidationResult",
]
