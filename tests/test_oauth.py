"""OAuth mechanics: PKCE, token store + refresh, callback server, and a full
sign-in flow driven against a local mock authorize/token server.

External endpoints (claude.ai, auth.openai.com) can't be reached in CI, so we
point the provider modules at a localhost mock and exercise the real code
paths: authorize-URL build, code->token exchange, refresh, and account decode.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from varyz.llm import claude, openai_oauth
from varyz.llm.oauth import (
    OAuthTokens,
    TokenStore,
    decode_jwt_claims,
    generate_pkce,
    wait_for_callback,
)


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("varyz.secrets.get_secret", lambda n: store.get(n))
    monkeypatch.setattr("varyz.secrets.set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr("varyz.secrets.delete_secret", lambda n: store.pop(n, None))
    return store


# ---- PKCE + JWT --------------------------------------------------------------


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert "=" not in verifier and "=" not in challenge


def _make_jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_decode_jwt_claims_reads_payload():
    token = _make_jwt({"email": "egor@example.com", "sub": "u1"})
    assert decode_jwt_claims(token)["email"] == "egor@example.com"


def test_decode_jwt_bad_token_is_empty():
    assert decode_jwt_claims("not-a-jwt") == {}


# ---- token store + refresh ---------------------------------------------------


def test_token_store_roundtrip_and_clear(fake_keyring):
    store = TokenStore("x_tokens", refresh_fn=lambda r: None)
    assert store.load() is None
    store.save(OAuthTokens(access_token="a", refresh_token="r", expires_at=time.time() + 999))
    assert store.access_token() == "a"
    store.clear()
    assert store.load() is None


def test_expired_token_triggers_refresh(fake_keyring):
    calls = []

    def refresh(rt):
        calls.append(rt)
        return OAuthTokens(access_token="fresh", refresh_token="r2", expires_at=time.time() + 999)

    store = TokenStore("y_tokens", refresh_fn=refresh)
    store.save(OAuthTokens(access_token="stale", refresh_token="r1", expires_at=time.time() - 10))
    assert store.access_token() == "fresh"
    assert calls == ["r1"]
    assert store.load().refresh_token == "r2"  # refreshed token persisted


def test_expired_without_refresh_token_errors(fake_keyring):
    from varyz.llm.oauth import OAuthError

    store = TokenStore("z_tokens", refresh_fn=lambda r: None)
    store.save(OAuthTokens(access_token="stale", refresh_token=None, expires_at=time.time() - 10))
    with pytest.raises(OAuthError):
        store.access_token()


def test_large_token_bundle_roundtrips(fake_keyring):
    """OpenAI's access+id_token bundle exceeds the Windows Credential Manager
    2560-byte blob limit (error 1783 CredWrite); file storage must handle it."""
    big = OAuthTokens(
        access_token="A" * 2000,
        refresh_token="R" * 800,
        expires_at=time.time() + 999,
        extra={"id_token": "J" * 1500, "account_id": "acct_1"},
    )
    assert len(big.to_json()) > 2560  # would fail CredWrite
    store = TokenStore("big_tokens", refresh_fn=lambda r: None)
    store.save(big)
    loaded = store.load()
    assert loaded.access_token == "A" * 2000
    assert loaded.extra["account_id"] == "acct_1"


# ---- localhost callback capture ---------------------------------------------


def test_wait_for_callback_captures_query():
    import httpx

    port = 1479
    result: dict = {}

    def hit():
        time.sleep(0.3)
        httpx.get(f"http://127.0.0.1:{port}/cb?code=abc&state=xyz")

    threading.Thread(target=hit, daemon=True).start()
    result = wait_for_callback(port, "/cb", timeout_s=5)
    assert result == {"code": "abc", "state": "xyz"}


# ---- full mock OAuth server: authorize + token endpoints ---------------------


class MockOAuthServer(http.server.BaseHTTPRequestHandler):
    tokens = {
        "access_token": "ACCESS",
        "refresh_token": "REFRESH",
        "expires_in": 3600,
    }
    received: dict = {}
    received_ua: str = ""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        type(self).received_ua = self.headers.get("User-Agent", "")
        try:
            type(self).received = json.loads(body)
        except json.JSONDecodeError:
            type(self).received = {k: v[0] for k, v in parse_qs(body).items()}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).tokens).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_token_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockOAuthServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/token"
    server.shutdown()


def test_claude_full_login_exchange(monkeypatch, fake_keyring, mock_token_server):
    monkeypatch.setattr(claude, "OAUTH_TOKEN_URL", mock_token_server)

    url, verifier, state = claude.build_authorize_url()
    parsed = parse_qs(urlparse(url).query)
    assert parsed["code_challenge_method"] == ["S256"]
    assert parsed["client_id"] == [claude.OAUTH_CLIENT_ID]
    assert parsed["state"] == [state]

    # The provider redirects to console.anthropic.com and shows "code#state".
    tokens = claude.complete_login(f"THECODE#{state}", verifier, state)
    assert tokens.access_token == "ACCESS"
    assert claude.token_store().load().refresh_token == "REFRESH"
    assert MockOAuthServer.received["code"] == "THECODE"
    assert MockOAuthServer.received["code_verifier"] == verifier
    assert MockOAuthServer.received["grant_type"] == "authorization_code"
    # A User-Agent must be present or Cloudflare 403s the token endpoint.
    assert "claude" in MockOAuthServer.received_ua.lower()


def test_claude_refresh_uses_token_endpoint(monkeypatch, fake_keyring, mock_token_server):
    monkeypatch.setattr(claude, "OAUTH_TOKEN_URL", mock_token_server)
    refreshed = claude.refresh_tokens("OLD_REFRESH")
    assert refreshed.access_token == "ACCESS"
    assert MockOAuthServer.received["grant_type"] == "refresh_token"
    assert MockOAuthServer.received["refresh_token"] == "OLD_REFRESH"


def test_openai_full_login_exchange(monkeypatch, fake_keyring, mock_token_server):
    id_token = _make_jwt(
        {"email": "egor@example.com",
         "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}}
    )
    MockOAuthServer.tokens = {
        "access_token": "OA_ACCESS", "refresh_token": "OA_REFRESH",
        "expires_in": 3600, "id_token": id_token,
    }
    monkeypatch.setattr(openai_oauth, "TOKEN_URL", mock_token_server)

    url, verifier, state = openai_oauth.build_authorize_url()
    parsed = parse_qs(urlparse(url).query)
    assert parsed["redirect_uri"] == [openai_oauth.REDIRECT_URI]
    assert parsed["scope"] == [openai_oauth.SCOPES]

    tokens = openai_oauth.complete_login("OACODE", verifier)
    assert tokens.access_token == "OA_ACCESS"
    assert tokens.extra["account_id"] == "acct_123"
    assert openai_oauth.account_label() == "egor@example.com"
    assert "codex" in MockOAuthServer.received_ua.lower()  # the 403 fix
    MockOAuthServer.tokens = {  # reset for other tests
        "access_token": "ACCESS", "refresh_token": "REFRESH", "expires_in": 3600,
    }


def test_openai_sse_parsing():
    lines = [
        "event: response.output_text.delta",
        'data: {"type": "response.output_text.delta", "delta": "Hello"}',
        "",
        'data: {"type": "response.output_text.delta", "delta": " world"}',
        'data: {"type": "response.completed"}',
        "data: [DONE]",
    ]
    assert "".join(openai_oauth._parse_sse_text(iter(lines))) == "Hello world"


def test_openai_oauth_payload_omits_unsupported_token_limit():
    provider = openai_oauth.OpenAIOAuthProvider("gpt-5.5", max_tokens=123)
    payload = provider._payload("system", "user")
    assert payload["model"] == "gpt-5.5"
    assert payload["stream"] is True
    assert "max_output_tokens" not in payload
