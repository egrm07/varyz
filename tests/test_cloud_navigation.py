"""Website navigation reuses the Cloud browser session without exposing credentials."""

import time

import pytest
from fastapi.testclient import TestClient

from varyz.cloud.app import SESSION_TTL, Settings, create_app, password_hash

ORIGIN = "https://varyzai.com"
PUBLIC = "https://cloud.varyzai.com"


@pytest.fixture
def browser(tmp_path):
    settings = Settings(
        database=str(tmp_path / "cloud.sqlite"), public_url=PUBLIC,
        admin_emails=("owner@example.test",),
    )
    app = create_app(settings)
    with app.state.store.connection() as db:
        for name in ("owner", "member"):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (name, name + "@example.test", password_hash("test-password-123"), "inactive"),
            )
    return TestClient(app, base_url=PUBLIC), app.state.store


def session(client, store, owner="owner", ttl=SESSION_TTL):
    token = store.credential(owner, "session", ttl)
    client.cookies.set("__Host-session", token, domain="cloud.varyzai.com", path="/")
    return token


@pytest.mark.parametrize("owner,admin", [(None, False), ("member", False), ("owner", True)])
def test_website_sees_only_session_flags(browser, owner, admin):
    client, store = browser
    if owner:
        session(client, store, owner)
    response = client.get("/api/browser-session", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json() == {"authenticated": owner is not None, "admin": admin}
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert "Origin" in response.headers["vary"] and "Cookie" in response.headers["vary"]
    assert "example.test" not in response.text


@pytest.mark.parametrize("origin", ["https://evil.test", "https://varyzai.com.evil.test", "null"])
def test_session_discovery_rejects_untrusted_origins(browser, origin):
    client, store = browser
    session(client, store)
    response = client.get("/api/browser-session", headers={"Origin": origin})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_website_cannot_read_other_apis_or_post_account_actions(browser):
    client, store = browser
    session(client, store)
    for path in ("/", "/admin", "/api/account"):
        assert client.get(path, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/account/action", headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/browser-session", headers={"Origin": ORIGIN}).status_code == 403


def test_session_discovery_renews_host_only_cookie_and_logout_is_shared(browser):
    client, store = browser
    token = session(client, store, ttl=10 * 86400)
    response = client.get("/api/browser-session", headers={"Origin": ORIGIN})
    cookie = response.headers["set-cookie"]
    assert "__Host-session=" in cookie and "Secure" in cookie and "HttpOnly" in cookie
    assert "Domain=" not in cookie and f"Max-Age={SESSION_TTL}" in cookie
    assert store.lookup(token, "session")["expires"] > time.time() + 29 * 86400
    client.get("/")
    response = client.post("/account/action", data={
        "csrf": client.cookies.get("__Host-csrf"), "action": "logout",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert store.lookup(token, "session") is None
    assert client.get("/api/browser-session", headers={"Origin": ORIGIN}).json() == {
        "authenticated": False, "admin": False,
    }


def test_admin_login_returns_and_existing_session_does_not_prompt_again(browser):
    client, store = browser
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    client.get("/login")
    response = client.post("/login", data={
        "csrf": client.cookies.get("__Host-csrf"),
        "email": "owner@example.test", "password": "test-password-123",
    }, follow_redirects=False)
    assert response.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"
    client.cookies.set("__Host-return_to", "/admin", domain="cloud.varyzai.com", path="/")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/admin"


def test_external_login_return_is_never_followed(browser):
    client, store = browser
    session(client, store)
    client.cookies.set("__Host-return_to", "https://evil.test", domain="cloud.varyzai.com")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"


def test_dashboard_admin_link_and_wrong_account_recovery(browser):
    client, store = browser
    session(client, store)
    assert '<a href="/admin">Admin</a>' in client.get("/").text
    client.cookies.clear()
    session(client, store, "member")
    assert '<a href="/admin">Admin</a>' not in client.get("/").text
    response = client.get("/admin")
    assert response.status_code == 404
    assert 'href="/login?reauth=1"' in response.text
    assert "owner@example.test" not in response.text
