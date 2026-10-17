"""HTTPS client and browser authorization. Credentials stay in the OS keyring."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import keyring
import keyring.errors

CALLBACK = "http://127.0.0.1:8766/callback"


def cloud_url(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Use an HTTPS cloud origin (HTTP is allowed only for localhost).")
    return value.rstrip("/")


def credential_name(config_path: Path, url: str) -> str:
    label = str(config_path.resolve()) + "\n" + cloud_url(url)
    return "cloud:" + hashlib.sha256(label.encode()).hexdigest()


def load_tokens(name: str) -> dict[str, Any]:
    value = keyring.get_password("varyz", name)
    if not value:
        raise ValueError("Cloud is disconnected; connect it before syncing.")
    return json.loads(value)


class CloudError(ValueError):
    """A safe operational error; do not include server responses containing preferences."""


class CloudStorageLimit(CloudError):
    """The selected history or reports exceed Cloud storage capacity."""


class CloudAccessInactive(CloudError):
    """Billing ended; keep credentials and local data for automatic recovery."""

    def __init__(self, body=None):
        from varyz.cloud.access import response_notice

        self.access = response_notice(body)
        super().__init__(self.access["message"])


class CloudProjectPaused(CloudError):
    def __init__(self, project: str):
        from varyz.scopes import stored_scope

        self.project = stored_scope(project)
        super().__init__("Project syncing is paused; saved Cloud context remains available.")


class CloudConflict(CloudError):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(
            "Cloud sync conflict: local changes were kept. Review the cloud record, then "
            "retry with --accept-remote or --keep-local to resolve it explicitly."
        )
        self.record = record


class CloudClient:
    def __init__(
        self,
        url: str,
        tokens: dict[str, Any],
        *,
        credential: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.url = cloud_url(url)
        self.tokens = tokens
        self.credential = credential
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_http = http is None
        self.computer_header = json.dumps(
            {"name": platform.node()[:120], "platform": platform.system()}, ensure_ascii=True
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid cloud API path.")
        try:
            renewals = retries = 0
            while True:
                # Keep the token that produced this response: another request on
                # this client may renew its in-memory tokens while HTTP is pending.
                access_token = self.tokens["access_token"]
                response = self.http.request(
                    method,
                    self.url + path,
                    headers={
                        "Authorization": "Bearer " + access_token,
                        "X-Varyz-Computer": self.computer_header,
                    },
                    **kwargs,
                )
                # Renew after any rejected attempt, including an overload retry.
                # Another process may rotate the token during the backoff delay.
                if (
                    response.status_code == 401
                    and self.tokens.get("refresh_token")
                    and renewals < 2
                ):
                    self._refresh(access_token)
                    renewals += 1
                    continue
                # Only overload rejected before work started can replay writes.
                if retries >= 3:
                    break
                if response.status_code != 429 and not (
                    response.status_code == 503
                    and response.headers.get("X-Varyz-Retry-Safe") == "1"
                ):
                    break
                try:
                    delay = min(60, max(1, int(response.headers.get("Retry-After", "30"))))
                except ValueError:
                    delay = 30
                delay = max(delay, 2 ** (retries + 1))
                # Desynchronize clients waking after the same burst.
                time.sleep(delay + secrets.randbelow(1000) / 1000)
                retries += 1
        except httpx.HTTPError as exc:
            raise CloudError(
                "Cloud could not be reached; local preferences remain available."
            ) from exc
        if response.status_code == 423:
            raise CloudProjectPaused(response.json().get("project", "*"))
        if response.status_code == 404 and path == "/api/project-sync":
            return {"paused_projects": []}  # Compatibility with older servers.
        if response.status_code == 409:
            current = response.json().get("current")
            if not isinstance(current, dict):
                raise CloudError("Cloud returned an invalid conflict response.")
            raise CloudConflict(current)
        if response.status_code == 402:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise CloudAccessInactive(body)
        if response.status_code == 403:
            raise CloudError("This Cloud connection does not have permission for this action.")
        if response.status_code == 401:
            raise CloudError("Cloud authorization expired or was revoked; reconnect.")
        if response.status_code in {413, 507} and path in {
            "/api/archive/sync",
            "/api/reports/sync",
            "/api/sync",
        }:
            raise CloudStorageLimit("Cloud storage is full. Review the selected projects.")
        if response.is_error:
            raise CloudError(
                f"Cloud request failed (HTTP {response.status_code}); no sync was acknowledged."
            )
        return response.json() if response.content else {}

    def _refresh(self, rejected: str) -> None:
        """Rotate the refresh token, once per rotation across every Varyz process.

        Cloud treats a second use of a spent refresh token as theft and signs this
        computer out. The daemon, its long-poll thread, hooks and MCP servers each
        hold the saved tokens, so the first to see them expire rotates them under a
        lock and the rest adopt the saved result instead of replaying the old token.
        """
        if not self.credential:
            self._rotate()
            return
        from varyz.db.connection import FileLock
        from varyz.legacy import home_path

        override = os.environ.get("VARYZ_HOME")
        folder = Path(override) / "cloud" if override else home_path("cloud")
        lock = FileLock(folder / (self.credential.split(":")[-1] + ".refresh.lock"))
        deadline = time.monotonic() + 30
        while not lock.acquire():
            if time.monotonic() > deadline:
                raise CloudError("Cloud sign-in is being renewed by another Varyz process.")
            time.sleep(0.1)
        try:
            with contextlib.suppress(ValueError, keyring.errors.KeyringError):
                saved = load_tokens(self.credential)
                if saved.get("access_token") not in (None, rejected):
                    self.tokens = saved
                    return
            self._rotate()
        finally:
            lock.release()

    def _rotate(self) -> None:
        response = self.http.post(
            self.url + "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],
                "client_id": "varyz-local",
            },
        )
        if response.is_error:
            raise CloudError("Cloud authorization expired or was revoked; reconnect.")
        tokens = response.json()
        if not isinstance(tokens.get("access_token"), str):
            raise CloudError("Cloud returned an invalid credential.")
        self.tokens = tokens
        if self.credential:
            keyring.set_password("varyz", self.credential, json.dumps(tokens))

    def paused_projects(self) -> set[str]:
        from varyz.scopes import stored_scope

        result = self.request("GET", "/api/project-sync")
        return {stored_scope(p) for p in result["paused_projects"]}

    def push(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.request("POST", "/api/sync", json={"records": records})["records"]

    def pull(self, cursor: int, wait: int = 0) -> dict[str, Any]:
        """The changes after ``cursor``; ``wait`` holds the request open up to that many
        seconds for another device's change (``waited`` in the answer when the server can)."""
        params = {"cursor": cursor, "limit": 500}
        if not wait:
            return self.request("GET", "/api/sync", params=params)
        return self.request("GET", "/api/sync", params={**params, "wait": wait}, timeout=wait + 30)


def browser_connect(
    url: str,
    *,
    timeout: float = 600,
    return_ticket: str | None = None,
    open_browser: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Receive one PKCE code on a fixed loopback callback; never a bearer in the URL."""
    origin = cloud_url(url)
    if return_ticket is not None and not re.fullmatch(r"[A-Za-z0-9_-]{43}", return_ticket):
        raise ValueError("Invalid computer connection ticket.")
    destination = origin + ("/#computer=" + return_ticket if return_ticket else "/#connect-varyz")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback" or query.get("state") != [state]:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid authorization callback.")
                return
            if "code" in query and len(query["code"]) == 1:
                result["code"] = query["code"][0]
            else:
                result["error"] = "Authorization declined."
            # This is a temporary sign-in tab. The originating Cloud page already
            # polls its capability; sending this tab to setup would duplicate it.
            import html

            nonce = secrets.token_urlsafe(24)
            body = (
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width">'
                "<title>Varyz connected</title>"
                "<style>body{background:#11120f;color:#f4f1e8;"
                "font:17px/1.6 'IBM Plex Sans','Segoe UI',Arial,sans-serif;"
                "max-width:480px;margin:12vh auto;padding:24px}"
                "h1{font:400 40px/1.1 Marcellus,Georgia,serif;letter-spacing:-.03em}"
                "p{color:#aaa99f}a{color:#ffc23d}</style></head>"
                "<body><h1>Sign-in received</h1><p>Varyz is checking your connection. "
                'You can return to Cloud.</p><p id="help" hidden>'
                "This browser kept the tab open. "
                'You can close it.</p><a href="'
                + html.escape(destination, quote=True)
                + '">Return to Cloud</a><script nonce="'
                + nonce
                + '">'
                'setTimeout(()=>{window.close();document.getElementById("help").hidden=false;},800);'
                "</script></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{nonce}'; frame-ancestors 'none'",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    with HTTPServer(("127.0.0.1", 8766), CallbackHandler) as server:
        server.timeout = 1
        authorization = (
            origin
            + "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": "varyz-local",
                    "redirect_uri": CALLBACK,
                    "scope": "varyz",
                    "state": state,
                    "code_challenge": challenge.decode(),
                    "code_challenge_method": "S256",
                }
            )
        )
        if not (open_browser or webbrowser.open)(authorization):
            raise CloudError("Could not open the authorization browser; check the default browser.")
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in result:
        raise CloudError(result.get("error", "Authorization timed out; no account was connected."))
    try:
        response = httpx.post(
            origin + "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "varyz-local",
                "code": result["code"],
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
            },
            timeout=30,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise CloudError("Cloud authorization could not be completed.") from exc
    if response.is_error:
        raise CloudError("Cloud rejected authorization; reconnect.")
    tokens = response.json()
    if not isinstance(tokens.get("access_token"), str):
        raise CloudError("Cloud returned an invalid credential.")
    return tokens
