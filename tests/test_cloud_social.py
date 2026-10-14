"""Provider protocol fixtures, account-link boundaries and persistent browser login."""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from varyz.cloud.app import SESSION_TTL, Settings, create_app, password_hash


@pytest.fixture
def social(tmp_path):
    settings = Settings(
        database=str(tmp_path / "social.sqlite"),
        public_url="https://cloud.example.test",
        google_client_id="google-client",
        google_client_secret="google-secret",
        github_client_id="github-client",
        github_client_secret="github-secret",
        email_sender=lambda message: None,
    )
    app = create_app(settings)
    client = TestClient(app, base_url=settings.public_url)
    identity = {"subject": "g-123", "email": "social@example.test", "verified": True}
    requests = []

    def provider(request):
        requests.append(request)
        if request.url.path.endswith("/token") or request.url.path.endswith("/access_token"):
            body = parse_qs(request.content.decode())
            assert body["code_verifier"] and len(body["code_verifier"][0]) >= 43
            assert body["redirect_uri"][0].startswith(settings.public_url + "/auth/")
            return httpx.Response(200, json={"access_token": "provider-token-never-persist"})
        assert request.headers["Authorization"] == "Bearer provider-token-never-persist"
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(
                200,
                json={
                    "sub": identity["subject"],
                    "email": identity["email"],
                    "email_verified": identity["verified"],
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 123, "login": "mutable-name"})
        if request.url.path == "/user/emails":
            return httpx.Response(
                200,
                json=[
                    {"email": identity["email"], "primary": True, "verified": identity["verified"]}
                ],
            )
        raise AssertionError("Unexpected provider endpoint")

    app.state.social.http_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(provider)
    )
    return client, app.state.store, identity, requests


def start(client, provider="google"):
    response = client.get(f"/auth/{provider}/start", follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params
    return params["state"][0]


def callback(client, state, provider="google"):
    return client.get(
        f"/auth/{provider}/callback",
        params={"state": state, "code": "test-code"},
        follow_redirects=False,
    )


def complete(client, username="Social_Owner"):
    page = client.get("/auth/complete")
    assert page.status_code == 200 and "Choose your username" in page.text
    return client.post(
        "/auth/complete",
        data={"csrf": client.cookies.get("__Host-csrf"), "username": username},
        follow_redirects=False,
    )


@pytest.mark.parametrize("provider", ["google", "github"])
def test_social_signup_requires_chosen_username_and_reuses_stable_identity(social, provider):
    client, store, identity, requests = social
    state = start(client, provider)
    assert callback(client, state, provider).headers["location"] == "/auth/complete"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert complete(client, "ab").status_code == 422
    response = complete(client)
    assert response.status_code == 303
    assert f"Max-Age={SESSION_TTL}" in response.headers["set-cookie"]
    with store.connection() as db:
        owner = db.execute("SELECT * FROM accounts").fetchone()
        assert owner["username"] == "social_owner" and owner["email_verified"] == 1
        assert owner["status"] == "inactive"  # signing in is not payment
        assert db.execute("SELECT COUNT(*) FROM oauth_flows").fetchone()[0] == 0
        assert "provider-token" not in json.dumps(dict(owner))
    assert requests
    original = owner["id"]
    identity["email"] = "new-email@example.test"
    assert callback(client, start(client, provider), provider).status_code == 303
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
        assert db.execute("SELECT account FROM social_identities").fetchone()[0] == original


def test_state_is_browser_provider_bound_single_use_and_rejects_unicode(social):
    client, _, _, requests = social
    state = start(client)
    stranger = TestClient(client.app, base_url="https://cloud.example.test")
    assert callback(stranger, state).status_code == 400
    assert callback(client, state, "github").status_code == 400
    assert callback(client, "é" * 43).status_code == 400
    assert not requests
    assert callback(client, state).status_code == 303
    assert callback(client, state).status_code == 400


@pytest.mark.parametrize("provider", ["google", "github"])
def test_unverified_provider_email_cannot_create_account(social, provider):
    client, store, identity, _ = social
    identity["verified"] = False
    response = callback(client, start(client, provider), provider)
    assert response.status_code == 502
    assert "Could not verify" in response.text
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def seed(store, identifier="existing", email="social@example.test"):
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) VALUES(?,?,?,1)",
            (identifier, email, password_hash("correct-password")),
        )


def login(client, email="social@example.test"):
    client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf": client.cookies.get("__Host-csrf"),
            "email": email,
            "password": "correct-password",
        },
        follow_redirects=False,
    )


def test_existing_email_does_not_auto_link_and_authenticated_link_works(social):
    client, store, _, _ = social
    seed(store)
    assert callback(client, start(client)).status_code == 409
    assert client.post("/auth/google/link", data={"csrf": "missing"}).status_code == 403
    assert login(client).status_code == 303
    dashboard = client.get("/")
    policy = dashboard.headers["content-security-policy"]
    assert "form-action 'self' https://accounts.google.com https://github.com;" in policy
    linked = client.post(
        "/auth/google/link",
        data={"csrf": client.cookies.get("__Host-csrf")},
        follow_redirects=False,
    )
    assert linked.status_code == 303
    assert "https://accounts.google.com" in linked.headers["content-security-policy"]
    state = parse_qs(urlparse(linked.headers["location"]).query)["state"][0]
    assert callback(client, state).status_code == 303
    with store.connection() as db:
        assert db.execute("SELECT account FROM social_identities").fetchone()[0] == "existing"
    assert "Google connected" in client.get("/").text


def test_link_refuses_changed_or_revoked_browser_session(social):
    client, store, _, _ = social
    seed(store)
    assert login(client).status_code == 303
    client.get("/")
    response = client.post(
        "/auth/google/link",
        data={"csrf": client.cookies.get("__Host-csrf")},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    client.cookies.set(
        "__Host-session",
        store.credential("existing", "session", 60),
        domain="cloud.example.test",
        path="/",
    )
    assert callback(client, state).status_code == 403
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM social_identities").fetchone()[0] == 0


def test_taken_username_can_retry_and_signup_flow_cannot_replay(social):
    client, store, _, _ = social
    seed(store, "other", "other@example.test")
    with store.connection() as db:
        db.execute("UPDATE accounts SET username='social_owner' WHERE id='other'")
    assert callback(client, start(client)).status_code == 303
    assert complete(client).status_code == 409
    assert complete(client, "another_owner").status_code == 303
    assert (
        client.post(
            "/auth/complete", data={"csrf": client.cookies.get("__Host-csrf"), "username": "replay"}
        ).status_code
        == 400
    )


def test_persistent_session_survives_new_client_and_logout_revokes_it(social):
    client, store, _, _ = social
    seed(store)
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert f"Max-Age={SESSION_TTL}" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "SameSite=lax" in cookie and "correct-password" not in cookie
    saved = client.cookies.get("__Host-session")
    fresh = TestClient(client.app, base_url="https://cloud.example.test")
    fresh.cookies.set("__Host-session", saved)
    assert fresh.get("/login", follow_redirects=False).status_code == 303
    fresh.get("/")
    assert (
        fresh.post(
            "/account/action",
            data={"csrf": fresh.cookies.get("__Host-csrf"), "action": "logout"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert store.lookup(saved, "session") is None
    client.get("/login")
    response = login(client)
    with store.connection() as db:
        db.execute("UPDATE credentials SET expires=? WHERE kind='session'", (time.time() - 1,))
    assert client.get("/login", follow_redirects=False).status_code == 200


def test_provider_without_credentials_stays_unavailable_and_apple_is_absent(social):
    client, store, _, _ = social
    client.app.state.settings.google_client_secret = ""
    page = client.get("/login")
    assert 'type="button" disabled' not in page.text
    assert "Sign in with Google" not in page.text
    assert 'href="/auth/google/start"' not in page.text
    assert "coming soon" not in page.text
    assert "Sign in with GitHub" in page.text
    assert "Apple" not in page.text
    assert client.get("/auth/google/start").status_code == 503
    assert client.get("/auth/apple/start").status_code == 503
    seed(store)
    assert login(client).status_code == 303
    policy = client.get("/").headers["content-security-policy"]
    assert "form-action 'self' https://github.com;" in policy
    assert "accounts.google.com" not in policy


def test_expired_authorization_and_signup_cannot_create_an_account(social):
    client, store, _, requests = social
    state = start(client)
    with store.connection() as db:
        db.execute("UPDATE oauth_flows SET expires=?", (time.time() - 1,))
    assert callback(client, state).status_code == 400
    assert not requests
    assert callback(client, start(client)).status_code == 303
    client.get("/auth/complete")
    with store.connection() as db:
        db.execute("UPDATE oauth_flows SET expires=?", (time.time() - 1,))
    assert (
        client.post(
            "/auth/complete",
            data={"csrf": client.cookies.get("__Host-csrf"), "username": "late_owner"},
        ).status_code
        == 400
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_provider_failure_is_single_use_and_does_not_expose_secrets(social):
    client, store, _, _ = social

    def unavailable(request):
        return httpx.Response(503, text="google-secret internal upstream error")

    client.app.state.social.http_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable)
    )
    state = start(client)
    response = callback(client, state)
    assert response.status_code == 502
    assert "google-secret" not in response.text
    assert callback(client, state).status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
