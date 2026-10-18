"""Pure graph domain contracts: no SQLite, web, CLI, or provider imports."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from varyz.domain import Sensitivity, TimePrecision


def stable_hash(*parts: Any) -> str:
    material = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EntityType(StrEnum):
    PERSON = "person"
    PLACE = "place"
    ORGANIZATION = "organization"
    PROJECT = "project"
    OBJECT = "object"
    EVENT = "event"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    DELETED = "deleted"


class AliasVerification(StrEnum):
    VERIFIED = "verified"
    AUTOMATIC = "automatic"
    CANDIDATE = "candidate"
    REJECTED = "rejected"


class ObjectType(StrEnum):
    ENTITY = "entity"
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    INTERVAL = "interval"
    LOCATION = "location"
    BOOLEAN = "boolean"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class ClaimModality(StrEnum):
    FACT = "fact"
    PLAN = "plan"
    BELIEF = "belief"
    FEELING = "feeling"
    DREAM = "dream"
    REPORT = "report"
    REQUEST = "request"
    PROMISE = "promise"
    INFERENCE = "inference"


class ClaimState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class PlanState(StrEnum):
    PLANNED = "planned"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class EvidenceRole(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    UPDATES = "updates"
    MENTIONS = "mentions"


class ClaimRelationType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DUPLICATES = "duplicates"
    NARROWS = "narrows"
    BROADENS = "broadens"
    CAUSED_BY = "caused_by"
    CORRELATES_WITH = "correlates_with"


class GraphRunState(StrEnum):
    STAGING = "staging"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    entity_type: EntityType
    canonical_label: str
    status: EntityStatus = EntityStatus.ACTIVE
    merged_into_id: str | None = None
    created_at: str
    updated_at: str
    graph_generation_id: str | None = None
    generation_version: str


class EntityAlias(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    entity_id: str
    namespace: str
    alias_value: str
    normalized_value: str
    source_entry_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    verification_state: AliasVerification
    fingerprint: str


class IdentityCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    left_entity_id: str
    right_entity_id: str
    reason_code: str
    score: float = Field(ge=0, le=1)
    explanation: str
    state: str = "pending"
    fingerprint: str


class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_id: str
    quote: str = Field(min_length=1)
    chunk_id: int | None = None
    speaker_key: str | None = None
    evidence_role: EvidenceRole = EvidenceRole.SUPPORTS
    confidence: float = Field(default=1.0, ge=0, le=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_offsets(self) -> EvidenceCandidate:
        if self.char_end is not None and self.char_start is None:
            raise ValueError("char_end requires char_start")
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end < self.char_start
        ):
            raise ValueError("char_end precedes char_start")
        return self


class ClaimCandidate(BaseModel):
    """Validated output from deterministic or model extractors."""

    model_config = ConfigDict(frozen=True)

    subject_key: str
    predicate: str
    predicate_version: str = "1"
    object_type: ObjectType
    object_value: Any
    object_surface: str
    object_entity_key: str | None = None
    polarity: Polarity = Polarity.POSITIVE
    modality: ClaimModality = ClaimModality.FACT
    asserted_by_key: str | None = None
    observed_at: datetime | None = None
    observed_tz: str | None = None
    asserted_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    temporal_precision: TimePrecision = TimePrecision.UNKNOWN
    temporal_inferred: bool = False
    original_time_text: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED
    plan_state: PlanState | None = None
    # "life" or "work". None where no extractor judged it, which reads as life.
    scope: str | None = None
    origin: str = "deterministic"
    evidence: tuple[EvidenceCandidate, ...] = ()

    @model_validator(mode="after")
    def valid_claim(self) -> ClaimCandidate:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        if self.object_type == ObjectType.ENTITY and not self.object_entity_key:
            raise ValueError("entity objects require object_entity_key")
        if self.origin != "user" and not self.evidence:
            raise ValueError("derived claims require evidence")
        if self.modality == ClaimModality.PLAN and self.plan_state is None:
            object.__setattr__(self, "plan_state", PlanState.UNKNOWN)
        return self

    def fingerprint(self, subject_entity_id: str, object_entity_id: str | None = None) -> str:
        return stable_hash(
            subject_entity_id,
            self.predicate,
            self.predicate_version,
            self.object_type,
            self.object_value,
            object_entity_id,
            self.polarity,
            self.modality,
            self.asserted_at.isoformat() if self.asserted_at else None,
            self.valid_from.isoformat() if self.valid_from else None,
            self.valid_to.isoformat() if self.valid_to else None,
            sorted(ev.entry_id for ev in self.evidence),
        )


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    subject_entity_id: str
    predicate: str
    predicate_version: str
    object_type: ObjectType
    object_value: Any
    object_surface: str
    object_entity_id: str | None = None
    polarity: Polarity
    modality: ClaimModality
    asserted_by_entity_id: str | None = None
    observed_at: str | None = None
    observed_tz: str | None = None
    asserted_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    temporal_precision: TimePrecision
    temporal_inferred: bool = False
    original_time_text: str | None = None
    confidence: float = Field(ge=0, le=1)
    sensitivity: Sensitivity
    extractor_name: str
    extractor_model: str | None = None
    prompt_version: str | None = None
    fingerprint: str
    lifecycle_state: ClaimState
    plan_state: PlanState | None = None
    scope: str | None = None
    origin: str
    graph_generation_id: str


class EvidenceLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    claim_id: str
    entry_id: str
    chunk_id: int | None = None
    quote: str
    speaker_entity_id: str | None = None
    evidence_role: EvidenceRole
    confidence: float = Field(ge=0, le=1)
    char_start: int | None = None
    char_end: int | None = None
    sensitivity: Sensitivity
    graph_generation_id: str
    fingerprint: str
    created_at: str


class ClaimRelation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    from_claim_id: str
    to_claim_id: str
    relation_type: ClaimRelationType
    confidence: float = Field(ge=0, le=1)
    explanation: str = ""
    graph_generation_id: str
    fingerprint: str
    created_at: str


class GraphRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    generation_key: str
    extractor_name: str
    extractor_version: str
    model: str | None = None
    prompt_version: str | None = None
    predicate_version: str
    identity_version: str
    input_content_hash: str
    state: GraphRunState
    entries_total: int = 0
    entries_processed: int = 0
    checkpoint_entry_id: str | None = None
    error: str | None = None
    started_at: str
    completed_at: str | None = None
    activated_at: str | None = None


class ExtractionBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    aliases: tuple[dict[str, Any], ...] = ()
    claims: tuple[ClaimCandidate, ...] = ()
    quarantined: tuple[dict[str, Any], ...] = ()


class GraphExtractor(Protocol):
    name: str
    version: str
    prompt_version: str | None
    model: str | None

    def extract(self, entry: Any) -> ExtractionBatch: ...


class IdentityResolver(Protocol):
    version: str

    def resolve(self, alias: dict[str, Any]) -> Any: ...
