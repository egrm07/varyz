"""Direct computer consent, reauthentication and callback security boundaries."""

import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app, password_hash

BASE = "http://127.0.0.1:8787"
CHALLENGE = "c" * 43
PASSWORD = "correct horse battery"


@pytest.fixture
def cloud(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        for owner, status in (("alice", "active"), ("unpaid", "inactive")):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (owner, owner + "@example.test", password_hash(PASSWORD), status),
            )
    with TestClient(app, base_url=BASE) as client:
        yield client, store


def csrf_from(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def set_session(client, token):
    for cookie in list(client.cookies.jar):
        if cookie.name == "session":
            client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)
    client.cookies.set("session", token, domain="127.0.0.1", path="/")


def query():
    return {
        "client_id": "varyz-local",
        "redirect_uri": "http://127.0.0.1:8766/callback",
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": CHALLENGE,
        "state": "connection-state",
    }


def click(client, store, *, old=False, owner="alice"):
    set_session(
        client,
        store.credential(
            owner,
            "session",
            3600,
            challenge=str(time.time() - (901 if old else 0)),
        ),
    )
    page = client.get("/")
    return client.post(
        "/computer/connect", data={"csrf": csrf_from(page)}, headers={"Origin": BASE}
    )


def test_dashboard_click_connects_directly_once(cloud):
    client, store = cloud
    assert click(client, store).status_code == 200
    intent = client.cookies.get("computer_intent")
    response = client.get("/oauth/authorize", params=query(), follow_redirects=False)
    assert response.status_code == 303
    callback = urlsplit(response.headers["location"])
    assert callback.scheme == "http" and callback.netloc == "127.0.0.1:8766"
    values = parse_qs(callback.query)
    assert values["state"] == ["connection-state"]
    code = store.lookup(values["code"][0], "code")
    assert code["account"] == "alice" and code["challenge"] == CHALLENGE
    assert store.lookup(intent, "computer_intent") is None
    assert client.get("/oauth/authorize", params=query()).status_code == 200


def test_old_sign_in_connects_directly_without_another_login(cloud):
    client, store = cloud
    assert click(client, store, old=True).status_code == 200
    response = client.get("/oauth/authorize", params=query(), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("http://127.0.0.1:8766/callback?code=")


def test_expired_session_on_consent_page_redirects_instead_of_raw_json(cloud):
    client, store = cloud
    set_session(client, store.credential("alice", "session", 3600))
    page = client.get("/oauth/authorize", params=query())
    set_session(
        client,
        store.credential(
            "alice",
            "session",
            -1,
            challenge=str(time.time() - 901),
        ),
    )
    response = client.post(
        "/oauth/authorize",
        data={**query(), "csrf": csrf_from(page)},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert not store.lookup(client.cookies.get("computer_intent", ""), "computer_intent")


@pytest.mark.parametrize("case", ["csrf", "origin", "unpaid", "unsigned"])
def test_direct_consent_cannot_be_forged(cloud, case):
    client, store = cloud
    if case == "unsigned":
        assert client.post("/computer/connect", data={"csrf": "bad"}).status_code == 403
        return
    set_session(
        client,
        store.credential(
            "unpaid" if case == "unpaid" else "alice",
            "session",
            3600,
        ),
    )
    page = client.get("/")
    response = client.post(
        "/computer/connect",
        data={
            "csrf": "bad" if case == "csrf" else csrf_from(page),
        },
        headers={"Origin": "https://evil.test" if case == "origin" else BASE},
    )
    assert response.status_code == (402 if case == "unpaid" else 403)
    assert not client.cookies.get("computer_intent")


@pytest.mark.parametrize("case", ["other_account", "expired", "external_client", "bad_redirect"])
def test_intent_is_account_bound_expiring_and_local_only(cloud, case):
    client, store = cloud
    assert click(client, store).status_code == 200
    intent = client.cookies.get("computer_intent")
    params = query()
    if case == "other_account":
        set_session(client, store.credential("unpaid", "session", 3600))
    elif case == "expired":
        with store.connection() as db:
            db.execute("UPDATE credentials SET expires=0 WHERE kind='computer_intent'")
    elif case == "external_client":
        params["redirect_uri"] = "https://connector.test/callback"
        params["client_id"] = client.post(
            "/oauth/register",
            json={
                "redirect_uris": [params["redirect_uri"]],
            },
        ).json()["client_id"]
    else:
        params["redirect_uri"] = "https://evil.test/callback"
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == (400 if case == "bad_redirect" else 200)
    if case in {"other_account", "external_client", "bad_redirect"}:
        assert store.lookup(intent, "computer_intent") is not None
