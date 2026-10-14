"""Pure privacy, consent, decision, receipt, and destination contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from varyz.domain import Sensitivity


def canonical_json(value: Any) -> bytes:
    def default(obj: Any) -> str:
        if isinstance(obj, datetime):
            return obj.astimezone(UTC).isoformat()
        if isinstance(obj, StrEnum):
            return str(obj)
        raise TypeError(type(obj).__name__)

    return json.dumps(
        value, default=default, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    LOCAL_ONLY = "local_only"
    TRANSFORM = "transform"
    ASK = "ask"
    AGGREGATE_ONLY = "aggregate_only"


class LabelSource(StrEnum):
    CONNECTOR_DEFAULT = "connector_default"
    DETERMINISTIC_RULE = "deterministic_rule"
    INHERITED = "inherited"
    LOCAL_CLASSIFIER = "local_classifier"
    USER_OVERRIDE = "user_override"
    DESTINATION_POLICY = "destination_policy"


class RulePrecedence(StrEnum):
    EXPLICIT_DENY = "explicit_deny"
    USER_RESOURCE_OVERRIDE = "user_resource_override"
    REVOKED_CONSENT = "revoked_consent"
    DESTINATION = "destination"
    PURPOSE = "purpose"
    SOURCE_DEFAULT = "source_default"
    SYSTEM_DEFAULT = "system_default"


class Interaction(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


class ContextRequest(BaseModel):
    """Declared before protected context is selected."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    actor: str
    purpose: str
    destination: str
    intent: str
    intent_hash: str
    requested_types: tuple[str, ...]
    requested_scope: dict[str, Any] = Field(default_factory=dict)
    max_items: int = Field(ge=0)
    max_chars: int = Field(ge=0)
    interaction: Interaction
    user_present: bool
    reversible: bool
    consent_grant_id: str | None = None
    conversation_id: str | None = None
    policy_version: str = "1"
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def hash_matches(self) -> ContextRequest:
        if self.intent_hash != hashlib.sha256(self.intent.encode("utf-8")).hexdigest():
            raise ValueError("intent_hash does not match intent")
        return self


class ContextItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    resource_type: str
    resource_id: str
    content: str
    sensitivity: Sensitivity
    label_source: LabelSource = LabelSource.INHERITED
    label_reasons: tuple[str, ...] = ()
    source: str | None = None
    source_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def input_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    name: str
    precedence: RulePrecedence
    priority: int = 0
    effect: PolicyEffect
    reason_code: str
    version: str = "1"
    actor: str | None = None
    purpose: str | None = None
    destination: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    source: str | None = None
    sensitivity: Sensitivity | None = None
    transformation: str | None = None
    active: bool = True


class ConsentGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    actor: str
    destination: str
    purpose: str
    request_id: str | None = None
    conversation_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    entity_id: str | None = None
    topic: str | None = None
    scope_valid_from: datetime | None = None
    scope_valid_to: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    max_items: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=0)
    used_items: int = Field(default=0, ge=0)
    used_chars: int = Field(default=0, ge=0)
    revoked_at: datetime | None = None

    @field_validator(
        "scope_valid_from",
        "scope_valid_to",
        "valid_from",
        "valid_until",
        "revoked_at",
        mode="after",
    )
    @classmethod
    def normalize_timezone(cls, value: datetime | None) -> datetime | None:
        return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def valid_ranges(self) -> ConsentGrant:
        if self.resource_id is not None and self.resource_type is None:
            raise ValueError("resource_id requires resource_type")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until < self.valid_from
        ):
            raise ValueError("grant expiration precedes its start")
        if (
            self.scope_valid_from is not None
            and self.scope_valid_to is not None
            and self.scope_valid_to < self.scope_valid_from
        ):
            raise ValueError("grant content scope ends before it starts")
        return self

    def is_active(self, now: datetime) -> bool:
        now = now if now.tzinfo else now.replace(tzinfo=UTC)
        return (
            self.revoked_at is None
            and (self.valid_from is None or now >= self.valid_from)
            and (self.valid_until is None or now <= self.valid_until)
        )


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    resource_type: str
    resource_id: str
    effect: PolicyEffect
    reason_codes: tuple[str, ...]
    policy_version: str
    transformation: str | None = None
    input_hash: str


class TransformedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    resource_type: str
    resource_id: str
    content: str
    source_ids: tuple[str, ...]
    transformation: str | None = None
    transformation_version: str | None = None
    input_hash: str
    output_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreparedEgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: ContextRequest
    decisions: tuple[PolicyDecision, ...]
    items: tuple[TransformedItem, ...]
    payload: bytes
    payload_hash: str
    approval_required: bool
    blocked: bool
    receipt_id: str
    policy_version: str
    transformation_versions: dict[str, str] = Field(default_factory=dict)
    # Kinds of credential the broker removed on the way out, never their values.
    credentials_removed: tuple[str, ...] = ()

    @property
    def char_count(self) -> int:
        return sum(len(item.content) for item in self.items)


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    payload_hash: str
    actor: str
    approved_at: datetime
    expires_at: datetime | None = None
    allow_duplicate_risk: bool = False

    @field_validator("approved_at", "expires_at", mode="after")
    @classmethod
    def normalize_timezone(cls, value: datetime | None) -> datetime | None:
        return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def valid_window(self) -> Approval:
        if self.expires_at is not None and self.expires_at < self.approved_at:
            raise ValueError("approval expires before it is granted")
        return self

    def valid_for(self, prepared: PreparedEgress, now: datetime) -> bool:
        return (
            self.request_id == prepared.request.request_id
            and self.payload_hash == prepared.payload_hash
            and self.actor == prepared.request.actor
            and self.approved_at <= now
            and (self.expires_at is None or now <= self.expires_at)
        )


class EgressResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    request_id: str
    payload_hash: str
    state: str
    result_category: str
    response: Any = None
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    external_action_id: str | None = None
    reversible: bool = False
    receipt_id: str
    execution_id: str | None = None
    replayed: bool = False


class AdapterResult(BaseModel):
    """Classified destination result without leaking adapter-specific types."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    response: Any = None
    result_category: str = "success"
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    external_action_id: str | None = None
    reversible: bool | None = None


class DestinationAdapter(Protocol):
    """Trusted in-process transport boundary.

    The broker writes `started` before calling this interface and verifies its
    contract and result, but cannot sandbox or preempt arbitrary Python code.
    Implementations must send only `prepared.payload` and enforce
    `timeout_seconds`. Hostile-adapter isolation requires an out-of-process
    transport protocol; post-call verification can audit mutation, not undo it.
    """

    destination: str
    destination_type: str
    version: str
    supports_idempotency: bool
    produces_external_effect: bool
    reversible_default: bool
    timeout_seconds: float
    max_retries: int

    def execute(
        self, prepared: PreparedEgress, *, idempotency_key: str
    ) -> AdapterResult | Any: ...

    def classify_error(self, error: Exception) -> str: ...


class PolicyEngineContract(Protocol):
    version: str

    def evaluate(
        self,
        request: ContextRequest,
        items: tuple[ContextItem, ...],
        *,
        rules: tuple[PolicyRule, ...] = (),
        grants: tuple[ConsentGrant, ...] = (),
    ) -> tuple[PolicyDecision, ...]: ...


class TransformationRegistryContract(Protocol):
    def apply(
        self, name: str, item: ContextItem, request: ContextRequest
    ) -> TransformedItem: ...


class ReceiptStoreContract(Protocol):
    def record_preparation(
        self,
        prepared: PreparedEgress,
        considered: tuple[ContextItem, ...],
    ) -> str: ...
