"""The only supported client surface for the resolution daemon."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from varyz.personal_model.query import AnswerBasis
from varyz.resolution.daemon import DEFAULT_HOST, DEFAULT_PORT, DaemonStatus
from varyz.resolution.knowledge import OWNER_ID
from varyz.resolution.models import Caller, Resolution, ResolutionRequest


class ResolutionClientError(RuntimeError):
    pass


class ArchiveMismatchError(ResolutionClientError):
    """The daemon on this port serves another archive than the caller's."""


class DaemonUnavailableError(ResolutionClientError):
    """A refused connection: no request was delivered, so recovery is safe."""


def _same_file(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


class ResolutionClient:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        auth_mode: str = "session",
        session_token: str | None = None,
        timeout: float = 30.0,
        archive: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        """``archive``: the database this caller means. When given, the first
        request checks that the daemon on the port serves that database, and
        raises ArchiveMismatchError instead of reading or writing another one."""
        self.host = host
        self.port = port
        self.auth_mode = auth_mode
        self.session_token = session_token
        self.timeout = timeout
        self.archive = archive
        self.config_path = config_path
        self._archive_checked = archive is None
        self._base_url = f"http://{host}:{port}/v1"
        # The trial rule the last queued candidate went into effect as, if any.
        self.last_trial_id: str | None = None

    def resolve(
        self, request: ResolutionRequest, *, owner_answers_only: bool = False
    ) -> Resolution:
        """``owner_answers_only``: skip the archive and the personal model; the
        owner's recorded answer, or a question to them."""
        payload = request.model_dump(mode="json")
        if owner_answers_only:
            payload["owner_answers_only"] = True
        return self._decode_resolution(self._request("POST", "/resolve", payload))

    def poll(self, escalation_id: str) -> Resolution | None:
        try:
            return self._decode_resolution(
                self._request("GET", f"/escalations/{escalation_id}")
            )
        except ResolutionClientError as exc:
            if "404" in str(exc):
                return None
            raise

    def answer(
        self,
        escalation_id: str,
        answer: str,
        *,
        device_id: str,
        source: str = "escalation",
        scope: str | None = None,
    ) -> Resolution:
        """``scope``: '*' general preference, a project name, or ``None`` for the default."""
        return self._decode_resolution(
            self._request(
                "POST",
                f"/escalations/{quote(escalation_id, safe='')}/answer",
                {"answer": answer, "device_id": device_id, "source": source, "scope": scope},
            )
        )

    def dismiss(self, escalation_id: str) -> bool:
        """Close a pending owner question without answering it."""
        response = self._request(
            "POST", f"/escalations/{quote(escalation_id, safe='')}/dismiss", {}
        )
        return bool(response.get("dismissed"))

    @staticmethod
    def _decode_resolution(payload: dict[str, Any]) -> Resolution:
        """Validate a daemon response with JSON rather than Python semantics.

        The contracts are strict and frozen.  Their tuple and enum fields are
        intentionally represented as arrays and strings on the wire, which
        ``model_validate`` rejects in strict Python mode but
        ``model_validate_json`` correctly accepts as JSON representations.
        """
        return Resolution.model_validate_json(json.dumps(payload, ensure_ascii=False))

    def remember(
        self,
        *,
        question: str,
        answer: str,
        caller: Caller,
        device_id: str,
        basis: AnswerBasis = AnswerBasis.EXPLICIT,
        source: str = "manual",
        supersedes_id: str | None = None,
        escalation_id: str | None = None,
        scope: str | None = "*",
        author_id: str = OWNER_ID,
        origin: str = "own",
    ) -> str:
        """``author_id`` and ``origin``: who answered, and whether it is the owner's
        ``own`` answer, an ``org`` answer ('org:<id>' scope) or ``published`` taste."""
        response = self._request(
            "POST",
            "/knowledge",
            {
                "question": question,
                "answer": answer,
                "caller": caller.model_dump(mode="json"),
                "basis": basis.value,
                "source": source,
                "device_id": device_id,
                "supersedes_id": supersedes_id,
                "escalation_id": escalation_id,
                "scope": scope,
                "author_id": author_id,
                "origin": origin,
            },
        )
        return str(response["knowledge_id"])

    def knowledge(self) -> tuple[dict[str, Any], ...]:
        """Current owner beliefs: neither superseded nor retracted."""
        response = self._request("GET", "/knowledge")
        return tuple(response["items"])

    def retract(
        self, knowledge_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        response = self._request(
            "POST",
            f"/knowledge/{quote(knowledge_id, safe='')}/retract",
            {"caller": caller.model_dump(mode="json"), "device_id": device_id, "reason": reason},
        )
        return str(response["retraction_id"])

    def principles(self, status: str = "confirmed") -> tuple[dict[str, Any], ...]:
        """Current rules (or pending candidates): neither superseded nor withdrawn."""
        response = self._request("GET", f"/principles?status={quote(status, safe='')}")
        return tuple(response["items"])

    def add_principle(
        self,
        *,
        statement: str,
        caller: Caller,
        device_id: str,
        source: str = "manual",
        kind: str = "rule",
        scope: str | None = "*",
        rationale: str = "",
        provenance: dict[str, Any] | None = None,
        status: str = "confirmed",
        domain: str = "",
        supersedes_id: str | None = None,
    ) -> str | None:
        """A confirmed rule's id, or a candidate's (``None`` when seen before).

        ``supersedes_id``: the rule this one corrects, which it replaces."""
        payload: dict[str, Any] = {
            "statement": statement,
            "caller": caller.model_dump(mode="json"),
            "device_id": device_id,
            "source": source,
            "kind": kind,
            "scope": scope,
            "rationale": rationale,
            "provenance": provenance or {},
            "status": status,
            "domain": domain,
        }
        if supersedes_id is not None:
            payload["supersedes_id"] = supersedes_id
        response = self._request("POST", "/principles", payload)
        value = response["principle_id"]
        self.last_trial_id = response.get("trial_id")
        return None if value is None else str(value)

    def record_principle_uses(
        self, ids: list[str], *, task_key: str, device_id: str
    ) -> list[str]:
        """Rules shown in one task's briefing; the trial rules that now hold for good."""
        response = self._request(
            "POST", "/principles/uses",
            {"ids": ids, "task_key": task_key, "device_id": device_id},
        )
        return [str(value) for value in response.get("promoted") or []]

    def record_rule_question(
        self, principle_id: str, *, conversation_id: str, event: str, project: str | None = None
    ) -> bool:
        """Keep that a task asked the owner about a waiting rule ('asked'), or their
        answer ('yes', 'no', 'reword'). False when it was already kept."""
        response = self._request("POST", "/principles/questions", {
            "principle_id": principle_id, "conversation_id": conversation_id,
            "event": event, "project": project,
        })
        return bool(response.get("recorded"))

    def record_task_signals(
        self,
        conversation_id: str,
        receipts: list[dict[str, Any]],
        *,
        project: str | None,
        ready: bool,
        device_id: str,
        agent: str = "",
        model: str = "",
        memory_test: float | None = None,
    ) -> dict[str, Any]:
        """Keep finish_check's per-rule results; a ready task adopts the answers it used.
        ``memory_test`` is the holdout rate the caller's briefings used, if known."""
        payload: dict[str, Any] = {
            "conversation_id": conversation_id, "receipts": receipts, "project": project,
            "ready": ready, "device_id": device_id, "agent": agent, "model": model,
        }
        if memory_test is not None:
            payload["memory_test"] = memory_test
        return self._request("POST", "/tasks/signals", payload)

    def record_ask_signals(
        self,
        escalation_id: str,
        *,
        conversation_id: str | None = None,
        predict: bool = False,
        decision_kind: str = "",
        agent: str = "",
        model: str = "",
        agent_guess: str = "",
        ask_feedback: str = "",
    ) -> dict[str, Any]:
        """The agent's guess at the owner's answer, or the owner's word on whether
        the question was needed."""
        return self._request(
            "POST", f"/escalations/{quote(escalation_id, safe='')}/signals",
            {"conversation_id": conversation_id, "predict": predict,
             "decision_kind": decision_kind, "agent": agent, "model": model,
             "agent_guess": agent_guess, "ask_feedback": ask_feedback},
        )

    def record_rule_correction(
        self,
        principle_id: str,
        *,
        corrected: str | None,
        conversation_id: str | None,
        project: str | None,
        agent_turn: str = "",
        should_have_asked: bool = False,
    ) -> dict[str, Any]:
        """Mark the corrected rule as not held in this task; keep the output it was about."""
        return self._request(
            "POST", f"/principles/{quote(principle_id, safe='')}/correction",
            {"corrected": corrected, "conversation_id": conversation_id, "project": project,
             "agent_turn": agent_turn, "should_have_asked": should_have_asked},
        )

    def nudge_live(self) -> bool:
        """Ask the daemon to read the AI chats' new messages; True when it started a read."""
        return bool(self._request("POST", "/live/nudge", {}).get("reading"))

    def trial_rules(self) -> dict[str, Any]:
        """The trial lane: rules in effect on trial, how trials ended, the spot-check."""
        return dict(self._request("GET", "/principles/trials"))

    def scan_trials(
        self, *, apply: bool, device_id: str, caller: Caller | None = None
    ) -> dict[str, Any]:
        """Sort open candidates into trial and review; ``apply`` starts the trials."""
        payload: dict[str, Any] = {"apply": apply, "device_id": device_id}
        if caller is not None:
            payload["caller"] = caller.model_dump(mode="json")
        return dict(self._request("POST", "/principles/trials/scan", payload))

    def undo_trials(self, since: str, *, caller: Caller, device_id: str) -> int:
        """Withdraw every rule still on trial that went into effect since ``since``."""
        response = self._request(
            "POST", "/principles/trials/undo",
            {"since": since, "caller": caller.model_dump(mode="json"), "device_id": device_id},
        )
        return int(response["undone"])

    def confirm_principle(
        self,
        candidate_id: str,
        *,
        caller: Caller,
        device_id: str,
        statement: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
        rationale: str | None = None,
        domain: str | None = None,
        applies_to: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "caller": caller.model_dump(mode="json"),
            "device_id": device_id,
            "statement": statement,
            "scope": scope,
            "kind": kind,
            "rationale": rationale,
            "domain": domain,
        }
        if applies_to is not None:
            # Sent only when set, so an older daemon still takes the rest.
            payload["applies_to"] = applies_to
        response = self._request(
            "POST", f"/principles/{quote(candidate_id, safe='')}/confirm", payload,
        )
        return str(response["principle_id"])

    def retract_principle(
        self, principle_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        response = self._request(
            "POST",
            f"/principles/{quote(principle_id, safe='')}/retract",
            {"caller": caller.model_dump(mode="json"), "device_id": device_id, "reason": reason},
        )
        return str(response["retraction_id"])

    def workflows(self) -> tuple[dict[str, Any], ...]:
        """Current workflows: neither replaced nor withdrawn."""
        return tuple(self._request("GET", "/workflows")["items"])

    def add_workflow(
        self,
        *,
        name: str,
        steps: list[Any],
        caller: Caller,
        device_id: str,
        source: str = "manual",
        scope: str | None = "*",
        domain: str = "",
        rationale: str = "",
        provenance: dict[str, Any] | None = None,
        basis: str = "stated",
    ) -> str | None:
        """The workflow's id; ``None`` when a learned order changed nothing."""
        response = self._request(
            "POST",
            "/workflows",
            {
                "name": name,
                "steps": steps,
                "caller": caller.model_dump(mode="json"),
                "device_id": device_id,
                "source": source,
                "scope": scope,
                "domain": domain,
                "rationale": rationale,
                "provenance": provenance or {},
                "basis": basis,
            },
        )
        value = response["workflow_id"]
        return None if value is None else str(value)

    def retract_workflow(
        self, workflow_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        response = self._request(
            "POST",
            f"/workflows/{quote(workflow_id, safe='')}/retract",
            {"caller": caller.model_dump(mode="json"), "device_id": device_id, "reason": reason},
        )
        return str(response["retraction_id"])

    def record_workflow_nudge(
        self,
        workflow_id: str,
        *,
        answer: str,
        caller: Caller,
        device_id: str,
        steps: list[str] | None = None,
        task_step: str = "",
        project: str | None = None,
        skip_id: str | None = None,
    ) -> list[str]:
        """Record the owner's answer to a raised step; returns the ids of the new rows."""
        response = self._request(
            "POST",
            f"/workflows/{quote(workflow_id, safe='')}/nudges",
            {
                "answer": answer,
                "steps": steps or [],
                "task_step": task_step,
                "project": project,
                "skip_id": skip_id,
                "caller": caller.model_dump(mode="json"),
                "device_id": device_id,
            },
        )
        return [str(item) for item in response["nudge_ids"]]

    def review_workflow(
        self,
        workflow_id: str,
        *,
        step: str,
        decision: str,
        caller: Caller,
        device_id: str,
        source: str = "review",
    ) -> dict[str, Any]:
        """The owner's keep / optional / drop; the workflow agents follow afterwards."""
        return dict(
            self._request(
                "POST",
                f"/workflows/{quote(workflow_id, safe='')}/reviews",
                {
                    "step": step,
                    "decision": decision,
                    "source": source,
                    "caller": caller.model_dump(mode="json"),
                    "device_id": device_id,
                },
            )
        )

    def pending(self, recipient_id: str | None = None) -> tuple[dict[str, Any], ...]:
        """Open questions; only those routed to ``recipient_id`` when one is given."""
        path = "/escalations"
        if recipient_id is not None:
            path += f"?recipient={quote(recipient_id, safe='')}"
        response = self._request("GET", path)
        return tuple(response["items"])

    def status(self) -> DaemonStatus:
        return DaemonStatus(**self._request("GET", "/status"))

    def stop(self) -> None:
        self._request("POST", "/shutdown", {})

    def _check_archive(self) -> None:
        if self._archive_checked or self.archive is None:
            return
        try:
            served = self._send("GET", "/identity").get("archive")
        except ResolutionClientError as exc:
            if "HTTP 404" not in str(exc):
                raise
            # A daemon from before /identity existed cannot say; it is let
            # through rather than refused, as it was before the check.
            served = None
        if served and not _same_file(served, self.archive):
            raise ArchiveMismatchError(
                f"port {self.port} is served by another Varyz archive ({served}), "
                f"not this config's ({self.archive}). Give this config its own "
                "[resolution] port, then run `varyz daemon start`."
            )
        self._archive_checked = True

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            self._check_archive()
            return self._send(method, path, payload)
        except DaemonUnavailableError:
            # Never replay a timed-out write or an HTTP/authentication failure.
            # Health and shutdown commands remain observations and controls.
            if self.config_path is None or path in {"/status", "/shutdown"}:
                raise
            from varyz.config import load_config
            from varyz.resolution.availability import is_paused

            cfg = load_config(self.config_path)
            if (not cfg.resolution.autostart or is_paused(cfg)
                    or self.host not in {"127.0.0.1", "localhost", "::1"}):
                raise
            if ensure_daemon(self, self.config_path) is None:
                raise
            self._archive_checked = self.archive is None
            self._check_archive()
            return self._send(method, path, payload)

    def _send(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.session_token is not None:
            headers["Authorization"] = f"Bearer {self.session_token}"
        request = Request(self._base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - configured daemon
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ResolutionClientError(f"daemon returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, ConnectionRefusedError) or (
                isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED
            ):
                raise DaemonUnavailableError(f"resolution daemon unavailable: {exc}") from exc
            raise ResolutionClientError(f"resolution daemon unavailable: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ResolutionClientError("daemon returned a non-object response")
        return decoded


def ensure_daemon(
    client: ResolutionClient, config_path: Path, *, timeout: float = 15.0
) -> DaemonStatus | None:
    """Return the running daemon's status, starting it detached if it is down.

    Loading the archive takes a few seconds on a large database, so the wait is
    bounded by a deadline rather than a poll count. ``None`` means it did not
    come up; callers report an outage rather than an empty answer.
    """
    try:
        return client.status()
    except ArchiveMismatchError:
        # Starting another daemon cannot help: the port is taken by the
        # archive it would be mistaken for.
        raise
    except ResolutionClientError:
        pass
    from varyz.config import load_config
    from varyz.resolution.availability import is_paused

    cfg = load_config(config_path)
    if is_paused(cfg):
        return None
    command = [sys.executable, "-m", "varyz", "daemon", "run", "--config", str(config_path)]
    if sys.flags.isolated:
        # The native launcher deliberately ignores foreign PYTHONHOME/PYTHONPATH.
        # Keep that isolation when its CLI starts the long-lived daemon too.
        command.insert(1, "-I")
    # Its own session and process group: the daemon outlives whatever started
    # it. Setup's window or tab closing, a Ctrl+C there, or the Mac setup app
    # ending (which takes its process group with it) must not stop it.
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    process = subprocess.Popen(  # noqa: S603 - fixed local module invocation
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        start_new_session=sys.platform != "win32",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client.status()
        except ArchiveMismatchError:
            raise
        except ResolutionClientError:
            # A second starter that lost the port race exits; the winner serves.
            if process.poll() is not None and time.monotonic() + 0.5 >= deadline:
                break
            time.sleep(0.1)
    return None


__all__ = [
    "ArchiveMismatchError",
    "ResolutionClient",
    "ResolutionClientError",
    "ensure_daemon",
]
