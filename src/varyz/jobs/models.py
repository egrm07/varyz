"""Pure durable-job contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    job_key: str
    resource_type: str
    resource_id: str
    task: str
    version: str
    state: JobState
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    error_category: str | None = None
    error_detail: str | None = None
    next_retry_at: str | None = None
    input_content_hash: str
    output_generation_id: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


def job_key(resource_type: str, resource_id: str, task: str, version: str) -> str:
    return f"{resource_type}:{resource_id}:{task}:{version}"
