"""Strict shared schema for anonymized Varyz network contributions.

Shared by the local collector and the Cloud receiver, so neither can drift into
accepting a field the other never meant to send. No archive or runtime imports.
"""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

NETWORK_CONSENT: Literal["network-v1"] = "network-v1"
Kind = Literal["correction", "escalation", "rule"]
Agent = Literal["claude", "codex", "chatgpt", "gemini", "cursor", "other"]
Outcome = Literal[
    "adopted", "overridden", "corrected", "answered", "confirmed", "revised", "rejected"
]
_LABEL = r"^[a-z0-9_]{1,32}$"
MAX_TEXT = 1200


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("expected a canonical UUID")
    return value


class Contribution(BaseModel):
    """One scrubbed example. Text fields hold placeholders, never raw identifiers."""

    model_config = ConfigDict(extra="forbid", strict=True)
    record_id: str
    kind: Kind
    day: str
    agent: Agent
    outcome: Outcome
    question: str = Field(default="", max_length=MAX_TEXT)
    answer: str = Field(default="", max_length=MAX_TEXT)
    correction: str = Field(default="", max_length=MAX_TEXT)
    basis: str = Field(default="", pattern=r"^[a-z_]{0,32}$")
    tier: int = Field(default=0, ge=0, le=5)
    confidence: float = Field(default=0, ge=0, le=1)
    urgency: str = Field(default="", pattern=r"^[a-z_]{0,32}$")
    tried_tiers: list[str] = Field(default_factory=list, max_length=8)
    seconds_to_answer: int = Field(default=0, ge=0, le=31_536_000)
    rule_kind: str = Field(default="", pattern=r"^[a-z_]{0,32}$")
    rule_scope: Literal["", "general", "project"] = ""

    @field_validator("record_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _uuid(value)

    @field_validator("day")
    @classmethod
    def recent_day(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        today = datetime.now(UTC).date()
        if value != parsed.isoformat() or not today - timedelta(days=30) <= parsed <= today:
            raise ValueError("day must be within the last 30 UTC days")
        return value

    @field_validator("tried_tiers")
    @classmethod
    def labels(cls, value: list[str]) -> list[str]:
        if any(not isinstance(v, str) or not re.match(_LABEL, v) for v in value):
            raise ValueError("tried_tiers must be short labels")
        return value


class ContributionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    consent_version: Literal["network-v1"] = NETWORK_CONSENT
    batch_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]{0,24})$", max_length=40)
    records: list[Contribution] = Field(min_length=1, max_length=200)

    @field_validator("batch_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _uuid(value)
