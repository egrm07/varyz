"""Narrow browser bridge for connecting this computer, without archive access."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

CLOUD_ORIGIN = "https://cloud.varyzai.com"
BRIDGE_PATH = "/cloud/computer"
SESSION_SECONDS = 1800
COMPLETION_SECONDS = 30


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def failure_reason(error: Exception) -> str:
    """Map a connection failure to a code the page can explain without leaking text."""
    from varyz.cloud.cli import InactiveAccount, OtherAccount, WrongSignIn
    from varyz.cloud.client import CloudError
    from varyz.db.connection import LockHeldError

    if isinstance(error, OtherAccount):
        return "other_account"
    if isinstance(error, WrongSignIn):
        return "wrong_sign_in"
    if isinstance(error, InactiveAccount):
        return "inactive"
    if isinstance(error, LockHeldError):
        return "busy"
    if isinstance(error, CloudError):
        if "could not be reached" in str(error):
            return "unavailable"
        if str(error) == "Authorization declined.":
            return "declined"
        if str(error).startswith("Authorization timed out"):
            return "timeout"
    if isinstance(error, OSError):
        # Usually the sign-in callback port is still held by an earlier attempt.
        return "busy"
    if str(error) == "Sign in to connect this computer.":
        return "sign_in"
    return "failed"


class ComputerConnection:
    """One expiring browser capability and one OAuth attempt per daemon."""

    def __init__(self, config: Path) -> None:
        self.config = config.resolve()
        self.lock = threading.Lock()
        self.ticket = ""
        self.expires = 0.0
        self.state = "ready"
        self.reason = ""
        self.account_id = ""
        self.preview_data: dict[str, Any] | None = None
        self.approval = ""
        self.result: dict[str, Any] = {}
        self.ticket_hash = ""
        self.approval_hash = ""
        self.session_path = self.config.with_name(self.config.stem + ".cloud-computer.json")
        self._restore()

    def _restore(self) -> None:
        try:
            saved = json.loads(self.session_path.read_text(encoding="utf-8"))
            remaining = min(SESSION_SECONDS, saved["expires_at"] - time.time())
            if remaining <= 0 or saved.get("version") != 1:
                return
            if (
                not all(
                    isinstance(saved[k], str)
                    for k in ("ticket_hash", "account_id", "state", "approval_hash", "reason")
                )
                or len(saved["ticket_hash"]) != 64
                or not isinstance(saved["result"], dict)
                or not isinstance(saved["preview"], (dict, type(None)))
            ):
                return
            self.ticket_hash = saved["ticket_hash"]
            self.account_id = saved["account_id"]
            self.state = saved["state"]
            self.preview_data = saved["preview"]
            self.approval_hash = saved["approval_hash"]
            self.result = saved["result"]
            self.reason = saved["reason"]
            self.expires = time.monotonic() + remaining
            # A crash is not proof that an upload finished. Never replay it.
            if self.state == "syncing":
                self.state, self.result = "interrupted", {}
            elif self.state == "connecting":
                self.state, self.reason = "failed", "interrupted"
        except (OSError, ValueError, KeyError, TypeError):
            return

    def _save(self) -> None:
        from varyz.cloud.local_sync import save_journal

        save_journal(
            self.session_path,
            {
                "version": 1,
                "expires_at": time.time() + max(0, self.expires - time.monotonic()),
                "ticket_hash": self.ticket_hash or _digest(self.ticket),
                "account_id": self.account_id,
                "state": self.state,
                "preview": self.preview_data,
                "approval_hash": self.approval_hash,
                "result": self.result,
                "reason": self.reason,
            },
        )

    def active(self) -> bool:
        """Keep the helper alive until browser setup and its final poll finish."""
        with self.lock:
            return self.state in {"connecting", "syncing"} or (
                bool(self.ticket or self.ticket_hash) and time.monotonic() < self.expires
            )

    def detect(self) -> dict[str, bool | str]:
        with self.lock:
            if self.state in {"connecting", "syncing"}:
                return {"installed": True, "busy": True}
            self.ticket = secrets.token_urlsafe(32)
            self.ticket_hash = _digest(self.ticket)
            self.expires = time.monotonic() + SESSION_SECONDS
            self.state = "ready"
            self.reason = ""
            self.preview_data = None
            self.result = {}
            self.approval_hash = ""
            self._save()
            return {"installed": True, "ticket": self.ticket, "browser_oauth": True}

    def status(self, ticket: str) -> dict[str, Any]:
        with self.lock:
            self._validate(ticket)
            # Authenticated heartbeats keep an open page's session alive.
            self.expires = time.monotonic() + (
                COMPLETION_SECONDS
                if self.state in {"complete", "complete_empty", "complete_limited"}
                else SESSION_SECONDS
            )
            self._save()
            if self.state == "failed":
                return {"state": self.state, "reason": self.reason}
            return {**self.result, "state": self.state}

    def start(
        self,
        ticket: str,
        account_id: str,
        *,
        switch: bool = False,
        resume_only: bool = False,
        browser_oauth: bool = False,
    ) -> dict[str, str]:
        with self.lock:
            self._validate(ticket)
            if not account_id or len(account_id) > 128:
                raise ValueError("Cloud account identity is required.")
            if self.state != "ready":
                raise ValueError("Connection request already used.")
            self.state = "connecting"
            self.account_id = account_id
            self._save()
            threading.Thread(
                target=self._connect,
                args=(account_id, switch, resume_only, browser_oauth),
                name="varyz-computer-connect",
                daemon=True,
            ).start()
            return {"state": self.state}

    def _validate(self, ticket: str) -> None:
        if (
            not ticket
            or time.monotonic() >= self.expires
            or not secrets.compare_digest(
                _digest(ticket),
                self.ticket_hash or _digest(self.ticket),
            )
        ):
            raise ValueError("Connection request expired. Try again.")

    def _open_browser(self, url: str) -> bool:
        """Hand OAuth to the original page's user-opened, automatically closable tab."""
        if not url.startswith(CLOUD_ORIGIN + "/oauth/authorize?"):
            return False
        with self.lock:
            self.result = {"authorization_url": url}
        return True

    def _connect(
        self,
        account_id: str,
        switch: bool = False,
        resume_only: bool = False,
        browser_oauth: bool = False,
    ) -> None:
        from varyz.cloud.cli import connect_account

        try:
            options: dict[str, Any] = {}
            if resume_only:
                options["resume_only"] = True
            if browser_oauth:
                options["open_browser"] = self._open_browser
            connect_account(
                CLOUD_ORIGIN,
                self.config,
                require_active=True,
                expected_account=account_id,
                return_ticket=self.ticket,
                switch_account=switch,
                **options,
            )
            state, reason = "connected", ""
        except Exception as error:
            # Provider/keyring exceptions may contain credentials. Only a fixed reason
            # code reaches the page, never the message.
            state, reason = "failed", failure_reason(error)
        with self.lock:
            self.state = state
            self.result = {}
            self.reason = reason
            self.expires = time.monotonic() + SESSION_SECONDS
            self._save()

    def setup(self, ticket: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        from varyz.cloud.guided import GuidedSetup

        with self.lock:
            self._validate(ticket)
            approval = payload.get("approval")
            if (
                action == "finish"
                and payload.get("approve") is True
                and self.state
                in {
                    "syncing",
                    "complete",
                    "complete_empty",
                    "complete_limited",
                    "interrupted",
                    "sync_failed",
                    "stale",
                    "conflict",
                    "verification_failed",
                }
                and isinstance(approval, str)
                and self.approval_hash
                and secrets.compare_digest(_digest(approval), self.approval_hash)
            ):
                return {**self.result, "state": self.state}
            if self.state not in {
                "connected",
                "preview_ready",
                "stale",
                "sync_failed",
                "conflict",
                "verification_failed",
                "complete",
                "complete_empty",
                "complete_limited",
                "interrupted",
            }:
                raise ValueError("Finish connecting this computer first.")
            setup = GuidedSetup(self.config, self.account_id)
            if action == "choices":
                choices = setup.choices()
                if self.preview_data:
                    projects = self.preview_data["projects"]
                    choices.update(
                        selected=[p for p in projects if p != "*"],
                        all_projects=not projects,
                        history_selected=self.preview_data.get("history_projects", []),
                        report_selected=self.preview_data.get("report_projects", []),
                    )
                    from varyz.scopes import normalize_scope

                    choices["sync_selected"] = sorted(
                        {
                            normalize_scope(p)
                            for p in choices["selected"]
                            + choices["history_selected"]
                            + choices["report_selected"]
                        }
                    )
                return choices
            if action == "preview":
                if payload.get("selection") == "projects":
                    self.preview_data = setup.preview_projects(payload.get("projects"))
                else:
                    # Older installed browser scripts remain supported during rollout.
                    scopes = setup.scopes(payload.get("projects"), payload.get("all_projects"))
                    self.preview_data = setup.preview(
                        scopes,
                        payload.get("history_projects", []),
                        payload.get("report_projects", []),
                    )
                self.approval = secrets.token_urlsafe(32)
                self.approval_hash = _digest(self.approval)
                self.state = "preview_ready"
                self.expires = time.monotonic() + SESSION_SECONDS
                self._save()
                return {
                    k: v
                    for k, v in self.preview_data.items()
                    if k not in {"signature", "history_signature", "report_signature"}
                } | {
                    "approval": self.approval,
                }
            if action != "finish" or payload.get("approve") is not True:
                raise ValueError("Approve the preview before syncing.")
            approval = payload.get("approval")
            if (
                self.state != "preview_ready"
                or not isinstance(approval, str)
                or not approval
                or (not secrets.compare_digest(_digest(approval), self.approval_hash))
            ):
                raise ValueError("Review a new preview before approving.")
            preview = self.preview_data
            if preview is None:
                raise ValueError("Review a new preview before approving.")
            self.approval = ""
            self.state = "syncing"
            self.result = {}
            self._save()
            threading.Thread(
                target=self._finish,
                args=(setup, preview),
                name="varyz-computer-sync",
                daemon=True,
            ).start()
            return {"state": self.state}

    def _finish(self, setup, preview) -> None:
        from varyz.cloud.client import CloudConflict, CloudStorageLimit
        from varyz.cloud.guided import PreviewChanged

        try:
            result = setup.finish(preview)
        except PreviewChanged:
            result = {"state": "stale"}
        except CloudConflict:
            result = {"state": "conflict"}
        except CloudStorageLimit:
            result = {"state": "sync_failed", "reason": "storage_limit"}
        except Exception:
            result = {"state": "sync_failed"}
        with self.lock:
            self.result = result
            self.state = result["state"]
            self.expires = time.monotonic() + (
                COMPLETION_SECONDS
                if self.state in {"complete", "complete_empty", "complete_limited"}
                else SESSION_SECONDS
            )
            self._save()


def handle_computer_request(
    handler: BaseHTTPRequestHandler,
    bridge: ComputerConnection | None,
) -> bool:
    """Handle only the bridge, keeping daemon authentication and CORS isolated."""
    path = urlsplit(handler.path).path
    actions = {BRIDGE_PATH + "/" + action: action for action in ("choices", "preview", "finish")}
    identity_path = BRIDGE_PATH + "/identity"
    if path != BRIDGE_PATH and path not in actions and path != identity_path:
        return False
    host, port = cast(tuple[str, int], handler.server.server_address)[:2]
    allowed = (
        host == "127.0.0.1"
        and handler.headers.get("Host") == f"127.0.0.1:{port}"
        and handler.headers.get("Origin") == CLOUD_ORIGIN
    )

    def reply(status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        if allowed:
            handler.send_header("Access-Control-Allow-Origin", CLOUD_ORIGIN)
            handler.send_header("Vary", "Origin")
            handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            handler.send_header(
                "Access-Control-Allow-Headers",
                "X-Varyz-Ticket, X-Varyz-Account, X-Varyz-Switch, X-Varyz-Resume, "
                "X-Varyz-Browser, Content-Type",
            )
            handler.send_header("Access-Control-Allow-Private-Network", "true")
        handler.end_headers()
        handler.wfile.write(body)

    if not allowed:
        reply(403, {"error": "Connection origin refused."})
    elif handler.command == "OPTIONS":
        reply(200, {})
    elif bridge is None or not bridge.config.is_file():
        reply(503, {"error": "Local setup is unavailable."})
    elif path == identity_path:
        if handler.command == "GET":
            reply(200, {"config_hash": _digest(str(bridge.config))})
        else:
            reply(405, {"error": "Unsupported method."})
    else:
        try:
            ticket = handler.headers.get("X-Varyz-Ticket", "")
            if path in actions:
                action = actions[path]
                if handler.command == "GET" and action == "choices":
                    reply(200, bridge.setup(ticket, action, {}))
                elif handler.command == "POST" and action in {"preview", "finish"}:
                    length = int(handler.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16_384 or handler.headers.get("Transfer-Encoding"):
                        raise ValueError("Invalid setup request.")
                    payload = json.loads(handler.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid setup request.")
                    reply(202 if action == "finish" else 200, bridge.setup(ticket, action, payload))
                else:
                    reply(405, {"error": "Unsupported method."})
            elif handler.command == "GET":
                reply(200, bridge.status(ticket) if ticket else bridge.detect())
            elif handler.command == "POST":
                # No body, URL, config path or arbitrary operation is accepted.
                if handler.headers.get("Content-Length", "0") != "0" or handler.headers.get(
                    "Transfer-Encoding"
                ):
                    raise ValueError("Connection request must be empty.")
                reply(
                    202,
                    bridge.start(
                        ticket,
                        handler.headers.get("X-Varyz-Account", ""),
                        switch=handler.headers.get("X-Varyz-Switch") == "1",
                        resume_only=handler.headers.get("X-Varyz-Resume") == "1",
                        browser_oauth=handler.headers.get("X-Varyz-Browser") == "1",
                    ),
                )
            else:
                reply(405, {"error": "Unsupported method."})
        except ValueError as error:
            reply(400, {"error": str(error)})
        except Exception:
            reply(503, {"error": "Could not prepare setup. Check local Varyz and try again."})
    return True
