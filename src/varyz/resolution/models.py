"""[CONTRACT — do not edit] Transport-independent resolution models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from varyz.personal_model.query import (
    AnswerBasis,
    LifeQueryResult,
    QueryDisposition,
    WithheldRecordMarker,
)


class Caller(BaseModel):
    """Who is asking, independent of how the request arrived."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    display_name: str
    purpose: str
    capability_id: str | None = None
    device_id: str
    transport: str = "stdio"

    @field_validator("agent_id", "display_name", "purpose", "device_id", "transport")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("caller fields cannot be blank")
        return value.strip()


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1, max_length=2000)
    caller: Caller
    purpose: str
    conversation_id: str | None = None
    max_wait_seconds: int = Field(default=0, ge=0, le=300)
    allow_escalation: bool = True
    # The project the agent is working in. A one-off product decision recorded for
    # one project never answers for another; ``None`` means project unknown.
    project: str | None = Field(default=None, max_length=200)

    @field_validator("project")
    @classmethod
    def project_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.casefold().split())
        return value if value and value not in {"*", "all", "global"} else None

    @field_validator("question", "purpose")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resolution request fields cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def purpose_matches_caller(self) -> ResolutionRequest:
        if self.purpose != self.caller.purpose:
            raise ValueError("request purpose must match the caller capability purpose")
        return self


class Resolution(BaseModel):
    """Wraps LifeQueryResult; it does not replace the existing query result."""

    model_config = ConfigDict(frozen=True)

    disposition: QueryDisposition
    basis: AnswerBasis | None
    answer: str | None
    tier: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    life_result: LifeQueryResult | None = None
    escalation_id: str | None = None
    withheld: tuple[WithheldRecordMarker, ...] = ()
    egress_request_id: str | None = None
    resolved_at: datetime
    monotonic_seq: int = Field(ge=0)

    @field_validator("resolved_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("resolved_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def coherent_outcome(self) -> Resolution:
        if self.disposition == QueryDisposition.NOT_KNOWN:
            if self.basis is not None or self.answer is not None:
                raise ValueError("not-known resolutions have neither a basis nor an answer")
        elif self.basis is None:
            raise ValueError("non-empty dispositions require an answer basis")
        if self.basis == AnswerBasis.ESCALATED:
            if self.escalation_id is None or self.disposition != QueryDisposition.CLARIFICATION:
                raise ValueError("escalated resolutions require a clarification and escalation id")
            if self.answer is not None:
                raise ValueError("pending escalations cannot carry an answer")
        elif self.escalation_id is not None and self.answer is None:
            raise ValueError("only a pending escalation may carry an unanswered escalation id")
        return self
