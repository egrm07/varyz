"""Independent trust gates for one staged personal-model generation.

These gates judge the compiled model, not the amount of archive underneath it.
The Mirror contract already owns evidence volume, authored-content volume, temporal
spread and active-day floors.  Repeating those here would let a large archive hide
the more dangerous failures: a quote assigned to its recipient, two same-named
people merged, or one conversation counted as several independent reasons.

``GATE_DEFINITIONS`` is the whole numeric contract.  Every threshold is stated
once, beside one rationale tied to the archive failure that requires it.  The
evaluator has no early return: an absent sample is ``unavailable`` for that gate,
and a shortfall is ``limited`` without changing any other result or blocking
activation.  Fact attribution is additionally reported per fact, because an
explicit, literally sourced birthday can be Ready even while its Person record
and the wider generation remain Limited.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from varyz.domain import Entry
from varyz.personal_model.episodes import EpisodeBuildResult
from varyz.personal_model.identity import IdentityResolutionResult, SourcePerson
from varyz.personal_model.memory import AtomicClaimInput
from varyz.personal_model.models import (
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
    CountersearchState,
    DerivationKind,
    DialogueContext,
    EntityKind,
    Fact,
    FactStatus,
    Goal,
    Interpretation,
    InterpretationCandidate,
    Observation,
    ObservationModality,
    Person,
    Preference,
    Project,
    Readiness,
    RelationshipSnapshot,
    ScopeKind,
)
from varyz.personal_model.projects import ProjectCompilation, ProjectEvidenceCandidate
from varyz.personal_model.repository import PersonalModelRecord, record_kind


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class GateVerdict(StrEnum):
    READY = "ready"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class ThresholdDirection(StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


@dataclass(frozen=True)
class GateDefinition:
    key: str
    threshold: float
    direction: ThresholdDirection
    unit: str
    failure: str
    measured_as: str
    rationale: str


GATE_DEFINITIONS: tuple[GateDefinition, ...] = (
    GateDefinition(
        key="episode_context_completeness",
        threshold=0.995,
        direction=ThresholdDirection.AT_LEAST,
        unit="complete target rate",
        failure=(
            "Turns would be interpreted without the target or reply ancestors that give them "
            "meaning."
        ),
        measured_as=(
            "Requested turns whose complete episode contains the intact target and every "
            "resolvable reply ancestor, divided by all requested turns."
        ),
        rationale=(
            "The live archive produced 108 quarantined targets while expanding 45,607 entries "
            "into 114,173 windows, so allowing at most one broken target in 200 exposes that "
            "failure without demanding impossible connector perfection."
        ),
    ),
    GateDefinition(
        key="observation_attribution",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="correctly attributed observation rate",
        failure=(
            "Observations would quote text outside their episode or assign another person's "
            "words to the attributed speaker."
        ),
        measured_as=(
            "Observations whose every literal span round-trips inside their episode and whose "
            "connector author resolves to the recorded speaker; quoted speech never qualifies."
        ),
        rationale=(
            "A live-path fixture once paired observations to episodes by list position and "
            "created span-outside-episode attribution, so even one surviving mismatch is too many."
        ),
    ),
    GateDefinition(
        key="person_resolution_coverage",
        threshold=0.98,
        direction=ThresholdDirection.AT_LEAST,
        unit="resolved identity rate",
        failure=(
            "The model would leave source identities unresolved or silently treat a weak "
            "same-name near-match as one person."
        ),
        measured_as=(
            "Resolved source identities divided by source identities plus unresolved near-merge "
            "review pairs; a shared display name contributes only a review pair.  Coverage, not "
            "precision: it counts how many identities were resolved, never whether they were "
            "resolved correctly.  Precision would need adjudicated merges and splits to score "
            "against, which this gate does not have."
        ),
        rationale=(
            "The live archive resolves 172 people and contains repeated names, so a two-percent "
            "review allowance keeps ambiguity visible instead of turning names into identity edges."
        ),
    ),
    GateDefinition(
        key="project_consolidation",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="unambiguous project-reference rate",
        failure=(
            "One project would appear under several records, or conflicting projects would be "
            "collapsed into one."
        ),
        measured_as=(
            "Project candidates mapped once per canonical key without conflicting durable "
            "identifiers, penalising every unresolved near-merge review pair."
        ),
        rationale=(
            "The live semantic path emitted one candidate per repeated project proposal despite a "
            "shared canonical key, so every staged project reference must consolidate "
            "unambiguously."
        ),
    ),
    GateDefinition(
        key="fact_attribution",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="literally attributed fact rate",
        failure=(
            "Facts would be attached to someone other than the person or project evidenced by "
            "their literal source span."
        ),
        measured_as=(
            "Active facts whose source span round-trips and whose pinned claim or observation "
            "subject exactly equals the fact subject, evaluated both per fact and in aggregate."
        ),
        rationale=(
            "The live acceptance path depends on a single birthday being answerable without "
            "borrowing the speaker's identity, so fact attribution has zero error allowance."
        ),
    ),
    GateDefinition(
        key="higher_order_independent_support",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="independently supported interpretation rate",
        failure=(
            "Higher-order conclusions would rest on one conversation repeated or restated rather "
            "than on at least two independent contexts."
        ),
        measured_as=(
            "Interpretation hypotheses with supporting assignments in at least two distinct "
            "independence groups, divided by all hypotheses."
        ),
        rationale=(
            "The live adversarial case contains twenty turns about one decision, so every "
            "higher-order hypothesis must clear two independent contexts and the generation rate "
            "must be complete."
        ),
    ),
    GateDefinition(
        key="countersearch_completion",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="completed symmetric-search rate",
        failure=(
            "Interpretations would report supporting evidence without completing the equally broad "
            "search for counterevidence."
        ),
        measured_as=(
            "Interpretation hypotheses carrying a completed symmetric countersearch manifest, "
            "divided by all hypotheses."
        ),
        rationale=(
            "The live synthesis acceptance case explicitly failed when only support returned, so "
            "there is no acceptable incomplete-search fraction."
        ),
    ),
    GateDefinition(
        key="scope_correctness",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="bounded-scope interpretation rate",
        failure=(
            "A pattern limited to work, one project, one relationship, or one period would be "
            "presented as an unscoped general truth."
        ),
        measured_as=(
            "Interpretation hypotheses carrying a domain, project, relationship, or time-period "
            "scope, divided by all hypotheses; bare global scope does not qualify."
        ),
        rationale=(
            "The live model previously risked promoting work-only evidence to character, so every "
            "compiled hypothesis must retain a concrete boundary."
        ),
    ),
    GateDefinition(
        key="temporal_correctness",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="temporally consistent record rate",
        failure=(
            "A record's stated or observed time would conflict with its evidence, or a superseded "
            "fact would still be presented as current."
        ),
        measured_as=(
            "Time-bearing records whose evidence contains their stated/observed timestamps and "
            "whose supersession links agree with fact status and readiness."
        ),
        rationale=(
            "The live adversarial set includes conflicts across years and stale-versus-current "
            "facts, so every temporal record must preserve its evidence time and supersession "
            "state."
        ),
    ),
    GateDefinition(
        key="confidence_support_gap",
        threshold=0.10,
        direction=ThresholdDirection.AT_MOST,
        unit="mean confidence overstatement",
        failure=(
            "High-confidence records would be the ones whose realised literal or independent "
            "support is thinnest."
        ),
        measured_as=(
            "Mean positive gap between stated confidence and structural support, where inferred "
            "support reaches one only after two independent groups and literal records require "
            "round-tripping evidence.  A structural diagnostic, not a calibration: it compares "
            "confidence against how much evidence is attached, never against whether the claim "
            "is true, and it is one-sided -- understated confidence scores zero.  Calibration "
            "needs correctness labels, which this gate does not have."
        ),
        rationale=(
            "The live model assigned fluent confidence before its support was calibrated, so mean "
            "overstatement is capped at ten percentage points rather than trusting self-ratings."
        ),
    ),
    GateDefinition(
        key="life_lookup_accuracy",
        threshold=1.0,
        direction=ThresholdDirection.AT_LEAST,
        unit="offline typed-lookup accuracy",
        failure=(
            "A typed life lookup would return a record other than the one its literal evidence "
            "supports."
        ),
        measured_as=(
            "Offline typed fixtures whose direct record projection matches kind, subject, "
            "predicate and expected evidence entry, divided by all fixtures."
        ),
        rationale=(
            "The live acceptance demonstration requires ordinary birthday, preference, event and "
            "project lookups, so every frozen typed fixture must return its evidenced record."
        ),
    ),
    GateDefinition(
        key="near_source_paraphrase_concentration",
        threshold=0.60,
        direction=ThresholdDirection.AT_MOST,
        unit="source-token concentration",
        failure=(
            "The higher-order model would mostly repeat archive wording with small edits instead "
            "of adding a supported abstraction."
        ),
        measured_as=(
            "Source-token overlap in higher-order hypothesis text, weighted by hypothesis token "
            "count; explicit facts and observations are excluded because literal fidelity is "
            "their job."
        ),
        rationale=(
            "The live model's useful output must add meaning beyond retrieved prose, so at least "
            "forty percent of higher-order wording must not merely echo its source spans."
        ),
    ),
)

GATE_KEYS: tuple[str, ...] = tuple(item.key for item in GATE_DEFINITIONS)
_DEFINITION_BY_KEY = {item.key: item for item in GATE_DEFINITIONS}


class GateResult(_FrozenModel):
    gate: str = Field(min_length=1)
    verdict: GateVerdict
    measured_value: float | None = Field(default=None, ge=0)
    threshold: float = Field(ge=0)
    direction: ThresholdDirection
    unit: str = Field(min_length=1)
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    failure: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unavailable_has_no_measurement(self) -> GateResult:
        if self.verdict == GateVerdict.UNAVAILABLE:
            if self.measured_value is not None or self.denominator:
                raise ValueError("unavailable gates cannot claim a measurement")
        elif self.measured_value is None or self.denominator == 0:
            raise ValueError("measured gates require a value and denominator")
        return self


class FactRecordGateResult(_FrozenModel):
    record_id: str = Field(min_length=1)
    verdict: GateVerdict
    measured_value: float = Field(ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    failure: str = Field(min_length=1)
    reasons: tuple[str, ...] = ()


class LifeLookupFixture(_FrozenModel):
    """One network-free typed lookup expectation over already compiled records."""

    fixture_id: str = Field(min_length=1)
    record_kind: Literal[
        "person",
        "project",
        "goal",
        "fact",
        "preference",
        "event",
        "relationship_snapshot",
        "interpretation",
    ]
    entity_kind: EntityKind | None = None
    entity_id: str | None = Field(default=None, min_length=1)
    predicate: str | None = Field(default=None, min_length=1)
    expected_record_id: str = Field(min_length=1)
    expected_evidence_entry_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def paired_entity_selector(self) -> LifeLookupFixture:
        if (self.entity_kind is None) != (self.entity_id is None):
            raise ValueError("typed lookup entity kind and id must be supplied together")
        return self


class GenerationReadiness(_FrozenModel):
    gates: tuple[GateResult, ...]
    fact_records: tuple[FactRecordGateResult, ...] = ()

    @model_validator(mode="after")
    def exactly_one_of_each_gate(self) -> GenerationReadiness:
        keys = tuple(item.gate for item in self.gates)
        if keys != GATE_KEYS:
            raise ValueError("generation readiness must contain all twelve gates in contract order")
        fact_ids = tuple(item.record_id for item in self.fact_records)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("per-record fact gate results must be unique")
        return self

    @property
    def by_gate(self) -> Mapping[str, GateResult]:
        return {item.gate: item for item in self.gates}

    @property
    def verdict_counts(self) -> dict[str, int]:
        counts = {str(item): 0 for item in GateVerdict}
        for item in self.gates:
            counts[str(item.verdict)] += 1
        return counts


@dataclass(frozen=True)
class ReadinessInputs:
    entries: Sequence[Entry]
    episodes: EpisodeBuildResult
    observations: Sequence[Observation]
    identity: IdentityResolutionResult
    source_people: Sequence[SourcePerson]
    projects: ProjectCompilation
    project_candidates: Sequence[ProjectEvidenceCandidate]
    atomic_claims: Sequence[AtomicClaimInput]
    records: Sequence[PersonalModelRecord]
    lookup_fixtures: Sequence[LifeLookupFixture] = ()


class PersonalModelReadinessEvaluator:
    """Compute all gates from one pinned, in-memory staged generation."""

    def evaluate(self, inputs: ReadinessInputs) -> GenerationReadiness:
        entries = {item.id: item for item in inputs.entries}
        episodes = {item.id: item for item in inputs.episodes.episodes}
        observations = {item.id: item for item in inputs.observations}
        claims = {item.id: item for item in inputs.atomic_claims}
        facts = tuple(item for item in inputs.records if isinstance(item, Fact))
        fact_records = tuple(
            self._fact_record(item, entries, observations, claims)
            for item in sorted(facts, key=lambda fact: fact.id)
        )
        gates = (
            self._episode_gate(inputs, entries, episodes),
            self._observation_gate(inputs, entries, episodes),
            self._person_gate(inputs),
            self._project_gate(inputs),
            self._fact_gate(fact_records),
            self._independent_support_gate(inputs.records),
            self._countersearch_gate(inputs.records),
            self._scope_gate(inputs.records),
            self._temporal_gate(inputs.records, entries),
            self._calibration_gate(inputs.records, fact_records, entries),
            self._lookup_gate(inputs, entries),
            self._paraphrase_gate(inputs.records, observations, entries),
        )
        return GenerationReadiness(gates=gates, fact_records=fact_records)

    def _episode_gate(
        self,
        inputs: ReadinessInputs,
        entries: Mapping[str, Entry],
        episodes: Mapping[str, ConversationEpisode],
    ) -> GateResult:
        good = 0
        stranded: list[str] = []
        for coverage in inputs.episodes.target_coverage:
            represented = [episodes[item] for item in coverage.episode_ids if item in episodes]
            intact = (
                not coverage.quarantined
                and coverage.target_entry_id in coverage.covered_entry_ids
                and any(
                    episode.completeness == ContextCompleteness.COMPLETE
                    and self._reply_ancestors_intact(
                        coverage.target_entry_id, episode, entries
                    )
                    for episode in represented
                )
            )
            if intact:
                good += 1
            else:
                stranded.append(coverage.target_entry_id)
        return self._rate_result(
            "episode_context_completeness",
            good,
            len(inputs.episodes.target_coverage),
            {"stranded_target_ids": stranded},
        )

    @staticmethod
    def _reply_ancestors_intact(
        target_id: str,
        episode: ConversationEpisode,
        entries: Mapping[str, Entry],
    ) -> bool:
        member_ids = {item.entry_id for item in episode.turns}
        if target_id not in member_ids:
            return False
        source_turn_ids = {
            item.source_turn_id: item.entry_id
            for item in episode.turns
            if item.source_turn_id is not None
        }
        seen: set[str] = set()
        current = target_id
        while current not in seen:
            seen.add(current)
            entry = entries.get(current)
            if entry is None:
                return False
            reply = entry.conversation.reply_to_id
            if reply is None:
                return True
            ancestor_id = reply if reply in entries else source_turn_ids.get(reply)
            if ancestor_id is None or ancestor_id not in member_ids:
                return False
            current = ancestor_id
        return False

    def _observation_gate(
        self,
        inputs: ReadinessInputs,
        entries: Mapping[str, Entry],
        episodes: Mapping[str, ConversationEpisode],
    ) -> GateResult:
        resolutions = {
            (item.source, item.source_account, item.source_key): item.entity_id
            for item in inputs.identity.source_entity_resolutions
            if item.kind == EntityKind.PERSON
        }
        good = 0
        invalid: list[str] = []
        for observation in inputs.observations:
            episode = episodes.get(observation.episode_id)
            episode_entries = (
                {item.entry_id for item in episode.turns} if episode is not None else set()
            )
            valid = bool(observation.spans) and episode is not None
            if observation.dialogue_context == DialogueContext.QUOTATION or (
                observation.modality == ObservationModality.QUOTED
            ):
                valid = False
            for span in observation.spans:
                entry = entries.get(span.entry_id)
                source_key = self._entry_source_key(entry) if entry is not None else None
                valid = valid and bool(
                    entry is not None
                    and span.entry_id in episode_entries
                    and span.end <= len(entry.body)
                    and entry.body[span.start : span.end] == span.exact_literal
                    and source_key == observation.speaker_source_key
                    and resolutions.get((entry.source, entry.source_account, source_key or ""))
                    == observation.speaker_person_id
                )
            valid = valid and bool(
                observation.speaker_attribution == AttributionState.RESOLVED
                and observation.speaker_person_id
            )
            if valid:
                good += 1
            else:
                invalid.append(observation.id)
        return self._rate_result(
            "observation_attribution",
            good,
            len(inputs.observations),
            {"invalid_observation_ids": invalid},
        )

    def _person_gate(self, inputs: ReadinessInputs) -> GateResult:
        resolved = sum(
            item.identity_key in inputs.identity.source_identity_to_person_id
            for item in inputs.source_people
        )
        near_merges = len(inputs.identity.review_candidates)
        denominator = len(inputs.source_people) + near_merges
        return self._rate_result(
            "person_resolution_coverage",
            resolved,
            denominator,
            {
                "source_identities": len(inputs.source_people),
                "resolved_source_identities": resolved,
                "unresolved_near_merge_pairs": near_merges,
            },
        )

    def _project_gate(self, inputs: ReadinessInputs) -> GateResult:
        candidates = {item.candidate_id: item for item in inputs.project_candidates}
        mapping = inputs.projects.project_candidate_to_project_id
        project_ids_by_key: dict[str, set[str]] = defaultdict(set)
        for candidate_id, candidate in candidates.items():
            project_id = mapping.get(candidate_id)
            if project_id is not None:
                project_ids_by_key[candidate.canonical_value_key].add(project_id)
        dirty_ids = {
            candidate_id
            for key, project_ids in project_ids_by_key.items()
            if len(project_ids) != 1
            for candidate_id, candidate in candidates.items()
            if candidate.canonical_value_key == key
        }
        candidates_by_project: dict[str, list[ProjectEvidenceCandidate]] = defaultdict(list)
        for candidate_id, project_id in mapping.items():
            if candidate_id in candidates:
                candidates_by_project[project_id].append(candidates[candidate_id])
        for grouped in candidates_by_project.values():
            if self._has_conflicting_project_identifiers(grouped):
                dirty_ids.update(item.candidate_id for item in grouped)
        good = sum(
            candidate_id in mapping and candidate_id not in dirty_ids
            for candidate_id in candidates
        )
        reviews = len(inputs.projects.review_candidates)
        return self._rate_result(
            "project_consolidation",
            good,
            len(candidates) + reviews,
            {
                "project_references": len(candidates),
                "unresolved_review_pairs": reviews,
                "ambiguous_candidate_ids": sorted(dirty_ids),
            },
        )

    @staticmethod
    def _has_conflicting_project_identifiers(
        candidates: Sequence[ProjectEvidenceCandidate],
    ) -> bool:
        identifiers: dict[object, list[set[str]]] = defaultdict(list)
        for candidate in candidates:
            by_kind: dict[object, set[str]] = defaultdict(set)
            for identifier in candidate.identifiers:
                by_kind[identifier.kind].add(identifier.canonical)
            for kind, values in by_kind.items():
                identifiers[kind].append(values)
        return any(
            left.isdisjoint(right)
            for sets in identifiers.values()
            for index, left in enumerate(sets)
            for right in sets[index + 1 :]
        )

    def _fact_record(
        self,
        fact: Fact,
        entries: Mapping[str, Entry],
        observations: Mapping[str, Observation],
        claims: Mapping[str, AtomicClaimInput],
    ) -> FactRecordGateResult:
        reasons: list[str] = []
        literal = False
        attributed = False
        for reference in fact.evidence:
            span = reference.span
            entry = entries.get(reference.entry_id)
            if (
                span is None
                or entry is None
                or span.end > len(entry.body)
                or entry.body[span.start : span.end] != span.exact_literal
            ):
                continue
            literal = True
            observation = (
                observations.get(reference.observation_id)
                if reference.observation_id is not None
                else None
            )
            claim = claims.get(reference.claim_id) if reference.claim_id is not None else None
            if observation is not None and (
                observation.subject_kind == fact.subject.kind
                and observation.subject_id == fact.subject.id
                and observation.subject_attribution == AttributionState.RESOLVED
            ):
                attributed = True
            if claim is not None and claim.subject == fact.subject:
                attributed = True
        if fact.status != FactStatus.ACTIVE:
            reasons.append("fact_is_not_active")
        if not literal:
            reasons.append("literal_source_span_is_absent")
        if not attributed:
            reasons.append("evidence_subject_does_not_match_fact_subject")
        ready = not reasons
        definition = _DEFINITION_BY_KEY["fact_attribution"]
        return FactRecordGateResult(
            record_id=fact.id,
            verdict=GateVerdict.READY if ready else GateVerdict.LIMITED,
            measured_value=float(ready),
            threshold=definition.threshold,
            failure=definition.failure,
            reasons=tuple(reasons),
        )

    def _fact_gate(self, facts: Sequence[FactRecordGateResult]) -> GateResult:
        return self._rate_result(
            "fact_attribution",
            sum(item.verdict == GateVerdict.READY for item in facts),
            len(facts),
            {
                "limited_fact_ids": [
                    item.record_id for item in facts if item.verdict != GateVerdict.READY
                ]
            },
        )

    def _independent_support_gate(
        self, records: Sequence[PersonalModelRecord]
    ) -> GateResult:
        hypotheses = self._higher_order_records(records)
        independent = [
            item
            for item in hypotheses
            if len(
                {
                    assignment.independence_group_id
                    for assignment in item.evidence_assignments
                    if str(assignment.role) == "supports"
                }
            )
            >= 2
        ]
        return self._rate_result(
            "higher_order_independent_support",
            len(independent),
            len(hypotheses),
            {
                "minimum_independent_contexts": 2,
                "thin_record_ids": sorted(
                    {item.id for item in hypotheses} - {item.id for item in independent}
                ),
            },
        )

    def _countersearch_gate(self, records: Sequence[PersonalModelRecord]) -> GateResult:
        hypotheses = self._higher_order_records(records)
        complete = [
            item
            for item in hypotheses
            if item.countersearch_state == CountersearchState.COMPLETE
            and item.countersearch_manifest is not None
            and item.countersearch_manifest.completed
        ]
        return self._rate_result(
            "countersearch_completion",
            len(complete),
            len(hypotheses),
            {
                "incomplete_record_ids": sorted(
                    {item.id for item in hypotheses} - {item.id for item in complete}
                )
            },
        )

    def _scope_gate(self, records: Sequence[PersonalModelRecord]) -> GateResult:
        hypotheses = self._higher_order_records(records)
        scoped = [item for item in hypotheses if item.scope.kind != ScopeKind.GLOBAL]
        return self._rate_result(
            "scope_correctness",
            len(scoped),
            len(hypotheses),
            {
                "unscoped_record_ids": sorted(
                    {item.id for item in hypotheses} - {item.id for item in scoped}
                )
            },
        )

    def _temporal_gate(
        self, records: Sequence[PersonalModelRecord], entries: Mapping[str, Entry]
    ) -> GateResult:
        superseded_ids = {
            superseded_id
            for item in records
            if isinstance(item, Fact)
            for superseded_id in item.supersedes_fact_ids
        }
        eligible: list[PersonalModelRecord] = []
        valid: list[PersonalModelRecord] = []
        invalid: list[str] = []
        for record in records:
            temporal_values = (
                record.temporal.observed_at,
                record.temporal.stated_at,
                record.temporal.valid_from,
                record.temporal.valid_to,
            )
            has_supersession = isinstance(record, Fact) and (
                record.status == FactStatus.SUPERSEDED
                or record.id in superseded_ids
                or bool(record.supersedes_fact_ids)
            )
            if not any(temporal_values) and not has_supersession:
                continue
            eligible.append(record)
            evidence_times = sorted(
                parsed
                for reference in record.evidence
                if (entry := entries.get(reference.entry_id)) is not None
                if (parsed := self._entry_time(entry)) is not None
            )
            correct = True
            for value in (record.temporal.observed_at, record.temporal.stated_at):
                if value is not None and evidence_times and not (
                    evidence_times[0] <= value <= evidence_times[-1]
                ):
                    correct = False
            if isinstance(record, Fact):
                should_be_superseded = record.id in superseded_ids
                marked_superseded = (
                    record.status == FactStatus.SUPERSEDED
                    and record.readiness == Readiness.SUPERSEDED
                )
                if should_be_superseded != marked_superseded:
                    correct = False
            if correct:
                valid.append(record)
            else:
                invalid.append(record.id)
        return self._rate_result(
            "temporal_correctness",
            len(valid),
            len(eligible),
            {"invalid_record_ids": sorted(invalid)},
        )

    def _calibration_gate(
        self,
        records: Sequence[PersonalModelRecord],
        fact_results: Sequence[FactRecordGateResult],
        entries: Mapping[str, Entry],
    ) -> GateResult:
        fact_support = {item.record_id: item.measured_value for item in fact_results}
        hypotheses = {item.id: item for item in self._higher_order_records(records)}
        samples: list[tuple[str, float]] = []
        for record in records:
            realised: float | None = None
            if isinstance(record, Fact):
                realised = fact_support.get(record.id, 0.0)
            elif record.id in hypotheses:
                groups = {
                    assignment.independence_group_id
                    for assignment in hypotheses[record.id].evidence_assignments
                    if str(assignment.role) == "supports"
                }
                realised = min(1.0, len(groups) / 2)
            elif isinstance(record, (Person, Project, Goal, Preference, RelationshipSnapshot)):
                if record.derivation == DerivationKind.USER_CONFIRMED:
                    realised = 1.0
                elif record.evidence:
                    valid_references = sum(
                        reference.entry_id in entries for reference in record.evidence
                    )
                    realised = valid_references / len(record.evidence)
            if realised is not None:
                samples.append((record.id, max(0.0, record.confidence - realised)))
        definition = _DEFINITION_BY_KEY["confidence_support_gap"]
        if not samples:
            return self._unavailable(definition)
        value = sum(item[1] for item in samples) / len(samples)
        worst = sorted(samples, key=lambda item: (-item[1], item[0]))[:10]
        return self._value_result(
            definition,
            value,
            numerator=sum(gap <= definition.threshold for _, gap in samples),
            denominator=len(samples),
            detail={"largest_overstatements": dict(worst)},
        )

    def _lookup_gate(
        self, inputs: ReadinessInputs, entries: Mapping[str, Entry]
    ) -> GateResult:
        by_id = {item.id: item for item in inputs.records}
        passed: list[str] = []
        failed: list[str] = []
        for fixture in inputs.lookup_fixtures:
            expected = by_id.get(fixture.expected_record_id)
            matches = sorted(
                item.id for item in inputs.records if self._lookup_matches(fixture, item)
            )
            if (
                expected is not None
                and matches == [fixture.expected_record_id]
                and self._lookup_matches(fixture, expected)
            ):
                evidence_ids = {item.entry_id for item in expected.evidence}
                if (
                    fixture.expected_evidence_entry_id in evidence_ids
                    and fixture.expected_evidence_entry_id in entries
                ):
                    passed.append(fixture.fixture_id)
                    continue
            failed.append(fixture.fixture_id)
        return self._rate_result(
            "life_lookup_accuracy",
            len(passed),
            len(inputs.lookup_fixtures),
            {"failed_fixture_ids": failed},
        )

    @staticmethod
    def _lookup_matches(
        fixture: LifeLookupFixture, record: PersonalModelRecord
    ) -> bool:
        if record.readiness not in {Readiness.READY, Readiness.LIMITED}:
            return False
        if isinstance(record, (Fact, Preference)) and record.status != FactStatus.ACTIVE:
            return False
        if record_kind(record) != fixture.record_kind:
            return False
        if fixture.entity_kind is not None:
            if isinstance(record, (Fact, Preference)):
                if (record.subject.kind, record.subject.id) != (
                    fixture.entity_kind,
                    fixture.entity_id,
                ):
                    return False
            elif str(fixture.entity_kind) != fixture.record_kind or record.id != fixture.entity_id:
                return False
        if fixture.predicate is None:
            return True
        if isinstance(record, (Fact, Preference)):
            return record.predicate == fixture.predicate
        if isinstance(record, Interpretation):
            return record.concept_key == fixture.predicate
        return False

    def _paraphrase_gate(
        self,
        records: Sequence[PersonalModelRecord],
        observations: Mapping[str, Observation],
        entries: Mapping[str, Entry],
    ) -> GateResult:
        hypotheses = self._higher_order_records(records)
        repeated = 0
        total = 0
        missing_source: list[str] = []
        for item in hypotheses:
            model_tokens = self._tokens(
                item.hypothesis if isinstance(item, InterpretationCandidate) else item.thesis
            )
            source_tokens: Counter[str] = Counter()
            for assignment in item.evidence_assignments:
                for observation_id in assignment.observation_ids:
                    observation = observations.get(observation_id)
                    if observation is None:
                        continue
                    for span in observation.spans:
                        entry = entries.get(span.entry_id)
                        if entry is not None and span.end <= len(entry.body):
                            source_tokens.update(
                                self._tokens(entry.body[span.start : span.end])
                            )
            if model_tokens and not source_tokens:
                missing_source.append(item.id)
            model_counter = Counter(model_tokens)
            repeated += sum(
                min(count, source_tokens.get(token, 0))
                for token, count in model_counter.items()
            )
            total += len(model_tokens)
        definition = _DEFINITION_BY_KEY["near_source_paraphrase_concentration"]
        if total == 0:
            return self._unavailable(definition)
        return self._value_result(
            definition,
            repeated / total,
            numerator=repeated,
            denominator=total,
            detail={"hypotheses_without_literal_source": sorted(missing_source)},
        )

    @staticmethod
    def _higher_order_records(
        records: Sequence[PersonalModelRecord],
    ) -> tuple[InterpretationCandidate | Interpretation, ...]:
        candidates = {
            item.id: item for item in records if isinstance(item, InterpretationCandidate)
        }
        if candidates:
            return tuple(candidates[item] for item in sorted(candidates))
        interpretations = {item.id: item for item in records if isinstance(item, Interpretation)}
        return tuple(interpretations[item] for item in sorted(interpretations))

    @staticmethod
    def _entry_source_key(entry: Entry | None) -> str | None:
        if entry is None:
            return None
        if entry.conversation.sender_id:
            return f"id:{entry.conversation.sender_id}"
        if entry.conversation.sender_label:
            return f"label:{entry.conversation.sender_label}"
        return None

    @staticmethod
    def _entry_time(entry: Entry) -> datetime | None:
        for literal in (entry.occurred_start, entry.captured_at, entry.ingested_at):
            if not literal:
                continue
            try:
                value = datetime.fromisoformat(literal.replace("Z", "+00:00"))
            except ValueError:
                continue
            if value.tzinfo is not None and value.utcoffset() is not None:
                return value
        return None

    @staticmethod
    def _tokens(value: str) -> tuple[str, ...]:
        return tuple(re.findall(r"[\w'-]+", value.casefold(), re.UNICODE))

    def _rate_result(
        self, key: str, numerator: int, denominator: int, detail: dict[str, Any]
    ) -> GateResult:
        definition = _DEFINITION_BY_KEY[key]
        if denominator == 0:
            return self._unavailable(definition, detail)
        return self._value_result(
            definition,
            numerator / denominator,
            numerator=numerator,
            denominator=denominator,
            detail=detail,
        )

    @staticmethod
    def _value_result(
        definition: GateDefinition,
        value: float,
        *,
        numerator: int,
        denominator: int,
        detail: dict[str, Any],
    ) -> GateResult:
        if definition.direction == ThresholdDirection.AT_LEAST:
            passes = value >= definition.threshold
        else:
            passes = value <= definition.threshold
        return GateResult(
            gate=definition.key,
            verdict=GateVerdict.READY if passes else GateVerdict.LIMITED,
            measured_value=round(value, 6),
            threshold=definition.threshold,
            direction=definition.direction,
            unit=definition.unit,
            numerator=numerator,
            denominator=denominator,
            failure=definition.failure,
            detail=detail,
        )

    @staticmethod
    def _unavailable(
        definition: GateDefinition, detail: dict[str, Any] | None = None
    ) -> GateResult:
        return GateResult(
            gate=definition.key,
            verdict=GateVerdict.UNAVAILABLE,
            threshold=definition.threshold,
            direction=definition.direction,
            unit=definition.unit,
            numerator=0,
            denominator=0,
            failure=definition.failure,
            detail=detail or {},
        )


__all__ = [
    "FactRecordGateResult",
    "GATE_DEFINITIONS",
    "GATE_KEYS",
    "GateDefinition",
    "GateResult",
    "GateVerdict",
    "GenerationReadiness",
    "LifeLookupFixture",
    "PersonalModelReadinessEvaluator",
    "ReadinessInputs",
    "ThresholdDirection",
]
