"""Strict shared aggregate schema; no archive or local runtime imports."""

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONSENT_VERSION: Literal["aggregate-v1"] = "aggregate-v1"
OperatingSystem = Literal["Windows", "Darwin", "Linux", "Other"]

METRICS = frozenset(
    {
        "answers_answered",
        "answers_ambiguous",
        "answers_not_known",
        "answers_clarification",
        "basis_explicit",
        "basis_reported",
        "basis_observed",
        "basis_inferred",
        "repeated_questions",
        "outcome_adopted",
        "outcome_corrected",
        "outcome_overridden",
        "correction_wrong_preference",
        "correction_stale_rule",
        "correction_wrong_scope",
        "correction_agent_ignored",
        "correction_other",
        "rules_confirmed",
        "rules_revised",
        "rules_rejected",
        "trial_kept",
        "trial_dropped",
        "latency_under_100ms",
        "latency_under_1s",
        "latency_under_5s",
        "latency_over_5s",
        "resolution_failed",
        "sync_ok",
        "sync_failed",
        "repair_recovered",
        "repair_blocked",
        "setup_started",
        "setup_ready",
        "setup_failed",
        "collection_failed",
        *(f"setup_{step}_{state}" for step in range(7) for state in ("done", "failed", "skipped")),
    }
)


class AggregateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    consent_version: Literal["aggregate-v1"] = CONSENT_VERSION
    sample_kind: Literal["usage", "probe"] = "usage"
    report_id: str
    day: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]{0,24})$", max_length=40)
    os: OperatingSystem
    counters: dict[str, int] = Field(min_length=1, max_length=len(METRICS))

    @field_validator("report_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("report_id must be a canonical UUID")
        return value

    @field_validator("day")
    @classmethod
    def valid_day(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        today = datetime.now(UTC).date()
        if value != parsed.isoformat() or not today - timedelta(days=30) <= parsed <= today:
            raise ValueError("day must be within the last 30 UTC days")
        return value

    @field_validator("counters")
    @classmethod
    def safe_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if not value.keys() <= METRICS or any(
            type(n) is not int or not 1 <= n <= 1000000 for n in value.values()
        ):
            raise ValueError("only bounded, named integer counters are accepted")
        return value
