"""Adversarial regression checks from the 2026-10-03 Cloud audit."""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app, redirect_kind, redirect_registered


@pytest.fixture
def service(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    return TestClient(app, base_url="http://127.0.0.1:8787"), app.state.store


@pytest.mark.parametrize(
    "uri",
    [
        "https://connector.test\n/callback",
        "https://connector.test /callback",
        "https://connector.test;script-src 'unsafe-inline'/callback",
        "https://connector.test\\@attacker.test/callback",
        "https://[broken/callback",
        "https://connector.test:invalid/callback",
    ],
)
def test_redirect_parser_rejects_ambiguous_urls(uri):
    assert redirect_kind(uri) is None
    # Existing registrations from before hardening cannot bypass validation.
    assert not redirect_registered([uri], uri)


def test_invalid_utf8_form_is_client_error(service):
    client, _ = service
    assert client.post("/oauth/token", content=b"\xff").status_code == 400


def test_unicode_csrf_does_not_crash(service):
    client, _ = service
    client.get("/login")
    assert client.post("/login", data={"csrf": "\u00e9"}).status_code == 403


def test_revoking_refresh_removes_connection_access_but_not_other_accounts(service):
    client, store = service
    access = store.credential("alice", "access", 3600, "connector")
    refresh = store.credential("alice", "refresh", 3600, "connector")
    other = store.credential("bob", "access", 3600, "connector")
    separate = store.credential("alice", "access", 3600, "other-connector")
    response = client.post("/oauth/revoke", data={"token": refresh, "client_id": "connector"})
    assert response.status_code == 200
    assert store.lookup(access, "access") is None
    assert store.lookup(refresh, "refresh") is None
    assert store.lookup(other, "access") is not None
    assert store.lookup(separate, "access") is not None


def test_wrong_client_cannot_revoke_connection(service):
    client, store = service
    refresh = store.credential("alice", "refresh", 3600, "connector")
    response = client.post("/oauth/revoke", data={"token": refresh, "client_id": "evil"})
    assert response.status_code == 200
    assert store.lookup(refresh, "refresh") is not None


def test_stolen_rotated_refresh_revokes_current_tokens(service):
    client, store = service
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
            ("alice", "alice@example.test", "unused"),
        )
    refresh = store.credential("alice", "refresh", 3600, "connector")
    other = store.credential("alice", "access", 3600, "other-connector")
    fields = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "connector"}
    tokens = client.post("/oauth/token", data=fields).json()
    assert store.lookup(tokens["access_token"], "access") is not None
    # A wrong client cannot use a consumed token to sign out its rightful owner.
    assert client.post("/oauth/token", data={**fields, "client_id": "evil"}).status_code == 400
    assert store.lookup(tokens["access_token"], "access") is not None
    assert client.post("/oauth/token", data=fields).status_code == 400
    assert store.lookup(tokens["access_token"], "access") is None
    assert store.lookup(tokens["refresh_token"], "refresh") is None
    assert store.lookup(other, "access") is not None


def test_oauth_login_flows_have_a_capacity_limit(service):
    client, store = service
    with store.connection() as db:
        db.executemany(
            "INSERT INTO oauth_flows VALUES(?,?,?)",
            [(str(i), "{}", 9999999999) for i in range(10000)],
        )
    response = client.get(
        "/oauth/authorize",
        params={
            "client_id": "varyz-local",
            "redirect_uri": "http://127.0.0.1:8766/callback",
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": "a" * 43,
        },
        follow_redirects=False,
    )
    assert response.status_code == 503


def test_slow_request_body_cannot_hold_a_worker_forever(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "slow.sqlite"), body_timeout=0.02))

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            # Stay stalled until the server cancels the receive. Two short timers
            # can both expire during a busy Windows scheduler tick, hiding the stall.
            await asyncio.Event().wait()
            yield b"}"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as client:
            response = await client.post("/mcp", content=SlowBody())
            assert response.status_code == 408
            assert (await client.get("/health")).status_code == 200

    asyncio.run(run())


def test_large_stream_without_content_length_is_rejected(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "large.sqlite"), body_limit=4))

    class LargeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as client:
            assert (await client.post("/mcp", content=LargeBody())).status_code == 413

    asyncio.run(run())


def test_https_cookies_require_host_prefix_and_ignore_legacy_session(tmp_path):
    app = create_app(
        Settings(database=str(tmp_path / "cookies.sqlite"), public_url="https://cloud.example.test")
    )
    client = TestClient(app, base_url="https://cloud.example.test")
    with app.state.store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
            ("alice", "alice@example.test", "unused"),
        )
    token = app.state.store.credential("alice", "session", 3600)
    client.cookies.set("session", token, domain=".example.test")
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-csrf=" in cookie and "Secure" in cookie and "HttpOnly" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie
    client.cookies.set("__Host-session", token, domain="cloud.example.test")
    assert client.get("/login", follow_redirects=False).status_code == 303


def test_security_log_omits_token_email_and_query(service, caplog):
    client, _ = service
    client.get("/api/account?token=private-query", headers={"Authorization": "Bearer secret-token"})
    assert "security_event=request_rejected" in caplog.text
    assert "private-query" not in caplog.text and "secret-token" not in caplog.text
    caplog.clear()
    client.get("/api/account", headers={"Origin": "https://foreign.example"})
    assert "status=403" in caplog.text


def test_login_limit_is_shared_across_peer_addresses(service):
    client, store = service
    client.get("/login")
    csrf = client.cookies.get("csrf")
    from varyz.cloud.store import digest

    for _ in range(10):
        assert store.allow_request(digest("login-account:alice@example.test"), 60, 10)
    response = client.post(
        "/login", data={"csrf": csrf, "email": "ALICE@example.test", "password": "wrong"}
    )
    assert response.status_code == 429


def test_sensitive_browser_action_requires_recent_authentication(service):
    client, store = service
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
            ("alice", "alice@example.test", "unused"),
        )
    client.cookies.set(
        "session", store.credential("alice", "session", 3600, challenge=str(time.time() - 901))
    )
    client.get("/")
    response = client.post(
        "/account/action",
        data={"csrf": client.cookies.get("csrf"), "action": "delete_account", "confirm": "DELETE"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?reauth=1"
    with store.connection() as db:
        assert db.execute("SELECT id FROM accounts WHERE id='alice'").fetchone()
    assert client.get("/login?reauth=1").status_code == 200


def test_refresh_cannot_reset_time_of_authentication(service):
    client, store = service
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
            ("alice", "alice@example.test", "unused"),
        )
    old_time = str(time.time() - 901)
    refresh = store.credential("alice", "refresh", 3600, "varyz-local", challenge=old_time)
    response = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "varyz-local"},
    )
    assert response.status_code == 200
    tokens = response.json()
    assert store.lookup(tokens["access_token"], "access")["challenge"] == old_time
    response = client.delete(
        "/api/account", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert response.status_code == 403
