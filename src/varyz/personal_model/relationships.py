"""Deterministic, non-interpretive relationship-history compilation.

This module measures attributable shared history.  It deliberately does not emit
trust, closeness, compatibility, personality, or other relationship judgments.
Callers must explicitly approve every input record and supply correlation groups;
names and message volume never establish a relationship by themselves.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.models import (
    AttributionState,
    DerivationKind,
    DialogueContext,
    Event,
    EvidenceReference,
    Fact,
    Observation,
    ObservationKind,
    ObservationModality,
    Person,
    Preference,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    RecordState,
    RelationshipMeasurement,
    RelationshipSnapshot,
    TemporalScope,
    derived_record_id,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RelationshipRecordKind(StrEnum):
    OBSERVATION = "observation"
    EVENT = "event"
    FACT = "fact"
    PREFERENCE = "preference"


class RelationshipDiagnosticCode(StrEnum):
    MISSING_OWNER = "missing_owner"
    INVALID_PERSON = "invalid_person"
    UNKNOWN_PERSON = "unknown_person"
    UNKNOWN_RECORD = "unknown_record"
    UNAPPROVED_RECORD = "unapproved_record"
    DELETED_EVIDENCE = "deleted_evidence"
    UNKNOWN_TIME = "unknown_time"
    UNRESOLVED_CONTEXT = "unresolved_context"
    UNRELATED_RECORD = "unrelated_record"
    EMPTY_PERIOD = "empty_period"


class RelationshipPeriod(_FrozenModel):
    """A deterministic half-open interval, ``start <= time < end``."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("relationship periods must be timezone-aware")
        return value

    @model_validator(mode="after")
    def nonempty(self) -> RelationshipPeriod:
        if self.end <= self.start:
            raise ValueError("relationship period end must follow start")
        return self


class RelationshipPolicy(_FrozenModel):
    version: str = Field(min_length=1)
    minimum_confidence: float = Field(default=0.0, ge=0, le=1)


class ApprovedRelationshipRecord(_FrozenModel):
    """Explicit privacy approval for one canonical record.

    ``other_person_ids`` scopes approval; approving a record for Anna does not
    license its reuse in a snapshot for another person.
    """

    record_kind: RelationshipRecordKind
    record_id: str = Field(min_length=1)
    other_person_ids: tuple[str, ...]

    @model_validator(mode="after")
    def unique_people(self) -> ApprovedRelationshipRecord:
        if not self.other_person_ids:
            raise ValueError("relationship approval requires a scoped person")
        if len(set(self.other_person_ids)) != len(self.other_person_ids):
            raise ValueError("relationship approval person ids must be unique")
        return self


class ObservationRelationshipContext(_FrozenModel):
    """Resolved context that the literal observation contract cannot itself carry."""

    observation_id: str = Field(min_length=1)
    other_person_ids: tuple[str, ...]
    participants_attribution: AttributionState
    occurred_at: datetime | None = None
    conversation_group_id: str | None = None
    decision_group_id: str | None = None
    correlation_group_id: str | None = None
    explicit_relationship_role_statement: bool = False

    @field_validator("occurred_at")
    @classmethod
    def occurred_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("observation context occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_context(self) -> ObservationRelationshipContext:
        if len(set(self.other_person_ids)) != len(self.other_person_ids):
            raise ValueError("observation context person ids must be unique")
        if self.participants_attribution == AttributionState.RESOLVED:
            if not self.other_person_ids:
                raise ValueError("resolved relationship context requires another person")
        elif self.other_person_ids:
            raise ValueError("unresolved relationship context cannot carry canonical people")
        return self


class CorrelationAssignment(_FrozenModel):
    """Maps summaries, cross-posts, or repeated records to one underlying occurrence."""

    record_kind: RelationshipRecordKind
    record_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)


class RelationshipDependency(_FrozenModel):
    record_kind: RelationshipRecordKind
    record_id: str
    record_hash: str
    evidence_entry_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]


class RelationshipDiagnostic(_FrozenModel):
    code: RelationshipDiagnosticCode
    message: str
    record_ids: tuple[str, ...] = ()
    person_id: str | None = None


class RelationshipHistory(_FrozenModel):
    """Structured lookup links accompanying a neutral snapshot."""

    snapshot_id: str
    archive_owner_person_id: str
    other_person_id: str
    period_start: datetime
    period_end: datetime
    fact_ids: tuple[str, ...] = ()
    preference_ids: tuple[str, ...] = ()
    shared_event_ids: tuple[str, ...] = ()
    commitment_observation_ids: tuple[str, ...] = ()
    relationship_role_observation_ids: tuple[str, ...] = ()


class RelationshipCompilation(_FrozenModel):
    snapshots: tuple[RelationshipSnapshot, ...]
    histories: tuple[RelationshipHistory, ...]
    dependencies: tuple[RelationshipDependency, ...]
    quarantines: tuple[RelationshipDiagnostic, ...]


Record = Observation | Event | Fact | Preference


_SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    # Unclassified is fail-closed and therefore the most restrictive state.
    Sensitivity.UNCLASSIFIED: 3,
}


class RelationshipHistoryCompiler:
    """Compile approved canonical records into period-specific shared history."""

    def __init__(self, policy: RelationshipPolicy | None = None) -> None:
        self.policy = policy or RelationshipPolicy(version="relationship-history-v1")
        self._producer = ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="relationship-history-compiler",
            version=self.policy.version,
            rule_version=self.policy.version,
        )

    def compile(
        self,
        *,
        generation_id: str,
        archive_owner_person_id: str,
        significant_person_ids: Sequence[str],
        periods: Sequence[RelationshipPeriod],
        people: Sequence[Person],
        observations: Sequence[Observation] = (),
        events: Sequence[Event] = (),
        facts: Sequence[Fact] = (),
        preferences: Sequence[Preference] = (),
        approvals: Sequence[ApprovedRelationshipRecord] = (),
        observation_contexts: Sequence[ObservationRelationshipContext] = (),
        correlations: Sequence[CorrelationAssignment] = (),
        deleted_entry_ids: Sequence[str] = (),
        created_at: datetime,
    ) -> RelationshipCompilation:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if len(set(significant_person_ids)) != len(tuple(significant_person_ids)):
            raise ValueError("significant person ids must be unique")
        if len(set(deleted_entry_ids)) != len(tuple(deleted_entry_ids)):
            raise ValueError("deleted evidence entry ids must be unique")

        diagnostics: list[RelationshipDiagnostic] = []
        person_map = self._unique_by_id(people, "person")
        owner = person_map.get(archive_owner_person_id)
        if owner is None or not owner.is_archive_owner or not self._usable_person(owner):
            diagnostics.append(
                RelationshipDiagnostic(
                    code=RelationshipDiagnosticCode.MISSING_OWNER,
                    message="the requested active archive owner is absent",
                    person_id=archive_owner_person_id,
                )
            )
            return RelationshipCompilation(
                snapshots=(), histories=(), dependencies=(), quarantines=tuple(diagnostics)
            )

        observation_map = self._unique_by_id(observations, "observation")
        event_map = self._unique_by_id(events, "event")
        fact_map = self._unique_by_id(facts, "fact")
        preference_map = self._unique_by_id(preferences, "preference")
        record_maps: Mapping[RelationshipRecordKind, Mapping[str, Record]] = {
            RelationshipRecordKind.OBSERVATION: observation_map,
            RelationshipRecordKind.EVENT: event_map,
            RelationshipRecordKind.FACT: fact_map,
            RelationshipRecordKind.PREFERENCE: preference_map,
        }
        contexts = self._unique_by_key(
            observation_contexts, lambda item: item.observation_id, "observation context"
        )
        for observation_id in tuple(contexts):
            if observation_id not in observation_map:
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.UNKNOWN_RECORD,
                        message="relationship context references an absent observation",
                        record_ids=(observation_id,),
                    )
                )
                del contexts[observation_id]
        correlation_map = self._unique_by_key(
            correlations,
            lambda item: f"{item.record_kind.value}:{item.record_id}",
            "correlation assignment",
        )
        for key, assignment in tuple(correlation_map.items()):
            if assignment.record_id not in record_maps[assignment.record_kind]:
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.UNKNOWN_RECORD,
                        message="correlation assignment references an absent canonical record",
                        record_ids=(assignment.record_id,),
                    )
                )
                del correlation_map[key]
        approved: dict[tuple[RelationshipRecordKind, str], set[str]] = defaultdict(set)
        for approval in approvals:
            records = record_maps[approval.record_kind]
            if approval.record_id not in records:
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.UNKNOWN_RECORD,
                        message="relationship approval references an absent canonical record",
                        record_ids=(approval.record_id,),
                    )
                )
                continue
            approved[(approval.record_kind, approval.record_id)].update(
                approval.other_person_ids
            )

        deleted = set(deleted_entry_ids)
        snapshots: list[RelationshipSnapshot] = []
        histories: list[RelationshipHistory] = []
        dependencies: dict[tuple[RelationshipRecordKind, str], RelationshipDependency] = {}
        requested_people = tuple(sorted(significant_person_ids))
        requested_periods = tuple(sorted(periods, key=lambda item: (item.start, item.end)))
        for person_id in requested_people:
            if person_id == archive_owner_person_id:
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.INVALID_PERSON,
                        message="self relationships are not compiled",
                        person_id=person_id,
                    )
                )
                continue
            person = person_map.get(person_id)
            if person is None:
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.UNKNOWN_PERSON,
                        message="significant person id is absent",
                        person_id=person_id,
                    )
                )
                continue
            if not self._usable_person(person):
                diagnostics.append(
                    RelationshipDiagnostic(
                        code=RelationshipDiagnosticCode.INVALID_PERSON,
                        message="merged, split, rejected, or quarantined people are not compiled",
                        person_id=person_id,
                    )
                )
                continue
            for period in requested_periods:
                selected = self._select_records(
                    owner_id=archive_owner_person_id,
                    other_id=person_id,
                    period=period,
                    records=record_maps,
                    contexts=contexts,
                    approved=approved,
                    deleted=deleted,
                    diagnostics=diagnostics,
                )
                if not any(selected.values()):
                    diagnostics.append(
                        RelationshipDiagnostic(
                            code=RelationshipDiagnosticCode.EMPTY_PERIOD,
                            message="no approved attributable history falls in this period",
                            person_id=person_id,
                        )
                    )
                    continue
                snapshot, history = self._snapshot(
                    generation_id=generation_id,
                    owner_id=archive_owner_person_id,
                    other_id=person_id,
                    period=period,
                    selected=selected,
                    contexts=contexts,
                    correlations=correlation_map,
                    created_at=created_at,
                )
                snapshots.append(snapshot)
                histories.append(history)
                for kind, items in selected.items():
                    for record in items:
                        dependencies[(kind, record.id)] = self._dependency(kind, record)

        return RelationshipCompilation(
            snapshots=tuple(sorted(snapshots, key=lambda item: item.id)),
            histories=tuple(sorted(histories, key=lambda item: item.snapshot_id)),
            dependencies=tuple(dependencies[key] for key in sorted(dependencies)),
            quarantines=tuple(
                sorted(
                    diagnostics,
                    key=lambda item: (
                        item.code.value,
                        item.person_id or "",
                        item.record_ids,
                        item.message,
                    ),
                )
            ),
        )

    def _select_records(
        self,
        *,
        owner_id: str,
        other_id: str,
        period: RelationshipPeriod,
        records: Mapping[RelationshipRecordKind, Mapping[str, Record]],
        contexts: Mapping[str, ObservationRelationshipContext],
        approved: Mapping[tuple[RelationshipRecordKind, str], set[str]],
        deleted: set[str],
        diagnostics: list[RelationshipDiagnostic],
    ) -> dict[RelationshipRecordKind, tuple[Record, ...]]:
        selected: dict[RelationshipRecordKind, tuple[Record, ...]] = {}
        for kind, record_map in records.items():
            included: list[Record] = []
            for record in sorted(record_map.values(), key=lambda item: item.id):
                if other_id not in approved.get((kind, record.id), set()):
                    continue
                if record.readiness in {
                    Readiness.QUARANTINED,
                    Readiness.REJECTED,
                    Readiness.SUPERSEDED,
                } or record.confidence < self.policy.minimum_confidence:
                    diagnostics.append(
                        RelationshipDiagnostic(
                            code=RelationshipDiagnosticCode.UNAPPROVED_RECORD,
                            message="record state or confidence is not eligible for shared history",
                            record_ids=(record.id,),
                            person_id=other_id,
                        )
                    )
                    continue
                evidence_entries = self._evidence_entries(record)
                if evidence_entries & deleted:
                    diagnostics.append(
                        RelationshipDiagnostic(
                            code=RelationshipDiagnosticCode.DELETED_EVIDENCE,
                            message="record depends on deleted evidence and was excluded",
                            record_ids=(record.id,),
                            person_id=other_id,
                        )
                    )
                    continue
                context = contexts.get(record.id) if isinstance(record, Observation) else None
                occurred_at = self._occurred_at(record, context)
                if occurred_at is None:
                    diagnostics.append(
                        RelationshipDiagnostic(
                            code=RelationshipDiagnosticCode.UNKNOWN_TIME,
                            message="record cannot be assigned to a bounded relationship period",
                            record_ids=(record.id,),
                            person_id=other_id,
                        )
                    )
                    continue
                if not self._falls_in_period(record, context, period):
                    continue
                if not self._relates(record, owner_id, other_id, context):
                    code = (
                        RelationshipDiagnosticCode.UNRESOLVED_CONTEXT
                        if isinstance(record, Observation)
                        and (
                            context is None
                            or context.participants_attribution != AttributionState.RESOLVED
                        )
                        else RelationshipDiagnosticCode.UNRELATED_RECORD
                    )
                    diagnostics.append(
                        RelationshipDiagnostic(
                            code=code,
                            message="record does not establish an attributable link to this person",
                            record_ids=(record.id,),
                            person_id=other_id,
                        )
                    )
                    continue
                included.append(record)
            selected[kind] = tuple(included)
        return selected

    def _snapshot(
        self,
        *,
        generation_id: str,
        owner_id: str,
        other_id: str,
        period: RelationshipPeriod,
        selected: Mapping[RelationshipRecordKind, tuple[Record, ...]],
        contexts: Mapping[str, ObservationRelationshipContext],
        correlations: Mapping[str, CorrelationAssignment],
        created_at: datetime,
    ) -> tuple[RelationshipSnapshot, RelationshipHistory]:
        observations = tuple(
            item
            for item in selected[RelationshipRecordKind.OBSERVATION]
            if isinstance(item, Observation)
        )
        events = tuple(
            item for item in selected[RelationshipRecordKind.EVENT] if isinstance(item, Event)
        )
        facts = tuple(
            item for item in selected[RelationshipRecordKind.FACT] if isinstance(item, Fact)
        )
        preferences = tuple(
            item
            for item in selected[RelationshipRecordKind.PREFERENCE]
            if isinstance(item, Preference)
        )
        literal_observations = tuple(
            item
            for item in observations
            if item.modality != ObservationModality.QUOTED
            and item.dialogue_context != DialogueContext.QUOTATION
        )
        commitments = tuple(
            item
            for item in literal_observations
            if item.kind == ObservationKind.COMMITMENT
            and item.modality == ObservationModality.ASSERTED
            and item.dialogue_context
            not in {
                DialogueContext.BRAINSTORMING,
                DialogueContext.HUMOUR_OR_SARCASM,
                DialogueContext.HYPOTHETICAL,
            }
        )
        role_statements = tuple(
            item
            for item in literal_observations
            if contexts[item.id].explicit_relationship_role_statement
            and item.modality == ObservationModality.ASSERTED
            and item.dialogue_context
            not in {
                DialogueContext.BRAINSTORMING,
                DialogueContext.HUMOUR_OR_SARCASM,
                DialogueContext.HYPOTHETICAL,
            }
        )
        role_statement_groups = {
            contexts[item.id].decision_group_id
            or contexts[item.id].correlation_group_id
            or self._group_id(RelationshipRecordKind.OBSERVATION, item.id, correlations)
            for item in role_statements
        }
        conversation_groups = {
            contexts[item.id].conversation_group_id
            for item in literal_observations
            if contexts[item.id].conversation_group_id is not None
        }
        decision_groups = {
            contexts[item.id].decision_group_id
            for item in literal_observations
            if contexts[item.id].decision_group_id is not None
        }
        turns: dict[str, set[str]] = {owner_id: set(), other_id: set()}
        speakers_by_conversation: dict[str, set[str]] = defaultdict(set)
        for observation in literal_observations:
            speaker = observation.speaker_person_id
            if observation.speaker_attribution != AttributionState.RESOLVED or speaker not in turns:
                continue
            entry_ids = {span.entry_id for span in observation.spans}
            turns[speaker].update(entry_ids)
            conversation = contexts[observation.id].conversation_group_id
            if conversation is not None:
                speakers_by_conversation[conversation].add(speaker)
        mutual_groups = {
            group
            for group, speakers in speakers_by_conversation.items()
            if {owner_id, other_id} <= speakers
        }
        event_groups = {
            self._group_id(RelationshipRecordKind.EVENT, event.id, correlations)
            for event in events
        }
        commitment_groups = {
            contexts[item.id].decision_group_id
            or contexts[item.id].correlation_group_id
            or self._group_id(RelationshipRecordKind.OBSERVATION, item.id, correlations)
            for item in commitments
        }
        measurements = (
            self._measurement(
                "independent.conversation_groups",
                len(conversation_groups),
                "Distinct supplied conversation groups with approved attributable records.",
            ),
            self._measurement(
                "independent.decision_groups",
                len(decision_groups),
                "Distinct supplied decision groups; repeated turns and cross-posts count once.",
            ),
            self._measurement(
                "mutual.conversation_groups",
                len(mutual_groups),
                "Conversation groups containing attributable source turns from both people.",
            ),
            self._measurement(
                "diagnostic.owner_authored_turns",
                len(turns[owner_id]),
                "Unique attributable source turns; diagnostic only, never significance support.",
            ),
            self._measurement(
                "diagnostic.other_authored_turns",
                len(turns[other_id]),
                "Unique attributable source turns; diagnostic only, never significance support.",
            ),
            self._measurement(
                "independent.shared_events",
                len(event_groups),
                "Distinct supplied event correlation groups involving both people.",
            ),
            self._measurement(
                "independent.commitments",
                len(commitment_groups),
                "Distinct supplied commitment decision or correlation groups.",
            ),
            self._measurement(
                "explicit.relationship_role_statements",
                len(role_statement_groups),
                "Distinct correlated groups of attributable literal relationship-role statements.",
            ),
            self._measurement(
                "structured.facts",
                len(facts),
                "Approved canonical facts available in structured shared history.",
            ),
            self._measurement(
                "structured.preferences",
                len(preferences),
                "Approved canonical preferences available in structured shared history.",
            ),
        )
        all_records: tuple[Record, ...] = (*observations, *events, *facts, *preferences)
        evidence = self._evidence(all_records)
        claim_ids = tuple(sorted({claim for item in all_records for claim in item.claim_ids}))
        observed_times = tuple(
            occurred
            for item in all_records
            if (occurred := self._occurred_at(item, contexts.get(item.id))) is not None
        )
        sensitivity = max(
            (
                sensitivity
                for item in all_records
                for sensitivity in (
                    item.sensitivity,
                    *(reference.sensitivity for reference in item.evidence),
                )
            ),
            key=_SENSITIVITY_RANK.__getitem__,
        )
        snapshot_id = derived_record_id(
            "relationship_snapshot",
            canonical_subject=f"{owner_id}:{other_id}",
            canonical_predicate=(
                f"period:{period.start.isoformat()}:{period.end.isoformat()}"
            ),
            evidence_membership=(),
            identity_version=self.policy.version,
        )
        snapshot = RelationshipSnapshot(
            id=snapshot_id,
            generation_id=generation_id,
            evidence=evidence,
            claim_ids=claim_ids,
            temporal=TemporalScope(
                observed_at=max(observed_times) if observed_times else None,
                valid_from=period.start,
                valid_to=period.end,
                time_precision=TimePrecision.EXACT,
            ),
            confidence=min(item.confidence for item in all_records),
            sensitivity=sensitivity,
            derivation=(
                DerivationKind.OBSERVED
                if evidence or claim_ids
                else DerivationKind.USER_CONFIRMED
            ),
            producer=self._producer,
            readiness=Readiness.STAGED,
            created_at=created_at,
            archive_owner_person_id=owner_id,
            other_person_id=other_id,
            period_start=period.start,
            period_end=period.end,
            measurements=measurements,
            shared_event_ids=tuple(sorted(item.id for item in events)),
            commitment_observation_ids=tuple(sorted(item.id for item in commitments)),
        )
        history = RelationshipHistory(
            snapshot_id=snapshot.id,
            archive_owner_person_id=owner_id,
            other_person_id=other_id,
            period_start=period.start,
            period_end=period.end,
            fact_ids=tuple(sorted(item.id for item in facts)),
            preference_ids=tuple(sorted(item.id for item in preferences)),
            shared_event_ids=snapshot.shared_event_ids,
            commitment_observation_ids=snapshot.commitment_observation_ids,
            relationship_role_observation_ids=tuple(sorted(item.id for item in role_statements)),
        )
        return snapshot, history

    @staticmethod
    def _usable_person(person: Person) -> bool:
        return person.state == RecordState.ACTIVE and person.readiness not in {
            Readiness.QUARANTINED,
            Readiness.REJECTED,
            Readiness.SUPERSEDED,
        }

    @staticmethod
    def _unique_by_id(items: Sequence[Record | Person], label: str):
        return RelationshipHistoryCompiler._unique_by_key(items, lambda item: item.id, label)

    @staticmethod
    def _unique_by_key(items: Sequence, key, label: str):
        result = {}
        for item in items:
            item_key = key(item)
            if item_key in result:
                raise ValueError(f"duplicate {label}: {item_key}")
            result[item_key] = item
        return result

    @staticmethod
    def _occurred_at(
        record: Record, context: ObservationRelationshipContext | None
    ) -> datetime | None:
        if isinstance(record, Observation) and context is not None and context.occurred_at:
            return context.occurred_at
        return (
            record.temporal.valid_from
            or record.temporal.observed_at
            or record.temporal.stated_at
            or record.temporal.valid_to
        )

    @staticmethod
    def _falls_in_period(
        record: Record,
        context: ObservationRelationshipContext | None,
        period: RelationshipPeriod,
    ) -> bool:
        if isinstance(record, Observation):
            occurred = RelationshipHistoryCompiler._occurred_at(record, context)
            return occurred is not None and period.start <= occurred < period.end
        if isinstance(record, Event):
            occurred = RelationshipHistoryCompiler._occurred_at(record, None)
            return occurred is not None and period.start <= occurred < period.end
        valid_from = record.temporal.valid_from
        valid_to = record.temporal.valid_to
        if valid_from is not None:
            return valid_from < period.end and (valid_to is None or valid_to > period.start)
        occurred = RelationshipHistoryCompiler._occurred_at(record, None)
        return occurred is not None and period.start <= occurred < period.end

    @staticmethod
    def _relates(
        record: Record,
        owner_id: str,
        other_id: str,
        context: ObservationRelationshipContext | None,
    ) -> bool:
        if isinstance(record, Observation):
            if context is None or context.participants_attribution != AttributionState.RESOLVED:
                return False
            if other_id not in context.other_person_ids:
                return False
            involved = {
                record.speaker_person_id,
                record.subject_id,
                *record.addressee_person_ids,
            }
            return {owner_id, other_id} <= involved
        if isinstance(record, Event):
            return {owner_id, other_id} <= set(record.participant_ids)
        if isinstance(record, (Fact, Preference)):
            return record.subject.id == other_id
        return False

    @staticmethod
    def _evidence_entries(record: Record) -> set[str]:
        entries = {reference.entry_id for reference in record.evidence}
        if isinstance(record, Observation):
            entries.update(span.entry_id for span in record.spans)
        return entries

    @staticmethod
    def _evidence(records: Sequence[Record]) -> tuple[EvidenceReference, ...]:
        references: dict[str, EvidenceReference] = {}
        for record in records:
            for reference in record.evidence:
                pinned = (
                    reference.model_copy(update={"observation_id": record.id})
                    if isinstance(record, Observation)
                    else reference
                )
                references[pinned.model_dump_json()] = pinned
        return tuple(references[key] for key in sorted(references))

    @staticmethod
    def _group_id(
        kind: RelationshipRecordKind,
        record_id: str,
        correlations: Mapping[str, CorrelationAssignment],
    ) -> str:
        key = f"{kind.value}:{record_id}"
        assignment = correlations.get(key)
        return assignment.group_id if assignment is not None else key

    @staticmethod
    def _measurement(key: str, count: int, description: str) -> RelationshipMeasurement:
        return RelationshipMeasurement(key=key, count=count, description=description)

    @staticmethod
    def _dependency(kind: RelationshipRecordKind, record: Record) -> RelationshipDependency:
        payload = record.model_dump_json(exclude={"generation_id"})
        return RelationshipDependency(
            record_kind=kind,
            record_id=record.id,
            record_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            evidence_entry_ids=tuple(sorted(RelationshipHistoryCompiler._evidence_entries(record))),
            claim_ids=tuple(sorted(record.claim_ids)),
        )


__all__ = [
    "ApprovedRelationshipRecord",
    "CorrelationAssignment",
    "ObservationRelationshipContext",
    "RelationshipCompilation",
    "RelationshipDependency",
    "RelationshipDiagnostic",
    "RelationshipDiagnosticCode",
    "RelationshipHistory",
    "RelationshipHistoryCompiler",
    "RelationshipPeriod",
    "RelationshipPolicy",
    "RelationshipRecordKind",
]
