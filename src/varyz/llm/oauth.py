"""OAuth 2.0 PKCE plumbing shared by the Claude and OpenAI sign-in flows.

Tokens live in the OS credential store as JSON blobs with expiry; providers
call `TokenStore.access_token()` which refreshes transparently. Provider-
specific constants (client ids, endpoints) live in the provider modules.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets as pysecrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx

from varyz import secrets as secret_store


def generate_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return pysecrets.token_urlsafe(24)


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # unix seconds
    extra: dict | None = None        # provider-specific (account ids, id_token…)

    def expired(self, slack_s: int = 120) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at - slack_s

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str) -> OAuthTokens:
        return cls(**json.loads(raw))


class OAuthError(RuntimeError):
    pass


def oauth_dir() -> Path:
    """Per-user directory for OAuth token bundles. Overridable via VARYZ_HOME
    (used by tests). Defaults to ~/.varyz/oauth, or ~/.varys/oauth on a machine
    that signed in before the rename."""
    import os

    from varyz.legacy import home_path

    override = os.environ.get("VARYZ_HOME")
    d = Path(override) / "oauth" if override else home_path("oauth")
    d.mkdir(parents=True, exist_ok=True)
    return d


class TokenStore:
    """File-backed token storage with transparent refresh.

    OAuth bundles (access + refresh + id_token JWT) exceed the Windows
    Credential Manager 2560-byte blob limit, so they live in a file — the same
    approach Codex/gcloud/gh use (auth.json). Small API keys stay in keyring.
    """

    def __init__(self, secret_name: str, refresh_fn):
        """refresh_fn(refresh_token) -> OAuthTokens (raises OAuthError)."""
        self.secret_name = secret_name
        self.refresh_fn = refresh_fn

    @property
    def path(self) -> Path:
        return oauth_dir() / f"{self.secret_name}.json"

    def load(self) -> OAuthTokens | None:
        if self.path.exists():
            return OAuthTokens.from_json(self.path.read_text(encoding="utf-8"))
        # Fall back to a legacy keyring value from before file storage.
        raw = secret_store.get_secret(self.secret_name)
        return OAuthTokens.from_json(raw) if raw else None

    def save(self, tokens: OAuthTokens) -> None:
        import os

        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(tokens.to_json(), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic

    def clear(self) -> None:
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        secret_store.delete_secret(self.secret_name)  # also drop any legacy value

    def access_token(self) -> str:
        tokens = self.load()
        if tokens is None:
            raise OAuthError(f"not signed in ({self.secret_name})")
        if tokens.expired():
            if not tokens.refresh_token:
                raise OAuthError("token expired and no refresh token - sign in again")
            tokens = self.refresh_fn(tokens.refresh_token)
            self.save(tokens)
        return tokens.access_token


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot localhost redirect target: captures ?code=&state= and replies
    with a small 'return to varyz' page."""

    server_version = "varyz-oauth"
    result: dict = {}

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != type(self).expected_path:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in type(self).result
        self.wfile.write(
            ("<html><head><meta charset='utf-8'><style>body{background:#11120f;color:#f4f1e8;"
             "font:17px/1.6 'IBM Plex Sans','Segoe UI',Arial,sans-serif;max-width:480px;"
             "margin:12vh auto;padding:24px}h2{font:400 36px/1.1 Marcellus,Georgia,serif;"
             "letter-spacing:-.03em}p{color:#aaa99f}</style></head><body>"
             + ("<h2>Signed in.</h2><p>You can close this tab and return to varyz.</p>"
                if ok else "<h2>Sign-in failed.</h2><p>Return to the terminal.</p>")
             + "</body></html>").encode()
        )

    def log_message(self, *args):  # silence request logging
        pass


class CallbackListener:
    """A one-shot localhost redirect target that can be cancelled/closed early
    (so a stuck web sign-in can free the port and let the user retry)."""

    def __init__(self, port: int, path: str):
        self._handler = type(
            "Handler", (_CallbackHandler,), {"expected_path": path, "result": {}}
        )
        self._server = http.server.HTTPServer(("127.0.0.1", port), self._handler)

    def wait(self, timeout_s: int = 180) -> dict:
        self._server.timeout = timeout_s
        done = threading.Event()

        def run():
            while not self._handler.result and not done.is_set():
                try:
                    self._server.handle_request()
                except OSError:
                    break  # socket closed by close()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        done.wait(timeout_s)
        return dict(self._handler.result)

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._server.server_close()


def wait_for_callback(port: int, path: str, timeout_s: int = 300) -> dict:
    """Serve one request on 127.0.0.1:port and return its query params."""
    listener = CallbackListener(port, path)
    try:
        result = listener.wait(timeout_s)
    finally:
        listener.close()
    if not result:
        raise OAuthError("timed out waiting for the browser sign-in")
    return result


def exchange_token(url: str, data: dict, headers: dict | None = None) -> dict:
    resp = httpx.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthError(f"token endpoint returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def exchange_token_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    resp = httpx.post(url, json=payload, headers=headers or {}, timeout=30)
    if resp.status_code != 200:
        raise OAuthError(f"token endpoint returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def open_browser(url: str) -> None:
    import contextlib
    import webbrowser

    with contextlib.suppress(Exception):  # headless: URL is printed for manual opening
        webbrowser.open(url)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying (we minted it ourselves via the
    token endpoint; we only read claims like the account id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
