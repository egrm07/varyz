"""Strict shared schema for feedback agents send to the Varyz developer.

Shared by the local sender and the Cloud receiver, as contribution_schema is, so
neither can accept a field the other never meant to send. No archive or runtime
imports.
"""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from varyz.analytics_schema import OperatingSystem
from varyz.contribution_schema import MODEL, Agent

Area = Literal[
    "setup", "connection", "sync", "cloud", "rules", "questions", "reading", "memory",
    "speed", "other",
]
NoticedBy = Literal["agent", "user"]
MAX_TITLE = 120
MAX_DETAILS = 1200  # What the scrubber keeps of one text.
MAX_EXPECTED = 600
MAX_CHECKS = 60


class Feedback(BaseModel):
    """One report. Text is written by the agent and scrubbed before it leaves."""

    model_config = ConfigDict(extra="forbid", strict=True)
    feedback_id: str
    day: str
    title: str = Field(min_length=3, max_length=MAX_TITLE)
    details: str = Field(min_length=3, max_length=MAX_DETAILS)
    expected: str = Field(default="", max_length=MAX_EXPECTED)
    area: Area = "other"
    noticed_by: NoticedBy = "agent"
    agent: Agent = "other"
    model: str = Field(default="", max_length=64, pattern=MODEL)
    version: str = Field(pattern=r"^[0-9A-Za-z.+-]{1,40}$")
    os: OperatingSystem
    # What `varyz doctor` said at the time: how many checks ran and which failed.
    doctor_checks: int = Field(default=0, ge=0, le=500)
    doctor_failing: list[str] = Field(default_factory=list, max_length=MAX_CHECKS)

    @field_validator("feedback_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("expected a canonical UUID")
        return value

    @field_validator("day")
    @classmethod
    def _day(cls, value: str) -> str:
        day = date.fromisoformat(value)
        today = datetime.now(UTC).date()
        if day.isoformat() != value or not today - timedelta(days=60) <= day <= today + timedelta(
            days=1
        ):
            raise ValueError("expected a recent day")
        return value

    @field_validator("doctor_failing")
    @classmethod
    def _checks(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z0-9 _.-]{1,60}", name) for name in value):
            raise ValueError("expected doctor check names")
        return value
