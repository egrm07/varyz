"""[CONTRACT — do not edit] Ports for the owner-resolution service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from varyz.personal_model.query import AnswerBasis
from varyz.resolution.models import Caller, Resolution, ResolutionRequest


class KnowledgeStore(Protocol):
    """Read and append explicit owner knowledge without mutating prior entries."""

    def try_resolve(self, request: ResolutionRequest) -> Resolution | None: ...

    def remember(
        self,
        *,
        question: str,
        answer: str,
        caller: Caller,
        basis: AnswerBasis,
        source: str,
        device_id: str,
        supersedes_id: str | None = None,
        escalation_id: str | None = None,
    ) -> str: ...


class EscalationQueue(Protocol):
    """Durable pending questions and their eventual owner answers."""

    def enqueue(
        self,
        request: ResolutionRequest,
        *,
        tried_tiers: tuple[int, ...],
        urgency: str,
    ) -> str: ...

    def poll(self, escalation_id: str) -> Resolution | None: ...

    def answer(self, escalation_id: str, answer: str, *, device_id: str) -> Resolution: ...

    def expire(self, *, before: datetime) -> int: ...


class Notifier(Protocol):
    """Content-free notification boundary; question text is never accepted."""

    def notify(self, escalation_id: str, urgency: str) -> None: ...


class ResolverTier(Protocol):
    def try_resolve(self, request: ResolutionRequest) -> Resolution | None: ...
