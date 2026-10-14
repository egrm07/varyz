"""Durable escalation queue and owner-answer learning loop."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.personal_model.query import AnswerBasis, QueryDisposition
from varyz.resolution.away import status as away_status
from varyz.resolution.knowledge import (
    OWNER_ID,
    SQLiteKnowledgeStore,
    _datetime,
    _iso,
    _next_monotonic_seq,
    _write_transaction,
    knowledge_resolution,
    normalize_question,
)
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.ports import Notifier
from varyz.resolution.scope import suggest_scope


class NullNotifier:
    def notify(self, escalation_id: str, urgency: str) -> None:
        return None


class SQLiteEscalationQueue:
    """A restart-safe queue whose answers become tier-one knowledge."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        knowledge: SQLiteKnowledgeStore,
        *,
        notifier: Notifier | None = None,
        device_id: str = "local",
        clock: Any = utc_now,
        scope_model: Any = None,
    ) -> None:
        self._conn = conn
        self._knowledge = knowledge
        # The local model that reads an unscoped answer; None keeps the heuristics.
        self._scope_model = scope_model
        self._notifier = notifier or NullNotifier()
        self._device_id = device_id
        self._clock = clock
        self._lock = threading.RLock()
        self.notification_errors: list[str] = []

    def enqueue(
        self,
        request: ResolutionRequest,
        *,
        tried_tiers: tuple[int, ...],
        urgency: str,
        recipient_id: str = OWNER_ID,
    ) -> str:
        """Queue a question for ``recipient_id``, the owner unless routed elsewhere.

        The notifier hears only the escalation id and urgency: never the question,
        and never who it is for.
        """
        urgency = urgency.strip()
        recipient_id = recipient_id.strip()
        if not urgency:
            raise ValueError("urgency cannot be blank")
        if not recipient_id:
            raise ValueError("recipient_id cannot be blank")
        normalized = normalize_question(request.question)
        deferred = away_status(self._conn, request.project, request.conversation_id)["active"]
        created = False
        with _write_transaction(self._conn, self._lock):
            existing = self._conn.execute(
                """SELECT id FROM resolution_escalations
                   WHERE normalized_question = ? AND status = 'pending'
                     AND COALESCE(project, '') = ? AND recipient_id = ?""",
                (normalized, request.project or "", recipient_id),
            ).fetchone()
            if existing is not None:
                if deferred:
                    self._defer(str(existing["id"]), request)
                return str(existing["id"])
            escalation_id = new_id()
            seq = _next_monotonic_seq(self._conn, self._device_id)
            now = self._clock()
            self._conn.execute(
                """INSERT INTO resolution_escalations
                   (id, question, normalized_question, caller_agent_id,
                    caller_display_name, caller_purpose, caller_capability_id,
                    caller_device_id, caller_transport, purpose, conversation_id,
                    tried_tiers_json, urgency, status, created_device_id,
                    created_monotonic_seq, created_at, project, recipient_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?,
                           ?)""",
                (
                    escalation_id,
                    request.question,
                    normalized,
                    request.caller.agent_id,
                    request.caller.display_name,
                    request.caller.purpose,
                    request.caller.capability_id,
                    request.caller.device_id,
                    request.caller.transport,
                    request.purpose,
                    request.conversation_id,
                    json.dumps(tried_tiers),
                    urgency,
                    self._device_id,
                    seq,
                    _iso(now),
                    request.project,
                    recipient_id,
                ),
            )
            created = True
            if deferred:
                self._defer(escalation_id, request)
        if created and not deferred:
            try:
                self._notifier.notify(escalation_id, urgency)
            except Exception as exc:  # notification failure must never lose the question
                self.notification_errors.append(f"{type(exc).__name__}: {exc}")
        return escalation_id

    def _defer(self, escalation_id: str, request: ResolutionRequest) -> None:
        from varyz.resolution.knowledge import normalize_scope

        self._conn.execute(
            """INSERT OR IGNORE INTO away_reviews
               (escalation_id, project, conversation_id) VALUES (?, ?, ?)""",
            (escalation_id, normalize_scope(request.project) if request.project else "",
             request.conversation_id or ""),
        )

    def poll(self, escalation_id: str) -> Resolution | None:
        row = self._conn.execute(
            "SELECT * FROM resolution_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "answered":
            payload = row["answer_resolution_json"]
            return Resolution.model_validate_json(payload) if payload else None
        if row["status"] == "expired":
            return Resolution(
                disposition=QueryDisposition.NOT_KNOWN,
                basis=None,
                answer=None,
                tier=5,
                confidence=1.0,
                resolved_at=_datetime(row["answered_at"] or row["created_at"]),
                monotonic_seq=int(
                    row["answered_monotonic_seq"] or row["created_monotonic_seq"]
                ),
            )
        return Resolution(
            disposition=QueryDisposition.CLARIFICATION,
            basis=AnswerBasis.ESCALATED,
            answer=None,
            tier=5,
            confidence=1.0,
            escalation_id=escalation_id,
            resolved_at=_datetime(row["created_at"]),
            monotonic_seq=int(row["created_monotonic_seq"]),
        )

    def answer(
        self,
        escalation_id: str,
        answer: str,
        *,
        device_id: str,
        source: str = "escalation",
        scope: str | None = None,
        author_id: str | None = None,
    ) -> Resolution:
        """Close a pending question; ``source`` records who carried the answer in.

        ``scope`` is '*' when the owner meant their general way of working, or a
        project name for a one-off choice. When the owner did not say, the default
        comes from :func:`suggest_scope` and fails narrow to the asking project.
        ``author_id`` defaults to the question's recipient: whoever it was routed
        to is who answered it.
        """
        answer = answer.strip()
        if not answer:
            raise ValueError("answer cannot be blank")
        with _write_transaction(self._conn, self._lock):
            row = self._conn.execute(
                "SELECT * FROM resolution_escalations WHERE id = ?", (escalation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown escalation: {escalation_id}")
            if row["status"] != "pending":
                existing = self.poll(escalation_id)
                if row["status"] == "answered" and existing is not None:
                    return existing
                raise ValueError(f"escalation is {row['status']}")
            caller = self._caller(row)
            if scope is None:
                scope = suggest_scope(
                    row["question"], answer, _row_project(row), model=self._scope_model
                ).scope
            knowledge_id = self._knowledge.remember(
                question=row["question"],
                answer=answer,
                caller=caller,
                basis=AnswerBasis.USER_CONFIRMED,
                source=source,
                device_id=device_id,
                escalation_id=escalation_id,
                scope=scope,
                author_id=author_id or _row_recipient(row),
            )
            entry = self._knowledge.get(knowledge_id)
            if entry is None:  # pragma: no cover - same transaction inserted it
                raise RuntimeError("answered escalation did not create knowledge")
            result = knowledge_resolution(
                entry, confidence=1.0, escalation_id=escalation_id
            )
            closed_seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """UPDATE resolution_escalations
                   SET status = 'answered', answer_knowledge_id = ?,
                       answer_resolution_json = ?, answered_device_id = ?,
                       answered_monotonic_seq = ?, answered_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (
                    knowledge_id,
                    result.model_dump_json(),
                    device_id,
                    closed_seq,
                    _iso(self._clock()),
                    escalation_id,
                ),
            )
        return result

    def expire(self, *, before: datetime) -> int:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("expiry boundary must be timezone-aware")
        now = self._clock()
        count = 0
        with _write_transaction(self._conn, self._lock):
            rows = self._conn.execute(
                """SELECT id FROM resolution_escalations
                   WHERE status = 'pending' AND created_at < ?""",
                (_iso(before),),
            ).fetchall()
            for row in rows:
                seq = _next_monotonic_seq(self._conn, self._device_id)
                self._conn.execute(
                    """UPDATE resolution_escalations
                       SET status = 'expired', answered_device_id = ?,
                           answered_monotonic_seq = ?, answered_at = ?
                       WHERE id = ? AND status = 'pending'""",
                    (self._device_id, seq, _iso(now), row["id"]),
                )
                count += 1
        return count

    def dismiss(self, escalation_id: str) -> bool:
        """Close a pending question without an answer; nothing is learned from it.

        The owner's way to clear a question that no longer matters (a test run,
        an abandoned project).  Recorded as ``expired`` so the row stays in the
        audit trail.  False when it is not pending.
        """
        with _write_transaction(self._conn, self._lock):
            seq = _next_monotonic_seq(self._conn, self._device_id)
            cursor = self._conn.execute(
                """UPDATE resolution_escalations
                   SET status = 'expired', answered_device_id = ?,
                       answered_monotonic_seq = ?, answered_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (self._device_id, seq, _iso(self._clock()), escalation_id),
            )
        return cursor.rowcount == 1

    def recipient(self, escalation_id: str) -> str | None:
        """Who a question is for, or None for an unknown escalation."""
        row = self._conn.execute(
            "SELECT * FROM resolution_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        return _row_recipient(row) if row is not None else None

    def pending(self, recipient_id: str | None = None) -> tuple[dict[str, Any], ...]:
        """Open questions, oldest first; only ``recipient_id``'s when one is given."""
        rows = [
            row for row in self._conn.execute(
                """SELECT * FROM resolution_escalations
                   WHERE status = 'pending' ORDER BY created_at, id"""
            ).fetchall()
            if recipient_id is None or _row_recipient(row) == recipient_id
        ]
        return tuple(
            {
                "id": row["id"],
                "question": row["question"],
                "caller": self._caller(row).model_dump(mode="json"),
                "purpose": row["purpose"],
                "conversation_id": row["conversation_id"],
                "tried_tiers": tuple(json.loads(row["tried_tiers_json"])),
                "urgency": row["urgency"],
                "created_at": row["created_at"],
                "project": _row_project(row),
                "recipient_id": _row_recipient(row),
            }
            for row in rows
        )

    @staticmethod
    def _caller(row: sqlite3.Row) -> Caller:
        return Caller(
            agent_id=row["caller_agent_id"],
            display_name=row["caller_display_name"],
            purpose=row["caller_purpose"],
            capability_id=row["caller_capability_id"],
            device_id=row["caller_device_id"],
            transport=row["caller_transport"],
        )


def _row_project(row: sqlite3.Row) -> str | None:
    try:
        value = row["project"]
    except (IndexError, KeyError):  # read before migration 0037
        return None
    return str(value) if value else None


def _row_recipient(row: sqlite3.Row) -> str:
    try:
        value = row["recipient_id"]
    except (IndexError, KeyError):  # read before migration 0038: every question was the owner's
        return OWNER_ID
    return str(value) if value else OWNER_ID


DurableEscalationQueue = SQLiteEscalationQueue


__all__ = ["DurableEscalationQueue", "NullNotifier", "SQLiteEscalationQueue"]
