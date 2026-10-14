"""Connection identity, refresh persistence and account-scoped controls."""

import json
import re
import time

from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app
from varyz.cloud.client import CloudClient
from varyz.cloud.connections import connection_groups


def test_group_registrations_without_claiming_devices():
    rows = [
        {"client_id": str(i), "authenticated_at": str(i), "redirects": json.dumps([uri])}
        for i, uri in enumerate(
            [
                "http://127.0.0.1:1001/callback",
                "http://localhost:1002/callback",
                "http://127.0.0.1:1003/callback",
                "https://chatgpt.com/a",
                "https://chatgpt.com/b",
            ]
        )
    ]
    groups = connection_groups(rows)
    assert [(g["label"], len(g["sessions"])) for g in groups] == [
        ("Desktop apps", 3),
        ("ChatGPT", 2),
    ]
    assert all(g["category"] == "apps" for g in groups)
    assert "not shared" in groups[1]["detail"]


def test_computer_name_refresh_rename_and_scoped_disconnect(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        for owner in ("alice", "bob"):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?, ?,1)",
                (owner, owner + "@example.test", "unused", "active"),
            )
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    stamp = str(time.time())
    access = store.credential("alice", "access", 3600, "varyz-local", challenge=stamp)
    refresh = store.credential("alice", "refresh", 3600, "varyz-local", challenge=stamp)
    other = store.credential("alice", "access", 3600, "varyz-local")
    bob = store.credential("bob", "access", 3600, "varyz-local", challenge=stamp)
    cloud = CloudClient("http://127.0.0.1:8787", {"access_token": access}, http=client)
    cloud.computer_header = json.dumps({"name": "Egor PC", "platform": "Windows"})
    cloud.request("GET", "/api/account")
    client.cookies.set("session", store.credential("alice", "session", 3600))
    page = client.get("/?view=dashboard").text
    assert "Egor PC" in page and "Windows" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    fields = {"csrf": csrf, "client_id": "varyz-local", "authenticated_at": stamp}
    assert (
        client.post(
            "/account/action",
            data={**fields, "action": "rename_connection", "connection_name": "My workstation"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    rotated = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "varyz-local"},
    ).json()
    info = json.loads(store.lookup(rotated["access_token"], "access")["redirect"])
    assert info == {"name": "Egor PC", "platform": "Windows", "alias": "My workstation"}
    assert (
        client.post(
            "/account/action",
            data={**fields, "action": "revoke_connection", "confirm_action": "1"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert store.lookup(rotated["access_token"], "access") is None
    assert store.lookup(rotated["refresh_token"], "refresh") is None
    assert store.lookup(other, "access") is not None
    assert store.lookup(bob, "access") is not None


def test_unknown_signins_group_without_claiming_one_device():
    groups = connection_groups(
        [
            {"client_id": "varyz-local", "authenticated_at": "1"},
            {"client_id": "varyz-local", "authenticated_at": "2"},
        ]
    )
    assert len(groups) == 1
    assert groups[0]["label"] == "Unidentified sign-ins"
    assert len(groups[0]["sessions"]) == 2
    assert "may belong" in groups[0]["detail"]


def test_refresh_and_access_are_one_authorization():
    row = {
        "client_id": "varyz-local",
        "authenticated_at": "1",
        "redirect": json.dumps({"name": "Workstation", "platform": "Windows"}),
    }
    assert len(connection_groups([row, row])[0]["sessions"]) == 1


def test_mixed_or_app_scheme_redirects_do_not_impersonate_chatgpt():
    for redirects in [
        ["https://chatgpt.com/callback", "https://other.test/callback"],
        ["untrusted://chatgpt.com/callback"],
        ["https://chatgpt.com.evil.test/callback"],
    ]:
        group = connection_groups([{"client_id": "app", "redirects": json.dumps(redirects)}])[0]
        assert group["label"] != "ChatGPT"
