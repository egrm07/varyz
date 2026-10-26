"""Small authenticated loopback HTTP daemon for owner resolution."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import sqlite_vec

from varyz.cloud.computer import ComputerConnection, handle_computer_request
from varyz.config import Config
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis, LifeModelQueryService
from varyz.resolution.escalation import SQLiteEscalationQueue
from varyz.resolution.knowledge import OWNER_ID, SQLiteKnowledgeStore
from varyz.resolution.live import LiveIngest
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.ports import Notifier, ResolverTier
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.resolver import (
    EvidenceTier,
    LifeResultTier,
    ResolutionResolver,
    SQLiteResolutionLedger,
)
from varyz.resolution.trials import TrialRules, TrialSettings, off_path, system_caller
from varyz.resolution.workflow_nudges import SQLiteNudgeStore
from varyz.resolution.workflows import SQLiteWorkflowStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1207
MAX_REQUEST_BYTES = 1_000_000


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    host: str
    port: int
    auth_mode: str
    pending_escalations: int


class ResolutionService:
    """Application service owned by the daemon; surfaces use the client instead."""

    def __init__(
        self,
        resolver: ResolutionResolver,
        escalations: SQLiteEscalationQueue,
        knowledge: SQLiteKnowledgeStore,
        principles: SQLitePrincipleStore | None = None,
        workflows: SQLiteWorkflowStore | None = None,
        trials: TrialRules | None = None,
        shadow_config: Config | None = None,
    ) -> None:
        self.resolver = resolver
        self.escalations = escalations
        self.knowledge = knowledge
        # Every request that touches the daemon's one connection takes this; the
        # resolver's own lock, so an answer's reads and writes take turns with them.
        lock = getattr(resolver, "lock", None)
        self.lock: Any = lock if lock is not None else threading.RLock()
        self.principles = principles or SQLitePrincipleStore(knowledge._conn)
        self.workflows = workflows or SQLiteWorkflowStore(knowledge._conn)
        self.nudges = SQLiteNudgeStore(knowledge._conn)
        # The trial lane: strong candidates go into effect before the owner reads
        # them (``trials``). ``None`` keeps every candidate in the review queue.
        self.trials = trials
        self.shadow_config = shadow_config

    def resolve(
        self, request: ResolutionRequest, *, owner_answers_only: bool = False
    ) -> Resolution:
        started = time.perf_counter()
        try:
            result = self.resolver.resolve(request, owner_answers_only=owner_answers_only)
        except Exception:
            if self.shadow_config is not None:
                from varyz.analytics import record

                record(self.shadow_config, "resolution_failed")
            raise
        if self.shadow_config is not None:
            from varyz.analytics import latency

            latency(self.shadow_config, time.perf_counter() - started)
        if self.shadow_config is not None:
            # Never let an experiment suppress or alter the current answer.
            with contextlib.suppress(Exception), self.lock:
                from varyz.resolution.shadow import observe_answer

                observe_answer(self.knowledge._conn, self.shadow_config, request, result,
                               owner_answers_only=owner_answers_only)
        return result

    def poll(self, escalation_id: str) -> Resolution | None:
        return self.escalations.poll(escalation_id)

    def answer(
        self,
        escalation_id: str,
        answer: str,
        *,
        device_id: str,
        source: str = "escalation",
        scope: str | None = None,
    ) -> Resolution:
        return self.escalations.answer(
            escalation_id, answer, device_id=device_id, source=source, scope=scope
        )

    def dismiss(self, escalation_id: str) -> bool:
        return self.escalations.dismiss(escalation_id)

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
        scope: str | None = "*",
        author_id: str = OWNER_ID,
        origin: str = "own",
    ) -> str:
        return self.knowledge.remember(
            question=question,
            answer=answer,
            caller=caller,
            basis=basis,
            source=source,
            device_id=device_id,
            supersedes_id=supersedes_id,
            escalation_id=escalation_id,
            scope=scope,
            author_id=author_id,
            origin=origin,
        )

    def retract(
        self, knowledge_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        return self.knowledge.retract(
            knowledge_id, caller=caller, device_id=device_id, reason=reason
        )

    def current_knowledge(self) -> tuple[dict[str, Any], ...]:
        return tuple(entry.to_dict() for entry in self.knowledge.current())

    def record_principle(self, payload: dict[str, Any]) -> str | None:
        """Add a confirmed rule, or queue a candidate when ``status`` says so."""
        return self.record_principle_lane(payload)["principle_id"]

    def record_principle_lane(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``record_principle``, and the trial rule a strong candidate became."""
        arguments: dict[str, Any] = {
            "statement": str(payload["statement"]),
            "caller": Caller.model_validate(payload["caller"]),
            "device_id": str(payload["device_id"]),
            "source": str(payload.get("source") or "manual"),
            "kind": str(payload.get("kind") or "rule"),
            "scope": payload.get("scope"),
            "rationale": str(payload.get("rationale") or ""),
            "provenance": dict(payload.get("provenance") or {}),
            "domain": payload.get("domain") or "",
        }
        status = payload.get("status", "confirmed")
        if status == "candidate":
            candidate_id = self.principles.propose(**arguments)
            trial_id = None
            if candidate_id is not None and self.trials is not None:
                trial_id = self.trials.consider(
                    candidate_id, caller=arguments["caller"],
                    device_id=arguments["device_id"],
                )
            return {"principle_id": candidate_id, "trial_id": trial_id}
        if status != "confirmed":
            raise ValueError("status must be confirmed or candidate")
        return {"principle_id": self.principles.add(
            **arguments, supersedes_id=payload.get("supersedes_id")
        ), "trial_id": None}

    def _trials(self) -> TrialRules:
        if self.trials is None:
            raise ValueError("this daemon has no trial lane")
        return self.trials

    def record_principle_uses(self, payload: dict[str, Any]) -> list[str]:
        """Rules shown in a task's briefing; the trial rules that held for good."""
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            raise ValueError("ids is a list of rule ids")
        if self.trials is None:
            return []
        return self.trials.record_uses(
            [str(value) for value in ids], str(payload["task_key"]),
            device_id=str(payload["device_id"]),
        )

    def record_rule_question(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A waiting rule a task asked the owner about, or their answer to it."""
        from datetime import UTC, datetime

        from varyz.resolution import rule_questions

        with self.lock:
            recorded = rule_questions.record(
                self.knowledge._conn, str(payload.get("principle_id") or ""),
                str(payload.get("conversation_id") or ""), str(payload.get("event") or ""),
                project=payload.get("project") or None,
                stamp=datetime.now(UTC).isoformat(),
            )
        return {"recorded": recorded}

    def record_task_signals(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What finish_check found for each rule, and the answers a finished task adopted."""
        from varyz.resolution import rule_signals

        conversation = str(payload.get("conversation_id") or "") or None
        receipts = payload.get("receipts") or []
        if not isinstance(receipts, list):
            raise ValueError("receipts is the list finish_check returned")
        # The rate the caller's briefings held rules back at, so each draw is recorded
        # as it was made; else this daemon's own setting.
        rate = payload.get("memory_test")
        memory_test = (float(rate) if isinstance(rate, int | float) and 0 <= rate <= 0.1
                       else rule_signals.memory_test_rate(self.shadow_config))
        with self.lock:
            checks = rule_signals.record_checks(
                self.knowledge._conn, conversation,
                [item for item in receipts if isinstance(item, dict)],
                project=payload.get("project") or None,
                agent=str(payload.get("agent") or ""), model=str(payload.get("model") or ""),
                memory_test=memory_test,
            )
            adopted = (
                rule_signals.adopt_finished(
                    self.knowledge._conn, conversation, device_id=str(payload["device_id"])
                )
                if payload.get("ready") else []
            )
        return {"checks": checks, "adopted": adopted}

    def record_rule_correction(self, principle_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A rule the owner just taught: the one it corrected, and the output it was about."""
        from varyz.resolution import rule_signals

        conversation = str(payload.get("conversation_id") or "") or None
        corrected = str(payload.get("corrected") or "") or None
        agent_turn = str(payload.get("agent_turn") or "")
        with self.lock:
            check = (
                rule_signals.record_correction(
                    self.knowledge._conn, corrected, conversation,
                    project=payload.get("project") or None,
                )
                if corrected else None
            )
            kept = bool(agent_turn.strip()) and rule_signals.record_turn(
                self.knowledge._conn, principle_id, method="agent_supplied",
                agent_turn=agent_turn, thread=conversation or "",
            )
            ask = (
                rule_signals.record_ask_feedback(
                    self.knowledge._conn, "should_have_asked", principle_id=principle_id,
                    conversation_id=conversation,
                )
                if payload.get("should_have_asked") else None
            )
        return {"check": check, "turn_kept": kept, "ask_feedback": ask}

    def record_ask_signals(self, escalation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Before the owner answers: the agent's guess. After: whether asking was needed."""
        from varyz.resolution import rule_signals

        with self.lock:
            predicted = bool(payload.get("predict")) and rule_signals.record_prediction(
                self.knowledge._conn, escalation_id,
                decision_kind=str(payload.get("decision_kind") or ""),
                agent=str(payload.get("agent") or ""), model=str(payload.get("model") or ""),
                agent_guess=str(payload.get("agent_guess") or ""),
            )
            label = str(payload.get("ask_feedback") or "")
            feedback = rule_signals.record_ask_feedback(
                self.knowledge._conn, label, escalation_id=escalation_id,
                conversation_id=str(payload.get("conversation_id") or "") or None,
            ) if label else None
        return {"predicted": predicted, "ask_feedback": feedback}

    def trial_status(self) -> dict[str, Any]:
        trials = self._trials()
        status = trials.status()
        status["spot_check_ids"] = [rule.id for rule in trials.spot_check(
            str(len(status["rules"])))]
        return status

    def scan_trials(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload["device_id"])
        caller = (Caller.model_validate(payload["caller"]) if payload.get("caller")
                  else system_caller(device_id))
        return self._trials().scan(
            apply=payload.get("apply") is True, caller=caller, device_id=device_id
        )

    def undo_trials(self, payload: dict[str, Any]) -> int:
        from varyz.resolution.knowledge import _datetime

        return self._trials().undo(
            _datetime(str(payload["since"])),
            caller=Caller.model_validate(payload["caller"]),
            device_id=str(payload["device_id"]),
        )

    def confirm_principle(self, candidate_id: str, payload: dict[str, Any]) -> str:
        return self.principles.confirm(
            candidate_id,
            caller=Caller.model_validate(payload["caller"]),
            device_id=str(payload["device_id"]),
            statement=payload.get("statement"),
            scope=payload.get("scope"),
            kind=payload.get("kind"),
            rationale=payload.get("rationale"),
            domain=payload.get("domain"),
            applies_to=payload.get("applies_to"),
        )

    def retract_principle(
        self, principle_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        return self.principles.retract(
            principle_id, caller=caller, device_id=device_id, reason=reason
        )

    def current_principles(self, status: str = "confirmed") -> tuple[dict[str, Any], ...]:
        if status == "withdrawn":
            return self.principles.withdrawn()
        return tuple(item.to_dict() for item in self.principles.current(status))

    def record_workflow(self, payload: dict[str, Any]) -> str | None:
        """The owner's stated order, or a learned one (``None`` when nothing changed)."""
        steps = payload["steps"]
        if not isinstance(steps, list):
            raise ValueError("steps is a list, in the order the owner takes them")
        arguments: dict[str, Any] = {
            "name": str(payload["name"]),
            "steps": steps,
            "caller": Caller.model_validate(payload["caller"]),
            "device_id": str(payload["device_id"]),
            "source": str(payload.get("source") or "manual"),
            "scope": payload.get("scope"),
            "domain": payload.get("domain") or "",
            "provenance": dict(payload.get("provenance") or {}),
        }
        basis = payload.get("basis", "stated")
        if basis == "learned":
            return self.workflows.learn(**arguments)
        if basis != "stated":
            raise ValueError("basis must be stated or learned")
        return self.workflows.record(**arguments, rationale=str(payload.get("rationale") or ""))

    def retract_workflow(
        self, workflow_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        return self.workflows.retract(
            workflow_id, caller=caller, device_id=device_id, reason=reason
        )

    def current_workflows(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.to_dict() for item in self.workflows.current())

    def record_workflow_nudge(self, workflow_id: str, payload: dict[str, Any]) -> list[str]:
        """The owner's answer to a raised step: 'took', 'skipped', or 'rework'."""
        caller = Caller.model_validate(payload["caller"])
        device_id = str(payload["device_id"])
        answer = str(payload.get("answer") or "")
        if answer == "rework":
            skip_id = str(payload.get("skip_id") or "")
            if not skip_id:
                raise ValueError("answer 'rework' names the skip_id it follows")
            return [self.nudges.record_rework(skip_id, caller=caller, device_id=device_id)]
        steps = payload.get("steps")
        if not isinstance(steps, list):
            raise ValueError("steps is the list of earlier steps the agent raised")
        return self.nudges.record_answer(
            workflow_id, steps=[str(step) for step in steps], answer=answer,
            caller=caller, device_id=device_id,
            task_step=str(payload.get("task_step") or ""),
            project=payload.get("project") or None,
        )

    def review_workflow(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The owner's keep / optional / drop for a step they keep skipping."""
        return self.nudges.review(
            workflow_id, step=str(payload["step"]), decision=str(payload["decision"]),
            caller=Caller.model_validate(payload["caller"]),
            device_id=str(payload["device_id"]),
            source=str(payload.get("source") or "review"),
        )


class ResolutionDaemon:
    """Configurable local daemon.  Widening the bind requires strong auth."""

    def __init__(
        self,
        service: ResolutionService,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        auth_mode: str = "session",
        session_token: str | None = None,
    ) -> None:
        host = host.strip()
        if not host:
            raise ValueError("daemon host cannot be blank")
        if not 0 <= port <= 65535:
            raise ValueError("daemon port must be between 0 and 65535")
        if not _is_loopback(host) and auth_mode not in {"token", "mutual_tls"}:
            raise ValueError("a non-loopback bind requires token or mutual_tls auth")
        self.service = service
        self.host = host
        self.port = port
        self.auth_mode = auth_mode
        self.session_token = session_token
        if auth_mode == "token" and session_token is None:
            self.session_token = secrets.token_urlsafe(32)
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._owned_connection: sqlite3.Connection | None = None
        # The database this daemon serves. Two archives on one machine share a
        # default port, and a client of one used to be answered -- and written
        # into -- by the other's daemon without either noticing. `/v1/identity`
        # lets a client check it reached its own archive before anything else.
        self.archive: str | None = None
        # Reads the AI chats' new messages a moment after a prompt (`/v1/live/nudge`).
        self.live: LiveIngest | None = None
        self.computer_connection: ComputerConnection | None = None
        self._questions_swept = float("-inf")

    @property
    def address(self) -> tuple[str, int]:
        if self._server is not None:
            address = self._server.server_address
            return str(address[0]), int(address[1])
        return self.host, self.port

    def status(self) -> DaemonStatus:
        host, port = self.address
        return DaemonStatus(
            running=self._server is not None,
            host=host,
            port=port,
            auth_mode=self.auth_mode,
            pending_escalations=len(self.service.escalations.pending()),
        )

    def serve_forever(self) -> None:
        server = self._make_server()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            self._server = None
            self._close_owned_connection()

    def start(self) -> DaemonStatus:
        if self._server is not None:
            return self.status()
        self._make_server()
        self._thread = threading.Thread(
            target=self._serve_existing,
            name="varyz-resolution-daemon",
            daemon=True,
        )
        self._thread.start()
        return self.status()

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        server.server_close()
        self._server = None
        self._thread = None

    def _serve_existing(self) -> None:
        server = self._server
        if server is None:  # pragma: no cover - guarded by start
            return
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            self._close_owned_connection()

    def _make_server(self) -> HTTPServer:
        if self._server is not None:
            return self._server
        daemon = self

        class Handler(_ResolutionHandler):
            resolution_daemon = daemon

        # A thread per request. The one SQLite connection is still used by one
        # request at a time (`ResolutionService.lock`); what runs beside them is
        # tier 2, which reads through connections of its own. Four agents asking
        # at once waited 8 s for the last answer, and a status check sent among
        # them as long, when the daemon served one request at a time.
        class Server(ThreadingHTTPServer):
            def service_actions(self) -> None:
                daemon.expire_stale_questions()
                if daemon.service.shadow_config is not None:
                    from varyz.analytics import tick

                    tick(daemon.service.shadow_config)
                    from varyz.resolution import rule_signals

                    rule_signals.tick(daemon.service.knowledge._conn,
                                      daemon.service.shadow_config, daemon.service.lock)

        server = Server((self.host, self.port), Handler)
        server.daemon_threads = True
        self._server = server
        return self._server

    def expire_stale_questions(self) -> None:
        """Hourly: owner questions left a week unanswered close as expired."""
        if time.monotonic() - self._questions_swept < 3600:
            return
        self._questions_swept = time.monotonic()
        with contextlib.suppress(Exception), self.service.lock:
            self.service.escalations.expire_stale()

    def _close_owned_connection(self) -> None:
        connection = self._owned_connection
        if connection is None:
            return
        self._owned_connection = None
        with self.service.lock:
            connection.close()

    def __enter__(self) -> ResolutionDaemon:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class _ResolutionHandler(BaseHTTPRequestHandler):
    resolution_daemon: ResolutionDaemon
    server_version = "VaryzResolution/1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._computer_request():
            return
        if not self._authorized():
            return
        with self.resolution_daemon.service.lock:
            self._get()

    def _get(self) -> None:
        path = urlsplit(self.path).path
        if path == "/v1/status":
            self._json(HTTPStatus.OK, asdict(self.resolution_daemon.status()))
            return
        if path == "/v1/identity":
            self._json(HTTPStatus.OK, {"archive": self.resolution_daemon.archive})
            return
        if path == "/v1/knowledge":
            items = self.resolution_daemon.service.current_knowledge()
            self._json(HTTPStatus.OK, {"items": items})
            return
        if path == "/v1/principles":
            status = parse_qs(urlsplit(self.path).query).get("status", ["confirmed"])[0]
            if status not in {"confirmed", "candidate", "withdrawn"}:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_status"})
                return
            items = self.resolution_daemon.service.current_principles(status)
            self._json(HTTPStatus.OK, {"items": items})
            return
        if path == "/v1/principles/trials":
            try:
                with self.resolution_daemon.service.lock:
                    trials = self.resolution_daemon.service.trial_status()
            except ValueError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no_trials", "detail": str(exc)})
                return
            self._json(HTTPStatus.OK, trials)
            return
        if path == "/v1/workflows":
            items = self.resolution_daemon.service.current_workflows()
            self._json(HTTPStatus.OK, {"items": items})
            return
        if path == "/v1/escalations":
            recipient = parse_qs(urlsplit(self.path).query).get("recipient", [None])[0]
            items = self.resolution_daemon.service.escalations.pending(recipient)
            self._json(HTTPStatus.OK, {"items": items})
            return
        prefix = "/v1/escalations/"
        if path.startswith(prefix):
            escalation_id = path[len(prefix) :]
            result = self.resolution_daemon.service.poll(escalation_id)
            if result is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "escalation_not_found"})
            else:
                self._json(HTTPStatus.OK, result.model_dump(mode="json"))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._computer_request():
            return
        if not self._authorized():
            return
        try:
            payload = self._payload()
            path = urlsplit(self.path).path
            if path == "/v1/resolve":
                # The resolver takes the lock itself, and not while tier 2 reads.
                owner_answers_only = payload.pop("owner_answers_only", False) is True
                request = ResolutionRequest.model_validate(payload)
                result = self.resolution_daemon.service.resolve(
                    request, owner_answers_only=owner_answers_only
                )
                self._json(HTTPStatus.OK, result.model_dump(mode="json"))
                return
            with self.resolution_daemon.service.lock:
                self._post(path, payload)
        except (KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__, "detail": str(exc)})

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/v1/knowledge":
            knowledge_id = self.resolution_daemon.service.remember(
                question=str(payload["question"]),
                answer=str(payload["answer"]),
                caller=Caller.model_validate(payload["caller"]),
                basis=AnswerBasis(payload.get("basis", AnswerBasis.EXPLICIT.value)),
                source=str(payload.get("source", "manual")),
                device_id=str(payload["device_id"]),
                supersedes_id=payload.get("supersedes_id"),
                escalation_id=payload.get("escalation_id"),
                scope=payload.get("scope") or "*",
                author_id=str(payload.get("author_id") or OWNER_ID),
                origin=str(payload.get("origin") or "own"),
            )
            self._json(HTTPStatus.CREATED, {"knowledge_id": knowledge_id})
            return
        if path == "/v1/principles":
            lane = self.resolution_daemon.service.record_principle_lane(payload)
            self._json(HTTPStatus.CREATED, lane)
            return
        if path == "/v1/principles/uses":
            promoted = self.resolution_daemon.service.record_principle_uses(payload)
            self._json(HTTPStatus.OK, {"promoted": promoted})
            return
        if path == "/v1/principles/questions":
            self._json(HTTPStatus.OK, self.resolution_daemon.service.record_rule_question(payload))
            return
        if path == "/v1/tasks/signals":
            self._json(HTTPStatus.OK, self.resolution_daemon.service.record_task_signals(payload))
            return
        if path.startswith("/v1/escalations/") and path.endswith("/signals"):
            self._json(HTTPStatus.OK, self.resolution_daemon.service.record_ask_signals(
                unquote(path[len("/v1/escalations/") : -len("/signals")]), payload
            ))
            return
        if path.startswith("/v1/principles/") and path.endswith("/correction"):
            self._json(HTTPStatus.OK, self.resolution_daemon.service.record_rule_correction(
                unquote(path[len("/v1/principles/") : -len("/correction")]), payload
            ))
            return
        if path == "/v1/principles/trials/scan":
            self._json(HTTPStatus.OK, self.resolution_daemon.service.scan_trials(payload))
            return
        if path == "/v1/principles/trials/undo":
            undone = self.resolution_daemon.service.undo_trials(payload)
            self._json(HTTPStatus.OK, {"undone": undone})
            return
        prefix = "/v1/principles/"
        if path.startswith(prefix) and path.endswith("/confirm"):
            principle_id = self.resolution_daemon.service.confirm_principle(
                unquote(path[len(prefix) : -len("/confirm")]), payload
            )
            self._json(HTTPStatus.CREATED, {"principle_id": principle_id})
            return
        if path.startswith(prefix) and path.endswith("/retract"):
            retraction_id = self.resolution_daemon.service.retract_principle(
                unquote(path[len(prefix) : -len("/retract")]),
                caller=Caller.model_validate(payload["caller"]),
                device_id=str(payload["device_id"]),
                reason=str(payload.get("reason", "")),
            )
            self._json(HTTPStatus.CREATED, {"retraction_id": retraction_id})
            return
        if path == "/v1/workflows":
            workflow_id = self.resolution_daemon.service.record_workflow(payload)
            self._json(HTTPStatus.CREATED, {"workflow_id": workflow_id})
            return
        prefix = "/v1/workflows/"
        if path.startswith(prefix) and path.endswith("/retract"):
            retraction_id = self.resolution_daemon.service.retract_workflow(
                unquote(path[len(prefix) : -len("/retract")]),
                caller=Caller.model_validate(payload["caller"]),
                device_id=str(payload["device_id"]),
                reason=str(payload.get("reason", "")),
            )
            self._json(HTTPStatus.CREATED, {"retraction_id": retraction_id})
            return
        if path.startswith(prefix) and path.endswith("/nudges"):
            nudge_ids = self.resolution_daemon.service.record_workflow_nudge(
                unquote(path[len(prefix) : -len("/nudges")]), payload
            )
            self._json(HTTPStatus.CREATED, {"nudge_ids": nudge_ids})
            return
        if path.startswith(prefix) and path.endswith("/reviews"):
            review = self.resolution_daemon.service.review_workflow(
                unquote(path[len(prefix) : -len("/reviews")]), payload
            )
            self._json(HTTPStatus.CREATED, review)
            return
        if path == "/v1/live/nudge":
            live = self.resolution_daemon.live
            self._json(HTTPStatus.OK, {"reading": live.nudge() if live is not None else False})
            return
        if path == "/v1/shutdown":
            self._json(HTTPStatus.OK, {"stopping": True})
            threading.Thread(
                target=self.resolution_daemon.stop,
                name="varyz-resolution-stop",
                daemon=True,
            ).start()
            return
        prefix = "/v1/knowledge/"
        suffix = "/retract"
        if path.startswith(prefix) and path.endswith(suffix):
            retraction_id = self.resolution_daemon.service.retract(
                unquote(path[len(prefix) : -len(suffix)]),
                caller=Caller.model_validate(payload["caller"]),
                device_id=str(payload["device_id"]),
                reason=str(payload.get("reason", "")),
            )
            self._json(HTTPStatus.CREATED, {"retraction_id": retraction_id})
            return
        prefix = "/v1/escalations/"
        if path.startswith(prefix) and path.endswith("/dismiss"):
            escalation_id = unquote(path[len(prefix) : -len("/dismiss")])
            dismissed = self.resolution_daemon.service.dismiss(escalation_id)
            self._json(HTTPStatus.OK, {"dismissed": dismissed})
            return
        suffix = "/answer"
        if path.startswith(prefix) and path.endswith(suffix):
            escalation_id = unquote(path[len(prefix) : -len(suffix)])
            result = self.resolution_daemon.service.answer(
                escalation_id,
                str(payload["answer"]),
                device_id=str(payload["device_id"]),
                source=str(payload.get("source") or "escalation"),
                scope=payload.get("scope"),
            )
            self._json(HTTPStatus.OK, result.model_dump(mode="json"))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _computer_request(self) -> bool:
        return handle_computer_request(self, self.resolution_daemon.computer_connection)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._computer_request():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _authorized(self) -> bool:
        token = self.resolution_daemon.session_token
        if token is None:
            return True
        if secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("invalid request body length")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise TypeError("request body must be a JSON object")
        return payload

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _read_only(db_path: Path) -> Callable[[], sqlite3.Connection]:
    """Opens the archive for tier 2 to read one question with."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA query_only=ON")
        return conn

    return connect


def build_resolution_daemon(
    db_path: Path,
    *,
    config: Config | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    auth_mode: str = "session",
    session_token: str | None = None,
    device_id: str = "local",
    tiers: tuple[ResolverTier, ...] = (),
    notifier: Notifier | None = None,
) -> ResolutionDaemon:
    """Assemble the daemon process without coupling it to global configuration."""
    migrated = open_db(db_path)
    migrated.close()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    verifier = None
    if config is not None:
        from varyz.resolution.paraphrase_verify import paraphrase_verifier_from_config

        verifier = paraphrase_verifier_from_config(config)
    knowledge = SQLiteKnowledgeStore(conn, verifier=verifier)
    scope_model = None
    if config is not None:
        from varyz.resolution.scope import scope_model_from_config

        scope_model = scope_model_from_config(config)
    escalations = SQLiteEscalationQueue(
        conn,
        knowledge,
        notifier=notifier,
        device_id=device_id,
        scope_model=scope_model,
    )
    ledger = SQLiteResolutionLedger(conn, device_id=device_id)
    lock = threading.RLock()
    default_tiers: tuple[ResolverTier, ...] = ()
    if config is not None:
        from varyz.personal_model import compiler_is_shipped
        from varyz.resolution.evidence_verify import evidence_verifier_from_config
        from varyz.retrieval.loaded_models import RetrievalModels

        default_tiers = (
            EvidenceTier(
                conn,
                config,
                verifier=evidence_verifier_from_config(config),
                models=RetrievalModels(config),
                connect=_read_only(Path(db_path)),
            ),
        )
        # A build that cannot compile the model does not answer from one an
        # older version left behind.
        if compiler_is_shipped():
            model = LifeModelQueryService(conn)
            default_tiers += (
                LifeResultTier(3, lambda request: model.ask_text(request.question)),
            )
    resolver = ResolutionResolver(
        (knowledge, *(tiers or default_tiers)),
        escalation_queue=escalations,
        ledger=ledger,
        lock=lock,
    )
    principles = SQLitePrincipleStore(conn)
    trials = TrialRules(
        conn, principles,
        settings=TrialSettings.from_config(config) if config is not None else TrialSettings(),
        off=off_path(db_path),
    )
    daemon = ResolutionDaemon(
        ResolutionService(resolver, escalations, knowledge, principles, trials=trials,
                          shadow_config=config),
        host,
        port,
        auth_mode=auth_mode,
        session_token=session_token,
    )
    daemon._owned_connection = conn
    daemon.archive = str(Path(db_path).resolve())
    if config is not None and config.workflows.live_reading:
        daemon.live = LiveIngest(config)
    return daemon


# The daemon answers every agent's ask from the code it imported at launch. It
# leaves once the installed source has changed and then held still for this many
# polls (an edit is often several files), and the next call starts a fresh one.
CODE_POLL_SECONDS = 5.0
CODE_SETTLE_POLLS = 6


def watch_code(
    stop: Callable[[], None],
    fingerprint: Callable[[], str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    running: Callable[[], bool] = lambda: True,
    can_stop: Callable[[], bool] = lambda: True,
) -> bool:
    """Call ``stop`` once the source moved on and settled; False if never."""
    launched = fingerprint()
    moved_to: str | None = None
    settled = 0
    while running():
        sleep(CODE_POLL_SECONDS)
        current = fingerprint()
        if current == launched:
            moved_to, settled = None, 0
            continue
        settled = settled + 1 if current == moved_to else 1
        moved_to = current
        if settled >= CODE_SETTLE_POLLS and can_stop():
            stop()
            return True
    return False


def run_resolution_daemon(
    db_path: Path,
    *,
    config: Config | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    auth_mode: str = "session",
    session_token: str | None = None,
    device_id: str = "local",
    tiers: tuple[ResolverTier, ...] = (),
    notifier: Notifier | None = None,
    watch_code_changes: bool = False,
    config_path: Path | None = None,
) -> None:
    """Build and run the daemon in the current process until stopped.

    With ``config_path``, Cloud sync runs in realtime beside it once the owner
    enabled it (`varyz.cloud.live_sync`).
    """
    daemon = build_resolution_daemon(
        db_path,
        config=config,
        host=host,
        port=port,
        auth_mode=auth_mode,
        session_token=session_token,
        device_id=device_id,
        tiers=tiers,
        notifier=notifier,
    )
    if watch_code_changes:
        from varyz.autosync import code_fingerprint

        threading.Thread(
            target=watch_code,
            args=(daemon.stop, code_fingerprint),
            kwargs={"can_stop": lambda: daemon.computer_connection is None
                    or not daemon.computer_connection.active()},
            name="varyz-resolution-code-watch",
            daemon=True,
        ).start()
    cloud = None
    if config_path is not None:
        from varyz.cloud.live_sync import LiveCloudSync
        from varyz.resolution.headless_reader import ResumeWatcher

        daemon.computer_connection = ComputerConnection(config_path)
        cloud = LiveCloudSync(config_path, db_path)
        cloud.start()
        # A reading that stopped for the plan's limits starts again once they allow.
        ResumeWatcher(config_path).start()
    try:
        daemon.serve_forever()
    finally:
        if cloud is not None:
            cloud.stop()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DaemonStatus",
    "ResolutionDaemon",
    "ResolutionService",
    "build_resolution_daemon",
    "run_resolution_daemon",
]
