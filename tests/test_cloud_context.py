# ruff: noqa: F811
"""Combined consent, historical comparisons and server-side sharing controls."""

import pytest

from tests.test_cloud_archive import hosted, payload, push  # noqa: F401
from varyz.cloud.archive import CONTEXT_SCOPE, Archive, Change


def call(client, headers, name, args):
    return client.post(
        "/mcp/context",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )


def test_combined_read_consent_does_not_widen_existing_tokens(hosted):
    client, store, auth = hosted
    assert push(client, auth()).status_code == 200
    for headers in (auth(readonly=True), auth(client_id="legacy")):
        assert call(client, headers, "context", {}).status_code == 403
    token = store.credential("alice", "access", 3600, "chatgpt", scope=CONTEXT_SCOPE)
    headers = {"Authorization": "Bearer " + token}
    context = call(client, headers, "context", {"project": "varyz"}).json()["result"][
        "structuredContent"
    ]
    assert context["history"]["records"][0]["id"] == "one"
    assert context["preferences"] == []
    assert client.get("/api/export", headers=headers).status_code == 403
    assert push(client, headers).status_code == 403
    assert call(client, headers, "context", {"project": []}).json()["result"]["isError"]


def test_version_retention_export_and_revocation(hosted):
    client, store, auth = hosted
    archive = Archive(store, "http://localhost")
    for version in range(8):
        record = payload(text=f"Plan revision {version}: explicit decision.")
        archive.push("alice", [Change(id="one", base_version=version, payload=record)])
    comparison = archive.compare("alice", "one")
    assert comparison["original"]["text"].startswith("Plan revision 0")
    assert comparison["current"]["text"].startswith("Plan revision 7")
    original_url = comparison["original"]["url"]
    source = client.get(original_url, headers=auth(readonly=True))
    assert source.status_code == 200 and "Plan revision 0" in source.text
    assert client.get(original_url, headers=auth("bob", readonly=True)).status_code == 404
    assert (
        client.get("/archive/one?version=invalid", headers=auth(readonly=True)).status_code == 404
    )
    exported = client.get("/api/export", headers=auth()).json()["archive"]
    assert len(exported["revisions"]) == 5
    assert archive.revision_usage("alice") > 0
    assert archive.timeline("bob")["records"] == []
    archive.revoke("alice", "varyz")
    assert archive.export("alice")["records"] == []
    assert archive.export("alice")["revisions"] == []
    with pytest.raises(ValueError, match="revoked"):
        archive.push("alice", [Change(id="other", base_version=0, payload=payload("other"))])
    archive.permit("alice", "varyz")
    assert archive.push("alice", [Change(id="other", base_version=0, payload=payload("other"))])
    archive.control("alice", True)
    assert archive.timeline("alice")["paused"] is True
    assert len(archive.export("alice")["records"]) == 1


def test_browser_history_controls_csrf_and_escaped_projects(hosted):
    client, store, auth = hosted
    archive = Archive(store, "http://localhost")
    archive.push("alice", [Change(id="one", base_version=0, payload=payload(project="varyz"))])
    session = store.credential("alice", "session", 3600)
    client.cookies.set("session", session)
    assert client.post("/account/action", data={"action": "pause_history"}).status_code == 403
    page = client.get("/?view=dashboard")
    assert "AI access to project history" in page.text
    csrf = client.cookies.get("csrf")
    response = client.post(
        "/account/action",
        data={
            "action": "pause_history",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert archive.paused("alice")
    page = client.get("/?view=dashboard")
    response = client.post(
        "/account/action",
        data={
            "action": "revoke_project",
            "project": "varyz",
            "csrf": client.cookies.get("csrf"),
            "confirm_action": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert archive.export("alice")["records"] == []


def test_encrypted_revision_payloads_and_capacity_rollback(hosted):
    from cryptography.fernet import Fernet

    from varyz.cloud.store import QuotaExceeded

    _, store, _ = hosted
    archive = Archive(store, "http://localhost", encryption_key=Fernet.generate_key().decode())
    archive.push(
        "alice", [Change(id="one", base_version=0, payload=payload(text="Original private plan"))]
    )
    archive.push(
        "alice", [Change(id="one", base_version=1, payload=payload(text="Updated private plan"))]
    )
    with store.connection() as db:
        row = db.execute("SELECT payload FROM archive_revisions WHERE account='alice'").fetchone()
        assert "Original private plan" not in row["payload"]
    archive.total_quota = 1
    with pytest.raises(QuotaExceeded):
        archive.push("alice", [Change(id="two", base_version=0, payload=payload("two"))])
    assert len(archive.export("alice")["records"]) == 1
