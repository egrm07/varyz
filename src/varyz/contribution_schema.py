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
Kind = Literal[
    "correction", "escalation", "rule", "check", "answer", "decision", "use", "session",
    "recipe",
]
Agent = Literal["claude", "codex", "chatgpt", "gemini", "cursor", "other"]
Outcome = Literal[
    "adopted", "overridden", "corrected", "answered", "confirmed", "revised", "rejected",
    "passed", "failed", "dismissed", "unlabelled", "decided", "shown", "complete", "blocked",
    "abandoned",
]
CheckStatus = Literal["", "passed", "failed", "not_applicable", "unchecked"]
Hit = Literal["", "hit", "miss"]
# A model family and version as agents report them, never free text.
MODEL = (r"^(?:(?:claude|gpt|o[1-9]|gemini|codex|grok|llama|qwen|deepseek|mistral|kimi|glm|"
         r"composer|sonnet|opus|haiku|fable)(?:[-._:][a-z0-9]+){0,8})?$")
# Who settled a label: the owner, a task that finished with the answer in use, or the
# agent's own report. Buyers weigh labels by this.
LabelSource = Literal["", "owner", "owner_replaced", "task_finished", "inspected",
                      "agent_report", "other"]
_LABEL = r"^[a-z0-9_]{1,32}$"
MAX_TEXT = 1200
MAX_NOTE = 300
MAX_ITEM = 180  # One fact in a session report, as record_progress allows.
MAX_STEP = 200  # One recipe step and how it is done, as record_recipe allows.
_REF = r"^(?:[0-9a-f]{20})?$"


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
    # Opaque per-installation references, so a rule, the rule it replaced and the
    # task it was checked in link up without any id leaving the computer. They are
    # salted per opt-in, so they never link across installations or opt-ins.
    rule_ref: str = Field(default="", pattern=_REF)
    replaces_ref: str = Field(default="", pattern=_REF)
    task_ref: str = Field(default="", pattern=_REF)
    label_source: LabelSource = ""
    attempt: int = Field(default=0, ge=0, le=100)
    # The correction pair, as the agent or reader paraphrased it: what the agent
    # did and what the owner wanted instead. Never the agent's raw output.
    agent_did: str = Field(default="", max_length=MAX_NOTE)
    wanted: str = Field(default="", max_length=MAX_NOTE)
    # The model that did the work, as the agent reported it.
    model: str = Field(default="", max_length=64, pattern=MODEL)
    # The rule's previous result in the same task ('' on the first), so a retry is
    # told from a first try. On a corrected check, 'passed' is a false "done".
    previous: CheckStatus = ""
    # The owner's memory test: whether the rule was shown at the task's start or
    # held back until its check, drawn at random.
    arm: Literal["", "shown", "held_out"] = ""
    # Whether a question needed the owner, or the agent should have asked one.
    ask_label: Literal["", "needed", "unneeded", "should_have_asked", "dropped"] = ""
    # Before the owner answered: the agent's guess, and whether it and the owner's
    # nearest earlier answer matched what they said.
    agent_guess: str = Field(default="", max_length=MAX_NOTE)
    agent_guess_hit: Hit = ""
    memory_guess_hit: Hit = ""
    # How preferences move: rules before this one, and how long the replaced one lasted.
    chain_depth: int = Field(default=0, ge=0, le=50)
    replaced_after_days: int = Field(default=0, ge=0, le=36_500)
    # Links an unlabelled `answer` to the `correction` that labels it later.
    ledger_ref: str = Field(default="", pattern=_REF)
    # `escalation` asked while the owner was away: how the agent classed the
    # question, the choice it proposed and whether the owner then chose it.
    decision_kind: Literal["", "preference", "delegated", "blocking", "permission"] = ""
    proposed_choice: str = Field(default="", max_length=MAX_NOTE)
    proposed_hit: Hit = ""
    # `session`: the working agent's own report of a finished, blocked or abandoned
    # task (task in `question`), each fact scrubbed. File names and artifacts stay.
    done: list[str] = Field(default_factory=list, max_length=6)
    checks: list[str] = Field(default_factory=list, max_length=6)
    pending: list[str] = Field(default_factory=list, max_length=6)
    # `recipe`: how an agent got a task done once it had worked it out. The task's
    # name is in `question`, how to tell it worked in `answer`, the trap in
    # `correction`, and the steps here, each scrubbed. A later use of the recipe
    # carries only references and its outcome (with what went wrong when it failed).
    steps: list[str] = Field(default_factory=list, max_length=8)

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

    @field_validator("done", "checks", "pending")
    @classmethod
    def facts(cls, value: list[str]) -> list[str]:
        if any(not isinstance(v, str) or len(v) > MAX_ITEM for v in value):
            raise ValueError("session facts are short strings")
        return value

    @field_validator("steps")
    @classmethod
    def recipe_steps(cls, value: list[str]) -> list[str]:
        if any(not isinstance(v, str) or len(v) > MAX_STEP for v in value):
            raise ValueError("recipe steps are short strings")
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
