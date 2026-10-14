"""Pure contracts for Varyz' canonical personal model.

These records are deliberately immutable and provider-agnostic. They preserve exact
source literals and compact, inspectable derivations; generated display prose is never
part of identity, and hidden model reasoning has no place in the schema.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from varyz.domain import Sensitivity, TimePrecision

type StableId = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\s]+$")]

#: An id in a field a provider is also asked to *write*, which is a
#: different job from holding one.  Same shape as `StableId` and a quarter
#: of its ceiling: the longest id this archive has ever minted is 71
#: characters, while 512 in a list of 64 is a grammar a greedy decode can
#: spend an entire reply inside -- and one did, emitting hex, which is the
#: failure that made the wire number its observations in the first place.
type WrittenId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\s]+$")]
type GenerationId = StableId
type EntryId = StableId
type ClaimId = StableId
type EpisodeId = StableId


def _canonical_hash(namespace: str, payload: Any) -> str:
    material = json.dumps(
        {"namespace": namespace, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def derived_record_id(
    record_kind: str,
    *,
    canonical_subject: str,
    canonical_predicate: str,
    evidence_membership: tuple[str, ...],
    identity_version: str,
) -> str:
    """Build an id from semantic identity, never title or explanatory prose."""
    return _canonical_hash(
        "personal-model-record",
        {
            "kind": record_kind,
            "subject": canonical_subject,
            "predicate": canonical_predicate,
            "evidence": sorted(set(evidence_membership)),
            "identity_version": identity_version,
        },
    )


def conversation_episode_id(
    ordered_entry_ids: tuple[str, ...], *, boundary_policy_version: str
) -> str:
    """Build an episode id from ordered membership and the boundary policy."""
    if not ordered_entry_ids:
        raise ValueError("an episode requires at least one entry")
    if len(set(ordered_entry_ids)) != len(ordered_entry_ids):
        raise ValueError("episode entry membership must be unique")
    return _canonical_hash(
        "conversation-episode",
        {"entries": ordered_entry_ids, "boundary_policy_version": boundary_policy_version},
    )


class CandidateAssemblyError(ValueError):
    """A candidate proposal the assembly could not resolve, named safely.

    A candidate adapter is an untrusted boundary: it wraps a model whose output
    may be anything, and a plain ``ValueError`` from inside it can carry that
    output in its message.  The compiler therefore refuses to repeat what an
    adapter said, and for a long time reported only "invalid candidates" -- true,
    unactionable, and the same sentence whether one proposal in sixty thousand
    named a person who does not exist or the provider was down.

    This carries a `reason` the adapter itself wrote: a fixed string naming the
    check that refused, never the text that failed it.  The compiler reports
    `reason` and nothing else, so an operator learns which rule fired without
    the payload behind it reaching a log, an error field or a report.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DerivationKind(StrEnum):
    EXPLICIT = "explicit"
    OBSERVED = "observed"
    INFERRED = "inferred"
    USER_CONFIRMED = "user_confirmed"


class Readiness(StrEnum):
    STAGED = "staged"
    READY = "ready"
    LIMITED = "limited"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EvidenceRole(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    UPDATES = "updates"
    MENTIONS = "mentions"
    DEFINES_SCOPE = "defines_scope"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ObservationModality(StrEnum):
    ASSERTED = "asserted"
    UNCERTAIN = "uncertain"
    HYPOTHETICAL = "hypothetical"
    QUOTED = "quoted"
    HUMOROUS_OR_SARCASTIC = "humorous_or_sarcastic"
    UNKNOWN = "unknown"


class ObservationKind(StrEnum):
    STATEMENT = "statement"
    REASON = "reason"
    CHOICE = "choice"
    TRADEOFF = "tradeoff"
    BOUNDARY = "boundary"
    COMMITMENT = "commitment"
    REACTION = "reaction"


class DialogueContext(StrEnum):
    VOLUNTEERED_REASONING = "volunteered_reasoning"
    DIRECT_ANSWER = "direct_answer"
    PROMPTED_AGREEMENT = "prompted_agreement"
    QUOTATION = "quotation"
    BRAINSTORMING = "brainstorming"
    INSTRUCTION = "instruction"
    HUMOUR_OR_SARCASM = "humour_or_sarcasm"
    HYPOTHETICAL = "hypothetical"
    UNKNOWN = "unknown"


class ContextCompleteness(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class AttributionState(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class InterpretationKind(StrEnum):
    VALUE = "value"
    DECISION_PATTERN = "decision_pattern"
    PERSISTENT_OBJECTIVE = "persistent_objective"
    TENSION = "tension"
    RELATIONSHIP = "relationship"
    CHANGE_OVER_TIME = "changed_over_time"


class ScopeKind(StrEnum):
    GLOBAL = "global"
    DOMAIN = "domain"
    PROJECT = "project"
    RELATIONSHIP = "relationship"
    TIME_PERIOD = "time_period"


class CountersearchState(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class InterpretationVerdict(StrEnum):
    """The owner's display judgment, deliberately separate from evidence confidence."""

    CONFIRM = "confirm"
    PARTIAL = "partial"
    REJECT = "reject"


class CountersearchLane(FrozenModel):
    """Auditable retrieval inputs and outputs for one side of hypothesis testing."""

    query_fingerprint: StableId
    query_variants: int = Field(ge=1)
    max_candidates: int = Field(ge=1)
    candidates_considered: int = Field(ge=0)
    candidate_pool_hash: StableId
    result_hash: StableId
    retrieval_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def considered_within_budget(self) -> CountersearchLane:
        if self.candidates_considered > self.max_candidates:
            raise ValueError("countersearch candidates considered exceed declared breadth")
        return self


class CountersearchManifest(FrozenModel):
    """Compact proof that support and counterevidence received symmetric search."""

    support: CountersearchLane
    counter: CountersearchLane
    evidence_window_from: datetime | None = None
    evidence_window_to: datetime | None = None
    source_scopes: tuple[str, ...]
    started_at: datetime
    completed_at: datetime | None = None
    completed: bool

    @model_validator(mode="after")
    def valid_symmetric_search(self) -> CountersearchManifest:
        for field_name, value in (
            ("evidence_window_from", self.evidence_window_from),
            ("evidence_window_to", self.evidence_window_to),
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if (
            self.evidence_window_from
            and self.evidence_window_to
            and self.evidence_window_to < self.evidence_window_from
        ):
            raise ValueError("countersearch window ends before it starts")
        if self.completed and self.completed_at is None:
            raise ValueError("completed countersearch requires completed_at")
        if not self.completed and self.completed_at is not None:
            raise ValueError("incomplete countersearch cannot carry completed_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("countersearch cannot complete before it starts")
        if not self.source_scopes or any(not scope.strip() for scope in self.source_scopes):
            raise ValueError("countersearch requires non-blank source scopes")
        if len(set(self.source_scopes)) != len(self.source_scopes):
            raise ValueError("countersearch source scopes must be unique")
        if self.support.query_variants != self.counter.query_variants:
            raise ValueError("support and countersearch query breadth must be symmetric")
        if self.support.max_candidates != self.counter.max_candidates:
            raise ValueError("support and countersearch candidate breadth must be symmetric")
        if self.support.retrieval_version != self.counter.retrieval_version:
            raise ValueError("support and countersearch must use one retrieval version")
        return self


class EntityKind(StrEnum):
    PERSON = "person"
    PROJECT = "project"
    GOAL = "goal"
    PLACE = "place"
    ORGANIZATION = "organization"
    EVENT = "event"


class RecordState(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    SPLIT = "split"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class PreferenceValence(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"
    AVOID = "avoid"
    PREFER = "prefer"


class GoalKind(StrEnum):
    INTENDED_OUTCOME = "intended_outcome"
    GOAL = "goal"
    WORKSTREAM = "workstream"
    SUBGOAL = "subgoal"


class GoalStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    STALLED = "stalled"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class FactStatus(StrEnum):
    ACTIVE = "active"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class FactValueKind(StrEnum):
    TEXT = "text"
    DATE = "date"
    MONTH_DAY = "month_day"
    NUMBER = "number"
    BOOLEAN = "boolean"
    LOCATION = "location"
    ENTITY_REF = "entity_ref"
    EXTENSION = "extension"


class DateAmbiguityKind(StrEnum):
    RELATIVE = "relative"
    NUMERIC_ORDER = "numeric_order"
    MISSING_YEAR = "missing_year"
    CONFLICTING_CONTEXT = "conflicting_context"
    UNKNOWN = "unknown"


class ProducerKind(StrEnum):
    DETERMINISTIC = "deterministic"
    LOCAL_MODEL = "local_model"
    USER = "user"


class ProducerMetadata(FrozenModel):
    kind: ProducerKind
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    model_name: str | None = None
    prompt_version: str | None = None
    rule_version: str | None = None

    @model_validator(mode="after")
    def required_versions(self) -> ProducerMetadata:
        if self.kind == ProducerKind.LOCAL_MODEL and (
            self.model_name is None or self.prompt_version is None
        ):
            raise ValueError("local-model producers require model_name and prompt_version")
        if self.kind == ProducerKind.DETERMINISTIC and self.rule_version is None:
            raise ValueError("deterministic producers require rule_version")
        if self.kind == ProducerKind.USER and (self.model_name or self.prompt_version):
            raise ValueError("user producers cannot carry model or prompt metadata")
        return self


class TextSpan(FrozenModel):
    entry_id: EntryId
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    exact_literal: str = Field(min_length=1)
    source_revision: str | None = None

    @model_validator(mode="after")
    def ordered(self) -> TextSpan:
        if self.end <= self.start:
            raise ValueError("text span end must follow start")
        if len(self.exact_literal) != self.end - self.start:
            raise ValueError("exact_literal length must match the source span")
        return self


class EvidenceReference(FrozenModel):
    entry_id: EntryId
    role: EvidenceRole
    claim_id: ClaimId | None = None
    episode_id: EpisodeId | None = None
    observation_id: StableId | None = None
    span: TextSpan | None = None
    independence_group_id: StableId | None = None
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED

    @model_validator(mode="after")
    def span_matches_entry(self) -> EvidenceReference:
        if self.span is not None and self.span.entry_id != self.entry_id:
            raise ValueError("evidence span must reference the same entry")
        return self


class TemporalScope(FrozenModel):
    observed_at: datetime | None = None
    stated_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_precision: TimePrecision = TimePrecision.UNKNOWN
    original_time_literal: str | None = None

    @field_validator("observed_at", "stated_at", "valid_from", "valid_to")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_interval(self) -> TemporalScope:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        return self


class InterpretationScope(FrozenModel):
    kind: ScopeKind
    # A label, and bounded like one: this model is also asked of a server,
    # where an unbounded string is an unbounded decode.
    domain: str | None = Field(default=None, max_length=160)
    project_id: StableId | None = None
    relationship_person_ids: tuple[StableId, StableId] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def scope_fields_match_kind(self) -> InterpretationScope:
        requirements = {
            ScopeKind.DOMAIN: self.domain,
            ScopeKind.PROJECT: self.project_id,
            ScopeKind.RELATIONSHIP: self.relationship_person_ids,
            ScopeKind.TIME_PERIOD: self.valid_from or self.valid_to,
        }
        required = requirements.get(self.kind)
        if self.kind != ScopeKind.GLOBAL and required is None:
            raise ValueError(f"{self.kind} scope is missing its defining field")
        if (
            self.relationship_person_ids
            and self.relationship_person_ids[0] == self.relationship_person_ids[1]
        ):
            raise ValueError("a relationship scope requires two different people")
        if self.valid_from and self.valid_from.tzinfo is None:
            raise ValueError("scope valid_from must be timezone-aware")
        if self.valid_to and self.valid_to.tzinfo is None:
            raise ValueError("scope valid_to must be timezone-aware")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("scope valid_to precedes valid_from")
        return self


class DerivedRecord(FrozenModel):
    id: StableId
    generation_id: GenerationId
    evidence: tuple[EvidenceReference, ...] = ()
    claim_ids: tuple[ClaimId, ...] = ()
    temporal: TemporalScope = Field(default_factory=TemporalScope)
    confidence: float = Field(ge=0, le=1)
    sensitivity: Sensitivity
    derivation: DerivationKind
    producer: ProducerMetadata
    readiness: Readiness = Readiness.STAGED
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def attributable_derivation(self) -> DerivedRecord:
        if (
            not self.evidence
            and not self.claim_ids
            and self.derivation != DerivationKind.USER_CONFIRMED
        ):
            raise ValueError("derived records require evidence, claims, or user confirmation")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim_ids must be unique")
        return self


class SourceTurn(FrozenModel):
    entry_id: EntryId
    source_turn_id: str | None = None
    speaker_source_key: str | None = None
    order: int = Field(ge=0)
    occurred_at: datetime | None = None
    edited: bool | None = None

    @field_validator("occurred_at")
    @classmethod
    def occurred_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("turn timestamps must be timezone-aware")
        return value


class ReplyEdge(FrozenModel):
    from_entry_id: EntryId
    to_entry_id: EntryId
    is_quote: bool = False

    @model_validator(mode="after")
    def not_self_reply(self) -> ReplyEdge:
        if self.from_entry_id == self.to_entry_id:
            raise ValueError("a turn cannot reply to itself")
        return self


class OmittedRange(FrozenModel):
    after_entry_id: EntryId | None = None
    before_entry_id: EntryId | None = None
    omitted_count: int = Field(gt=0)
    reason: str = Field(min_length=1)


class ConversationEpisode(DerivedRecord):
    source: str = Field(min_length=1)
    source_thread_id: str | None = None
    turns: tuple[SourceTurn, ...]
    participant_source_keys: tuple[str, ...] = ()
    participant_person_ids: tuple[StableId, ...] = ()
    reply_edges: tuple[ReplyEdge, ...] = ()
    initiating_entry_id: EntryId
    target_entry_id: EntryId
    completeness: ContextCompleteness
    boundary_policy_version: str = Field(min_length=1)
    correlation_id: StableId | None = None
    omitted_ranges: tuple[OmittedRange, ...] = ()

    @model_validator(mode="after")
    def valid_membership(self) -> ConversationEpisode:
        ids = tuple(turn.entry_id for turn in self.turns)
        if not ids:
            raise ValueError("an episode requires turns")
        if len(set(ids)) != len(ids):
            raise ValueError("episode turns must have unique entry ids")
        orders = tuple(turn.order for turn in self.turns)
        if len(set(orders)) != len(orders):
            raise ValueError("episode turns must have unique source-order values")
        if orders != tuple(sorted(orders)):
            raise ValueError("episode turns must be in source order")
        if self.initiating_entry_id not in ids or self.target_entry_id not in ids:
            raise ValueError("initiating and target turns must belong to the episode")
        for edge in self.reply_edges:
            if edge.from_entry_id not in ids or edge.to_entry_id not in ids:
                raise ValueError("reply edges cannot leave episode membership")
        if self.completeness == ContextCompleteness.COMPLETE and self.omitted_ranges:
            raise ValueError("a complete episode cannot have omitted ranges")
        if self.omitted_ranges and self.completeness != ContextCompleteness.INCOMPLETE:
            raise ValueError("omitted ranges require incomplete context")
        return self


class Observation(DerivedRecord):
    episode_id: EpisodeId
    kind: ObservationKind
    proposition: str = Field(min_length=1)
    spans: tuple[TextSpan, ...]
    speaker_person_id: StableId | None = None
    speaker_source_key: str | None = None
    speaker_attribution: AttributionState
    subject_kind: EntityKind | None = None
    subject_id: StableId | None = None
    subject_surface: str | None = None
    subject_attribution: AttributionState
    addressee_person_ids: tuple[StableId, ...] = ()
    dialogue_context: DialogueContext = DialogueContext.UNKNOWN
    polarity: Polarity = Polarity.UNKNOWN
    modality: ObservationModality = ObservationModality.UNKNOWN
    cost_or_stakes_literal: str | None = None

    @model_validator(mode="after")
    def valid_attribution_and_spans(self) -> Observation:
        if not self.spans:
            raise ValueError("observations require exact source spans")
        if self.speaker_attribution == AttributionState.RESOLVED and not self.speaker_person_id:
            raise ValueError("resolved speaker attribution requires speaker_person_id")
        if self.speaker_attribution != AttributionState.RESOLVED and self.speaker_person_id:
            raise ValueError("unresolved speaker attribution cannot carry speaker_person_id")
        if self.subject_attribution == AttributionState.RESOLVED and not self.subject_id:
            raise ValueError("resolved subject attribution requires subject_id")
        if self.subject_attribution != AttributionState.RESOLVED and self.subject_id:
            raise ValueError("unresolved subject attribution cannot carry subject_id")
        if self.readiness == Readiness.READY and (
            self.speaker_attribution != AttributionState.RESOLVED
            or self.subject_attribution != AttributionState.RESOLVED
        ):
            raise ValueError("ready observations require resolved speaker and subject")
        return self


class AliasKind(StrEnum):
    NAME = "name"
    USERNAME = "username"
    EMAIL = "email"
    PHONE = "phone"
    SOURCE_ACCOUNT_ID = "source_account_id"


class VerificationState(StrEnum):
    CANDIDATE = "candidate"
    AUTOMATIC = "automatic"
    USER_CONFIRMED = "user_confirmed"
    REJECTED = "rejected"


class EntityAlias(FrozenModel):
    kind: AliasKind
    exact_value: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    source: str | None = None
    source_account: str | None = None
    verification: VerificationState = VerificationState.CANDIDATE
    evidence_entry_ids: tuple[EntryId, ...] = ()


class Person(DerivedRecord):
    canonical_key: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    aliases: tuple[EntityAlias, ...] = ()
    roles_in_users_life: tuple[str, ...] = ()
    is_archive_owner: bool = False
    state: RecordState = RecordState.ACTIVE
    merged_into_person_id: StableId | None = None
    identity_override_id: StableId | None = None

    @model_validator(mode="after")
    def valid_identity_state(self) -> Person:
        if self.state == RecordState.MERGED:
            if not self.merged_into_person_id or not self.identity_override_id:
                raise ValueError("merged people require a target and user override")
            if self.merged_into_person_id == self.id:
                raise ValueError("a person cannot merge into itself")
        elif self.merged_into_person_id is not None:
            raise ValueError("only merged people may have a merge target")
        if self.state == RecordState.SPLIT and not self.identity_override_id:
            raise ValueError("split people require a user override")
        return self


class Project(DerivedRecord):
    canonical_key: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    aliases: tuple[EntityAlias, ...] = ()
    life_area: str | None = None
    participant_person_ids: tuple[StableId, ...] = ()
    purpose: str | None = None
    state: RecordState = RecordState.ACTIVE
    merged_into_project_id: StableId | None = None
    identity_override_id: StableId | None = None

    @model_validator(mode="after")
    def valid_identity_state(self) -> Project:
        if self.state == RecordState.MERGED:
            if not self.merged_into_project_id or not self.identity_override_id:
                raise ValueError("merged projects require a target and user override")
            if self.merged_into_project_id == self.id:
                raise ValueError("a project cannot merge into itself")
        elif self.merged_into_project_id is not None:
            raise ValueError("only merged projects may have a merge target")
        if self.state == RecordState.SPLIT and not self.identity_override_id:
            raise ValueError("split projects require a user override")
        return self


class Goal(DerivedRecord):
    canonical_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    kind: GoalKind
    status: GoalStatus
    project_ids: tuple[StableId, ...] = ()
    parent_goal_ids: tuple[StableId, ...] = ()
    dependency_goal_ids: tuple[StableId, ...] = ()
    intended_outcome: str | None = None

    @model_validator(mode="after")
    def valid_hierarchy(self) -> Goal:
        if self.id in self.parent_goal_ids or self.id in self.dependency_goal_ids:
            raise ValueError("a goal cannot parent or depend on itself")
        if self.kind == GoalKind.INTENDED_OUTCOME and self.parent_goal_ids:
            raise ValueError("an intended outcome cannot have a parent goal")
        return self


class DateAmbiguity(FrozenModel):
    kind: DateAmbiguityKind
    candidates: tuple[date, ...] = ()
    source_timestamp: datetime | None = None
    locale: str | None = None
    timezone: str | None = None
    explanation: str = Field(min_length=1)

    @field_validator("source_timestamp")
    @classmethod
    def source_timestamp_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("date-resolution source timestamp must be timezone-aware")
        return value


class TextFactValue(FrozenModel):
    kind: Literal[FactValueKind.TEXT]
    exact_literal: str = Field(min_length=1)
    normalized: str | None = None


class DateFactValue(FrozenModel):
    kind: Literal[FactValueKind.DATE]
    exact_literal: str = Field(min_length=1)
    value: date | None = None
    ambiguity: DateAmbiguity | None = None

    @model_validator(mode="after")
    def exact_or_ambiguous(self) -> DateFactValue:
        if (self.value is None) == (self.ambiguity is None):
            raise ValueError("a date must be either exact or explicitly ambiguous")
        return self


class MonthDayFactValue(FrozenModel):
    kind: Literal[FactValueKind.MONTH_DAY]
    exact_literal: str = Field(min_length=1)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def valid_month_day(self) -> MonthDayFactValue:
        # Leap day is valid for a recurring or unknown-year date.
        year = 2000 if self.month == 2 and self.day == 29 else 2001
        try:
            date(year, self.month, self.day)
        except ValueError as exc:
            raise ValueError("invalid month/day") from exc
        return self


class NumberFactValue(FrozenModel):
    kind: Literal[FactValueKind.NUMBER]
    exact_literal: str = Field(min_length=1)
    value: Decimal
    unit: str | None = None

    @field_validator("value")
    @classmethod
    def finite_number(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("number values must be finite")
        return value


class BooleanFactValue(FrozenModel):
    kind: Literal[FactValueKind.BOOLEAN]
    exact_literal: str = Field(min_length=1)
    value: bool


class LocationFactValue(FrozenModel):
    kind: Literal[FactValueKind.LOCATION]
    exact_literal: str = Field(min_length=1)
    label: str = Field(min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> LocationFactValue:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("location latitude and longitude must be supplied together")
        if self.latitude is not None and (
            not math.isfinite(self.latitude) or not math.isfinite(self.longitude or 0)
        ):
            raise ValueError("location coordinates must be finite")
        return self


class EntityRefFactValue(FrozenModel):
    kind: Literal[FactValueKind.ENTITY_REF]
    exact_literal: str = Field(min_length=1)
    entity_kind: EntityKind
    entity_id: StableId


class ExtensionFactValue(FrozenModel):
    kind: Literal[FactValueKind.EXTENSION]
    exact_literal: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    value: dict[str, Any]


FactValue = Annotated[
    TextFactValue
    | DateFactValue
    | MonthDayFactValue
    | NumberFactValue
    | BooleanFactValue
    | LocationFactValue
    | EntityRefFactValue
    | ExtensionFactValue,
    Field(discriminator="kind"),
]


class SubjectReference(FrozenModel):
    kind: EntityKind
    id: StableId


class Fact(DerivedRecord):
    subject: SubjectReference
    predicate: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    value: FactValue
    asserted_by_person_id: StableId | None = None
    status: FactStatus = FactStatus.ACTIVE
    supersedes_fact_ids: tuple[StableId, ...] = ()
    contradicted_by_fact_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def valid_fact_state(self) -> Fact:
        if self.id in self.supersedes_fact_ids or self.id in self.contradicted_by_fact_ids:
            raise ValueError("a fact cannot supersede or contradict itself")
        if self.status == FactStatus.CONTRADICTED and not self.contradicted_by_fact_ids:
            raise ValueError("contradicted facts require a conflicting fact reference")
        return self


class Preference(DerivedRecord):
    subject: SubjectReference
    predicate: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    object: FactValue
    valence: PreferenceValence
    strength: float | None = Field(default=None, ge=0, le=1)
    asserted_by_person_id: StableId | None = None
    scope_kind: str | None = None
    scope_label: str | None = None
    assertion_kind: str = "stated_taste"
    contradicted_by_preference_ids: tuple[StableId, ...] = ()
    supersedes_preference_ids: tuple[StableId, ...] = ()
    status: FactStatus = FactStatus.ACTIVE

    @model_validator(mode="after")
    def preference_is_not_mention(self) -> Preference:
        if (self.scope_kind is None) != (self.scope_label is None):
            raise ValueError("preference scope kind and label must be supplied together")
        if self.assertion_kind not in {"stated_taste", "project_directive"}:
            raise ValueError("unsupported preference assertion kind")
        if self.assertion_kind == "project_directive" and self.scope_kind != "project":
            raise ValueError("project directives require project scope")
        if self.derivation != DerivationKind.USER_CONFIRMED and not any(
            reference.role == EvidenceRole.SUPPORTS for reference in self.evidence
        ):
            raise ValueError("preferences require supporting evidence, not mention-only evidence")
        if self.id in self.contradicted_by_preference_ids:
            raise ValueError("a preference cannot contradict itself")
        if self.id in self.supersedes_preference_ids:
            raise ValueError("a preference cannot supersede itself")
        return self


class Event(DerivedRecord):
    canonical_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    event_kind: str = Field(min_length=1)
    participant_ids: tuple[StableId, ...] = ()
    project_ids: tuple[StableId, ...] = ()
    location: LocationFactValue | None = None

    @model_validator(mode="after")
    def event_has_time(self) -> Event:
        if not any(
            (
                self.temporal.observed_at,
                self.temporal.stated_at,
                self.temporal.valid_from,
                self.temporal.valid_to,
                self.temporal.original_time_literal,
            )
        ):
            raise ValueError("events require a dated or explicitly unresolved time")
        return self


class RelationshipMeasurement(FrozenModel):
    key: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    count: int = Field(ge=0)
    description: str = ""


class RelationshipSnapshot(DerivedRecord):
    archive_owner_person_id: StableId
    other_person_id: StableId
    period_start: datetime
    period_end: datetime
    measurements: tuple[RelationshipMeasurement, ...] = ()
    shared_event_ids: tuple[StableId, ...] = ()
    commitment_observation_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def valid_relationship_period(self) -> RelationshipSnapshot:
        if self.archive_owner_person_id == self.other_person_id:
            raise ValueError("a relationship snapshot requires two different people")
        if self.period_start.tzinfo is None or self.period_end.tzinfo is None:
            raise ValueError("relationship period timestamps must be timezone-aware")
        if self.period_end < self.period_start:
            raise ValueError("relationship period_end precedes period_start")
        return self


class InterpretationEvidence(FrozenModel):
    episode_id: EpisodeId
    observation_ids: tuple[StableId, ...]
    role: EvidenceRole
    independence_group_id: StableId
    strength_features: tuple[str, ...] = ()

    @model_validator(mode="after")
    def requires_observations(self) -> InterpretationEvidence:
        if not self.observation_ids:
            raise ValueError("interpretation evidence requires literal observations")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("interpretation observation ids must be unique")
        return self


class AlternativeExplanation(FrozenModel):
    # Bounded for the same reason the list below is, and the reasoning that
    # bounded the list should have reached these two the first time: an
    # unbounded string is an unbounded character repetition in the grammar, and
    # a greedy decode filled it to the 8,192-token cap on five of twelve
    # concepts on 2026-09-06.  Six hundred is a thesis and the evidence that
    # tells it apart, said once; the whole reply then has a ceiling of about
    # 3,000 tokens of text, which fits the slot with the ids beside it.
    thesis: str = Field(min_length=1, max_length=600)
    distinguishing_evidence: str = Field(min_length=1, max_length=600)
    # A ceiling, not a budget.  This model is also the shape a provider is
    # asked to generate, and an unbounded list of ids is an unbounded
    # repetition in the grammar built from it -- which a greedy decode is free
    # to keep filling until it runs out of tokens.  An alternative can only
    # cite evidence that was selected for it, and that is capped far below this
    # by ``SynthesisPolicy.max_candidates``.
    evidence_ids: tuple[WrittenId, ...] = Field(default=(), max_length=64)


class InterpretationCandidate(DerivedRecord):
    concept_key: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    hypothesis: str = Field(min_length=1)
    kind: InterpretationKind
    scope: InterpretationScope
    evidence_assignments: tuple[InterpretationEvidence, ...]
    alternatives: tuple[AlternativeExplanation, ...]
    missing_evidence: tuple[str, ...] = ()
    countersearch_state: CountersearchState = CountersearchState.NOT_STARTED
    countersearch_manifest: CountersearchManifest | None = None
    validation_issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def candidate_gate(self) -> InterpretationCandidate:
        roles = {item.role for item in self.evidence_assignments}
        if EvidenceRole.SUPPORTS not in roles:
            raise ValueError("interpretation candidates require supporting evidence")
        if self.readiness == Readiness.READY:
            raise ValueError("candidates cannot be user-visible ready conclusions")
        if self.countersearch_state == CountersearchState.COMPLETE and (
            self.countersearch_manifest is None or not self.countersearch_manifest.completed
        ):
            raise ValueError("completed countersearch requires an auditable completed manifest")
        if (
            self.countersearch_manifest is not None
            and self.countersearch_manifest.completed
            and self.countersearch_state != CountersearchState.COMPLETE
        ):
            raise ValueError("a completed manifest requires complete countersearch state")
        return self


class Interpretation(DerivedRecord):
    concept_key: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    thesis: str = Field(min_length=1)
    kind: InterpretationKind
    scope: InterpretationScope
    evidence_assignments: tuple[InterpretationEvidence, ...]
    alternatives: tuple[AlternativeExplanation, ...]
    missing_evidence: tuple[str, ...] = ()
    counterevidence_summary: str = Field(min_length=1)
    strongest_counterexample: str | None = None
    countersearch_state: CountersearchState
    countersearch_manifest: CountersearchManifest | None = None
    derivation_summary: str = Field(min_length=1, max_length=2000)
    candidate_id: StableId
    owner_verdict: InterpretationVerdict | None = None
    owner_kept_part: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def interpretation_gate(self) -> Interpretation:
        roles = {item.role for item in self.evidence_assignments}
        if EvidenceRole.SUPPORTS not in roles:
            raise ValueError("interpretations require supporting observations")
        if self.countersearch_state == CountersearchState.COMPLETE and (
            self.countersearch_manifest is None or not self.countersearch_manifest.completed
        ):
            raise ValueError("completed countersearch requires an auditable completed manifest")
        if (
            self.countersearch_manifest is not None
            and self.countersearch_manifest.completed
            and self.countersearch_state != CountersearchState.COMPLETE
        ):
            raise ValueError("a completed manifest requires complete countersearch state")
        if self.readiness == Readiness.READY:
            if self.countersearch_state != CountersearchState.COMPLETE:
                raise ValueError("ready interpretations require completed countersearch")
            if not self.alternatives:
                raise ValueError("ready interpretations require alternative explanations")
            if EvidenceRole.CONTRADICTS in roles and not self.strongest_counterexample:
                raise ValueError("ready interpretations must expose their counterexample")
        if self.owner_verdict == InterpretationVerdict.PARTIAL:
            if self.owner_kept_part is None or self.thesis != self.owner_kept_part:
                raise ValueError("a partial verdict must expose exactly its kept part")
        elif self.owner_kept_part is not None:
            raise ValueError("only a partial verdict may carry a kept part")
        if (
            self.owner_verdict == InterpretationVerdict.REJECT
            and self.readiness != Readiness.REJECTED
        ):
            raise ValueError("a rejected owner verdict requires rejected readiness")
        return self


__all__ = [
    "AlternativeExplanation",
    "AttributionState",
    "BooleanFactValue",
    "ContextCompleteness",
    "ConversationEpisode",
    "CountersearchLane",
    "CountersearchManifest",
    "CountersearchState",
    "DateAmbiguity",
    "DateAmbiguityKind",
    "DateFactValue",
    "DerivationKind",
    "DialogueContext",
    "EntityKind",
    "EntityRefFactValue",
    "EntryId",
    "EpisodeId",
    "Event",
    "EvidenceReference",
    "EvidenceRole",
    "ExtensionFactValue",
    "Fact",
    "FactStatus",
    "FactValue",
    "FactValueKind",
    "GenerationId",
    "Goal",
    "GoalKind",
    "GoalStatus",
    "Interpretation",
    "InterpretationCandidate",
    "InterpretationEvidence",
    "InterpretationKind",
    "InterpretationVerdict",
    "InterpretationScope",
    "LocationFactValue",
    "MonthDayFactValue",
    "NumberFactValue",
    "Observation",
    "ObservationKind",
    "ObservationModality",
    "OmittedRange",
    "Person",
    "Polarity",
    "Preference",
    "PreferenceValence",
    "ProducerKind",
    "ProducerMetadata",
    "Project",
    "Readiness",
    "RecordState",
    "RelationshipMeasurement",
    "RelationshipSnapshot",
    "ReplyEdge",
    "ScopeKind",
    "SourceTurn",
    "StableId",
    "SubjectReference",
    "TemporalScope",
    "TextFactValue",
    "CandidateAssemblyError",
    "TextSpan",
    "VerificationState",
    "conversation_episode_id",
    "derived_record_id",
]
