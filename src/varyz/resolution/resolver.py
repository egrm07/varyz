"""Ordered owner-resolution ladder and self-contained resolution ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, closing, nullcontext
from datetime import datetime
from itertools import count
from typing import Any

from varyz.config import Config
from varyz.domain import new_id, utc_now
from varyz.personal_model.query import (
    AnswerBasis,
    LifeQueryResult,
    LifeRelation,
    QueryDisposition,
    QueryProvenance,
    TypedAnswer,
    WithheldRecordMarker,
)
from varyz.resolution.knowledge import (
    _iso,
    _next_monotonic_seq,
    _write_transaction,
    normalize_question,
    without_project_name,
)
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.ports import EscalationQueue, ResolverTier
from varyz.retrieval.answer import recall
from varyz.retrieval.capsule import Evidence
from varyz.retrieval.search import (
    MIN_EVIDENCE_COVERAGE,
    evidence_coverage,
    lexically_grounded,
)


class SQLiteResolutionLedger:
    """Append-only audit log containing everything needed to interpret each call."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        device_id: str = "local",
        policy_version: str = "alpha-local-v1",
    ) -> None:
        self._conn = conn
        self._device_id = device_id
        self._policy_version = policy_version
        self._lock = threading.RLock()

    def append(self, request: ResolutionRequest, result: Resolution) -> Resolution:
        citations: list[dict[str, Any]] = []
        if result.life_result is not None:
            citations = [
                answer.provenance.model_dump(mode="json")
                for answer in result.life_result.answers
            ]
        with _write_transaction(self._conn, self._lock):
            seq = _next_monotonic_seq(self._conn, self._device_id)
            persisted = result.model_copy(update={"monotonic_seq": seq})
            self._conn.execute(
                """INSERT INTO resolution_ledger
                   (id, question, normalized_question, caller_agent_id,
                    caller_display_name, caller_purpose, caller_capability_id,
                    caller_device_id, caller_transport, purpose, conversation_id,
                    disposition, basis, answer, tier, confidence, escalation_id,
                    egress_request_id, policy_version, citations_json,
                    withheld_json, request_json, resolution_json, device_id,
                    monotonic_seq, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id(),
                    request.question,
                    normalize_question(request.question),
                    request.caller.agent_id,
                    request.caller.display_name,
                    request.caller.purpose,
                    request.caller.capability_id,
                    request.caller.device_id,
                    request.caller.transport,
                    request.purpose,
                    request.conversation_id,
                    persisted.disposition.value,
                    persisted.basis.value if persisted.basis else None,
                    persisted.answer,
                    persisted.tier,
                    persisted.confidence,
                    persisted.escalation_id,
                    persisted.egress_request_id,
                    self._policy_version,
                    json.dumps(citations, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        [item.model_dump(mode="json") for item in persisted.withheld],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    request.model_dump_json(),
                    persisted.model_dump_json(),
                    self._device_id,
                    seq,
                    _iso(persisted.resolved_at),
                ),
            )
        return persisted


class LifeResultTier:
    """Adapter for existing evidence or personal-model query implementations."""

    def __init__(
        self,
        tier: int,
        query: Callable[[ResolutionRequest], LifeQueryResult | None],
        *,
        allowed_bases: frozenset[AnswerBasis] | None = None,
    ) -> None:
        if tier not in {2, 3, 4}:
            raise ValueError("life-result adapters are valid only for tiers 2-4")
        self.tier = tier
        self._query = query
        self._allowed_bases = allowed_bases

    def try_resolve(self, request: ResolutionRequest) -> Resolution | None:
        life_result = self._query(request)
        if life_result is None:
            return None
        if life_result.disposition != QueryDisposition.ANSWERED or not life_result.answers:
            # Withholding is terminal and must not be accidentally converted into
            # an invented answer by a later tier.
            if life_result.withheld_records or life_result.sensitive_records_withheld:
                return Resolution(
                    disposition=QueryDisposition.NOT_KNOWN,
                    basis=None,
                    answer=None,
                    tier=self.tier,
                    confidence=1.0,
                    life_result=life_result,
                    withheld=life_result.withheld_records,
                    egress_request_id=life_result.egress_request_id,
                    resolved_at=utc_now(),
                    monotonic_seq=0,
                )
            return None
        answers = tuple(
            item
            for item in life_result.answers
            if self._allowed_bases is None or item.basis in self._allowed_bases
        )
        if not answers:
            return None
        basis = answers[0].basis
        if any(item.basis != basis for item in answers):
            basis = AnswerBasis.INFERRED if self.tier == 3 else AnswerBasis.OBSERVED
        return Resolution(
            disposition=QueryDisposition.ANSWERED,
            basis=basis,
            answer="; ".join(item.display_value for item in answers),
            tier=self.tier,
            confidence=min(item.confidence for item in answers),
            life_result=life_result,
            withheld=life_result.withheld_records,
            egress_request_id=life_result.egress_request_id,
            resolved_at=utc_now(),
            monotonic_seq=0,
        )


class EvidenceTier:
    """Tier 2: return bounded, cited archive passages or a policy withholding."""

    tier = 2

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        *,
        verifier: Any = None,
        models: Any = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._conn = conn
        # Opens a read-only connection for one question. A tier with one reads
        # nothing through `conn`, so the daemon lets it run beside other requests
        # (`own_connection`): its two seconds with the local model used to hold up
        # every agent and hook behind it.
        self._connect = connect
        self._config = config
        # ``evidence_verify.EvidenceVerifier``: the local model's look at whether
        # the passages about to be returned are about the thing asked.
        self._verifier = verifier
        # ``loaded_models.RetrievalModels``: the embedder, cross-encoder and
        # local-model judge `recall` reads the archive with when they are there.
        self._models = models

    @property
    def own_connection(self) -> bool:
        return self._connect is not None

    def _judged_evidence(
        self,
        conn: sqlite3.Connection,
        question: str,
        max_items: int,
        max_chars: int,
        excluded_sources: tuple[str, ...],
    ) -> Evidence | None:
        """`recall` with the local model reading the shortlist, or None without it.

        A judge that is loading or fails leaves `recall` with the cross-encoder's
        cut, which returned a fabricated answer on TRR dev where keywords and the
        coverage check did not; such a question takes the keyword path instead.
        """
        loaded = self._models.get() if self._models is not None else None
        if loaded is None:
            return None
        embedder, reranker, judge = loaded
        if not judge.ready():
            # Loaded for the next question; this one does not wait for it.
            judge.warm()
            return None
        evidence = recall(
            conn,
            self._config,
            question,
            embedder=embedder,
            reranker=reranker,
            judge=judge,
            limit=min(5, max_items),
            max_chars=min(1000, max_chars),
            include_sensitive=False,
            excluded_sources=excluded_sources,
        )
        return evidence if evidence.judged else None

    def try_resolve(self, request: ResolutionRequest) -> Resolution | None:
        if self._connect is None:
            return self._resolve(self._conn, request)
        with closing(self._connect()) as conn:
            return self._resolve(conn, request)

    def _resolve(self, conn: sqlite3.Connection, request: ResolutionRequest) -> Resolution | None:
        from varyz.trust import capability_limits

        max_items, max_chars = capability_limits(conn, request.caller)
        excluded_sources = tuple(self._config.graph.excluded_sources)
        if max_items <= 0 or max_chars <= 0:
            return self._withheld("caller_capability_denied")
        # The project's own name is not the subject: an archive that mentions
        # lumen does not therefore know which accent colour lumen should use.
        subject = without_project_name(request.question, request.project)
        if not lexically_grounded(
            conn,
            subject,
            excluded_sources=excluded_sources,
        ):
            return None
        # Resolution answers one question. `brief` deliberately fans a task out
        # and pads it with adjacent context; using it here let unrelated,
        # non-sensitive records replace a withheld sensitive hit and turn an
        # abstention into a fabricated answer. `recall` keeps abstention enabled
        # and reports sensitive matches without releasing their text.
        evidence = self._judged_evidence(
            conn, request.question, max_items, max_chars, excluded_sources
        )
        if evidence is None:
            evidence = recall(
                conn,
                self._config,
                request.question,
                embedder=None,
                limit=min(5, max_items),
                max_chars=min(1000, max_chars),
                include_sensitive=False,
                excluded_sources=excluded_sources,
            )
        # A sensitive candidate is terminal even when lower-ranked personal
        # capsules also survived filtering. Returning those lower-ranked rows
        # as the answer both hides that the real match was withheld and turns
        # retrieval padding into fabricated owner context.
        if evidence.withheld_sensitive:
            return self._withheld("sensitive_evidence_withheld")
        # Retrieval ranks and never refuses, and the index-wide gate above only
        # says the archive has used these words somewhere -- on an archive of a
        # few thousand entries it nearly always has. So each record is read
        # against the question on its own, and when not one of them says the
        # question in one place the question goes to the owner rather than out
        # as a tier-2 answer at the confidence of a rank. One record that does
        # carries the rest: an answer often needs a second record that shares
        # only the subject with the question (the date a decision was reversed,
        # the third message of a dispute), and dropping those lost correct
        # answers. Measured and explained at `evidence_coverage`.
        #
        # Records the judge chose are not held to it. The record that answers
        # often shares few words with the question ("Why did we choose
        # Postgres over DynamoDB?" against "Decision: PostgreSQL 16 ... Rejected:
        # key-value store"), and on recall_bench dev the check turned away 5
        # questions the judged records answered, after the judge had already
        # refused the records that are only on the same topic.
        #
        # The judge reads the passage channel only. Conversations fill the slots
        # it leaves, so when it picked nothing an unrelated agent chat that
        # shared a word with the question went out as the answer under its
        # flag. A conversation is held to coverage like any unjudged record.
        if not any(
            (evidence.judged and capsule.kind != "conversation")
            or evidence_coverage(
                conn,
                subject,
                entry_id=capsule.entry_id,
                window_id=capsule.external_id if capsule.kind == "conversation" else None,
                excluded_sources=excluded_sources,
            )
            >= MIN_EVIDENCE_COVERAGE
            for capsule in evidence.capsules
        ):
            return None
        # A word no record uses drops out of that count, and it is often the
        # subject: "the kayak" in an archive without one leaves "much" and "pay".
        # The local model reads the top records against the question; not one
        # about the thing asked sends it to the owner. When the model cannot
        # answer, the coverage check stands alone. See `evidence_verify`.
        if self._verifier is not None and (
            self._verifier.on_topic(subject, [capsule.text for capsule in evidence.capsules])
            is False
        ):
            return None

        answers: list[TypedAnswer] = []
        remaining = max_chars
        for capsule in evidence.capsules:
            citation = capsule.external_id or capsule.entry_id
            rendered = f"{capsule.text.strip()} [{capsule.source}: {citation}]"
            rendered = rendered[:remaining].rstrip()
            if not rendered:
                break
            basis = (
                AnswerBasis.REPORTED
                if capsule.provenance == "authored"
                else AnswerBasis.OBSERVED
            )
            occurred = _parse_datetime(capsule.occurred)
            answers.append(
                TypedAnswer(
                    relation=LifeRelation.INTERPRETATION,
                    record_kind="archive_evidence",
                    subject_entity_ids=(),
                    predicate_or_concept="owner_resolution_evidence",
                    value_kind="text",
                    display_value=rendered,
                    basis=basis,
                    status="active",
                    confidence=max(0.0, min(1.0, float(capsule.score))),
                    readiness="evidence",
                    observed_at=occurred,
                    stated_at=occurred,
                    sensitivity=capsule.sensitivity,
                    provenance=QueryProvenance(
                        generation_id="archive",
                        record_id=citation,
                        entry_ids=(capsule.entry_id,),
                    ),
                )
            )
            remaining -= len(rendered)
            if remaining <= 0:
                break
        if not answers:
            return None
        life_result = LifeQueryResult(
            disposition=QueryDisposition.ANSWERED,
            generation_id=None,
            answers=tuple(answers),
        )
        return LifeResultTier(2, lambda _: life_result).try_resolve(request)

    def _withheld(self, reason: str) -> Resolution:
        marker = WithheldRecordMarker(
            record_kind="archive_evidence",
            reason=reason,
            rule_version="alpha-local-v1",
        )
        result = LifeQueryResult(
            disposition=QueryDisposition.NOT_KNOWN,
            generation_id=None,
            not_known=True,
            sensitive_records_withheld=True,
            withheld_records=(marker,),
        )
        resolved = LifeResultTier(2, lambda _: result).try_resolve(
            ResolutionRequest(
                question="withheld",
                caller=Caller(
                    agent_id="policy",
                    display_name="Policy",
                    purpose="policy",
                    device_id="local",
                ),
                purpose="policy",
                allow_escalation=False,
            )
        )
        assert resolved is not None
        return resolved


_OWNER_ANSWERABLE_WITHHOLDINGS = frozenset({"sensitive_evidence_withheld"})


def _owner_may_answer(request: ResolutionRequest, result: Resolution) -> bool:
    """A sensitivity withholding can go to the owner; a denied caller cannot."""
    return (
        request.allow_escalation
        and result.egress_request_id is None
        and bool(result.withheld)
        and all(item.reason in _OWNER_ANSWERABLE_WITHHOLDINGS for item in result.withheld)
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ResolutionResolver:
    """Resolve in injected tier order, safely abstaining when every tier misses."""

    def __init__(
        self,
        tiers: Sequence[ResolverTier],
        *,
        escalation_queue: EscalationQueue | None = None,
        ledger: SQLiteResolutionLedger | None = None,
        min_confidence: float = 0.0,
        urgency: str = "normal",
        clock: Any = utc_now,
        lock: AbstractContextManager[Any] | None = None,
    ) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between zero and one")
        self._tiers = tuple(tiers)
        self._escalations = escalation_queue
        self._ledger = ledger
        self._min_confidence = min_confidence
        self._urgency = urgency
        self._clock = clock
        self._fallback_seq = count(1)
        self._fallback_lock = threading.Lock()
        # Held for every step that uses the tiers' shared connection. A tier with
        # a connection of its own (`own_connection`) runs without it, so other
        # requests to the daemon go on meanwhile.
        self.lock: AbstractContextManager[Any] = lock if lock is not None else threading.RLock()
        self.last_errors: tuple[str, ...] = ()

    def resolve(
        self, request: ResolutionRequest, *, owner_answers_only: bool = False
    ) -> Resolution:
        """``owner_answers_only``: answer only from what the owner recorded (tier
        1), else ask them. An archive passage or a model's reading of the owner
        never grants a permission or settles a blocker for them."""
        tried: list[int] = []
        errors: list[str] = []
        withheld: Resolution | None = None
        for position, tier in enumerate(self._tiers, start=1):
            tier_number = int(getattr(tier, "tier", position))
            if owner_answers_only and tier_number != 1:
                continue
            tried.append(tier_number)
            try:
                guard = nullcontext() if getattr(tier, "own_connection", False) else self.lock
                with guard:
                    result = tier.try_resolve(request)
            except Exception as exc:
                # Empty/missing derived tables, an unavailable local model, or a
                # malformed optional adapter make that tier miss; they do not make
                # the owner-resolution primitive fail or fabricate.
                errors.append(f"tier {tier_number}: {type(exc).__name__}: {exc}")
                continue
            if result is None:
                continue
            if result.disposition == QueryDisposition.NOT_KNOWN:
                if result.withheld or result.egress_request_id:
                    # Withholding is terminal for answers: later tiers must not
                    # stand in for the withheld match. Only the owner may, and only
                    # when the caller is entitled to ask and a policy (not an
                    # egress approval or a denied capability) is what blocked it.
                    if not _owner_may_answer(request, result) or self._escalations is None:
                        self.last_errors = tuple(errors)
                        return self._finish(request, result)
                    withheld = result
                    break
                continue
            if result.answer is not None and result.confidence < self._min_confidence:
                continue
            self.last_errors = tuple(errors)
            return self._finish(request, result)

        self.last_errors = tuple(errors)
        if request.allow_escalation and self._escalations is not None:
            with self.lock:
                escalation_id = self._escalations.enqueue(
                    request, tried_tiers=tuple(tried), urgency=self._urgency
                )
                pending = self._escalations.poll(escalation_id)
            if pending is not None:
                if withheld is not None and pending.answer is None:
                    pending = Resolution.model_validate(
                        {**dict(pending), "withheld": withheld.withheld}
                    )
                return self._finish(request, pending)
        if withheld is not None:
            return self._finish(request, withheld)
        unknown = Resolution(
            disposition=QueryDisposition.NOT_KNOWN,
            basis=None,
            answer=None,
            tier=5,
            confidence=1.0,
            resolved_at=self._clock(),
            monotonic_seq=self._local_seq(),
        )
        return self._finish(request, unknown)

    def _finish(self, request: ResolutionRequest, result: Resolution) -> Resolution:
        if self._ledger is not None:
            with self.lock:
                return self._ledger.append(request, result)
        if result.monotonic_seq == 0:
            return result.model_copy(update={"monotonic_seq": self._local_seq()})
        return result

    def _local_seq(self) -> int:
        with self._fallback_lock:
            return next(self._fallback_seq)


Resolver = ResolutionResolver


def resolve_for_owner(
    question: str,
    caller: Caller,
    purpose: str,
    *,
    resolver: ResolutionResolver,
    conversation_id: str | None = None,
    max_wait_seconds: int = 0,
    allow_escalation: bool = True,
) -> Resolution:
    """Transport-independent product primitive."""
    request = ResolutionRequest(
        question=question,
        caller=caller,
        purpose=purpose,
        conversation_id=conversation_id,
        max_wait_seconds=max_wait_seconds,
        allow_escalation=allow_escalation,
    )
    return resolver.resolve(request)


__all__ = [
    "EvidenceTier",
    "LifeResultTier",
    "ResolutionResolver",
    "Resolver",
    "SQLiteResolutionLedger",
    "resolve_for_owner",
]
