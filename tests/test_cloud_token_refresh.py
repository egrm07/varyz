"""A computer stays connected when several Varyz processes renew one sign-in."""

import base64
import hashlib
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from varyz.cloud import client as cloud_client
from varyz.cloud.app import Settings, create_app, password_hash
from varyz.cloud.client import CloudClient

NAME = "cloud:" + "a" * 64


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    app = create_app(Settings(
        database=str(tmp_path / "cloud.sqlite"),
        paddle_secret="test-secret",
        paddle_prices=("pri_paid",),
        paddle_client_token="test_" + "a" * 24,
        email_sender=lambda message: None,
        signup_email_validator=lambda email: email,
    ))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
            ("alice", "alice@example.test", password_hash("correct-password"), "active"),
        )
    http = TestClient(app, base_url="http://127.0.0.1:8787")
    saved: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(cloud_client.keyring, "get_password", lambda s, n: saved.get((s, n)))
    monkeypatch.setattr(cloud_client.keyring, "set_password",
                        lambda s, n, v: saved.__setitem__((s, n), v))
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect = "http://127.0.0.1:8766/callback"
    code = store.credential("alice", "code", 300, "varyz-local", redirect, challenge)
    tokens = http.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": "varyz-local", "code": code,
        "redirect_uri": redirect, "code_verifier": verifier,
    }).json()
    cloud_client.keyring.set_password("varyz", NAME, json.dumps(tokens))
    return http, store


def process(http):
    """One Varyz process: it reads the saved sign-in once and keeps it in memory."""
    return CloudClient("http://127.0.0.1:8787", cloud_client.load_tokens(NAME),
                       credential=NAME, http=http)


def expire_access(store):
    with store.connection() as db:
        db.execute("UPDATE credentials SET expires=? WHERE kind='access'", (time.time() - 1,))


def connected(store):
    """What the Cloud page checks before it asks to connect this computer again."""
    with store.connection() as db:
        return db.execute(
            "SELECT 1 FROM credentials WHERE account='alice' AND client='varyz-local' "
            "AND kind IN ('access','refresh') AND expires>?", (time.time(),),
        ).fetchone() is not None


def test_long_running_process_adopts_a_renewal_made_elsewhere(cloud):
    http, store = cloud
    listener, watcher = process(http), process(http)
    for _ in range(3):  # several hours of hourly expiry, each side renewing in turn
        expire_access(store)
        assert watcher.request("GET", "/api/account")["id"] == "alice"
        assert listener.request("GET", "/api/account")["id"] == "alice"
        assert connected(store)
        expire_access(store)
        assert listener.request("GET", "/api/account")["id"] == "alice"
        assert watcher.request("GET", "/api/account")["id"] == "alice"
        assert connected(store)


def test_simultaneous_renewals_rotate_once(cloud, monkeypatch):
    http, store = cloud
    clients = [process(http) for _ in range(4)]
    expire_access(store)
    rotations = []
    rotate = CloudClient._rotate

    def slow_rotate(self):
        rotations.append(1)
        time.sleep(0.2)
        rotate(self)

    monkeypatch.setattr(CloudClient, "_rotate", slow_rotate)
    results: list[object] = []

    def call(c):
        try:
            results.append(c.request("GET", "/api/account")["id"])
        except Exception as error:  # surfaced by the assertion below
            results.append(error)

    threads = [threading.Thread(target=call, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["alice"] * 4
    assert len(rotations) == 1
    assert connected(store)
