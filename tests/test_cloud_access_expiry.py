"""Expiry must stop Cloud reads/writes and explain why on existing connections."""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from varyz.cloud.access import notice
from varyz.cloud.client import CloudAccessInactive, CloudClient, CloudError

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app


@pytest.fixture
def service(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite"),
                              paddle_secret="secret", paddle_prices=("pri_paid",)))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password) VALUES('owner','test@example.test','x')"
        )
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    local = {"Authorization": "Bearer " + store.credential("owner", "access", 3600, "varyz-local")}
    plugin = {"Authorization": "Bearer " + store.credential(
        "owner", "access", 3600, "chatgpt", scope="varyz.plugin")}
    return client, store, local, plugin


def stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def event(client, end, *, occurred=None, identifier="evt_1", **changes):
    data = {"id": "sub_owner", "status": "active",
            "custom_data": {"varyz_account_id": "owner"},
            "items": [{"price": {"id": "pri_paid"}}],
            "current_billing_period": {"ends_at": end} if end is not None else None, **changes}
    raw = json.dumps({"event_id": identifier, "event_type": "subscription.updated",
                      "occurred_at": occurred or stamp(time.time()), "data": data}).encode()
    now = str(int(time.time()))
    signature = hmac.new(b"secret", now.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post("/api/billing/webhook", content=raw,
                       headers={"Paddle-Signature": f"ts={now};h1={signature}"})


def call(client, headers, path="/mcp/plugin", name="start_task"):
    return client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 42,
        "method": "tools/call", "params": {"name": name, "arguments": {
            "project": "demo", "task": "Implement a feature"}}})


def test_paid_deadline_blocks_reads_writes_without_cancel_webhook_and_renews(service, monkeypatch):
    client, store, local, plugin = service
    now = int(time.time())
    assert event(client, stamp(now + 30), occurred=stamp(now - 10)).status_code == 200
    assert client.get("/api/account", headers=local).json()["entitlement"] is True
    monkeypatch.setattr("varyz.cloud.billing.time.time", lambda: now + 30)
    account = client.get("/api/account", headers=local).json()
    assert account["status"] == "expired"
    assert account["cloud_access"]["reason"] == "subscription_expired"
    assert client.get("/api/sync", headers=local).status_code == 402
    assert client.post("/api/sync", headers=local, json={"records": []}).status_code == 402
    assert client.get("/api/export", headers=local).status_code == 200
    for path, headers in [("/mcp/plugin", plugin), ("/mcp", local)]:
        reply = call(client, headers, path)
        assert reply.status_code == 200
        result = reply.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["cloud_access"]["reason"] == "subscription_expired"
        assert "run out" in result["content"][0]["text"]
        assert "_meta" not in result  # Billing never requests another OAuth connection.
    assert event(client, stamp(now + 86400), occurred=stamp(now + 31),
                 identifier="evt_renewed").status_code == 200
    assert client.get("/api/account", headers=local).json()["entitlement"] is True
    assert not call(client, plugin).json()["result"].get("isError")
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_scheduled_stop_uses_effective_date_and_can_be_removed(service, monkeypatch, action):
    client, _, local, _ = service
    now = int(time.time())
    change = {"action": action, "effective_at": stamp(now + 20)}
    assert event(client, stamp(now + 100), scheduled_change=change,
                 occurred=stamp(now - 10)).status_code == 200
    monkeypatch.setattr("varyz.cloud.billing.time.time", lambda: now + 19)
    assert client.get("/api/account", headers=local).json()["entitlement"] is True
    monkeypatch.setattr("varyz.cloud.billing.time.time", lambda: now + 20)
    assert client.get("/api/account", headers=local).json()["entitlement"] is False
    assert event(client, stamp(now + 100), scheduled_change=None,
                 occurred=stamp(now + 21), identifier="evt_remove").status_code == 200
    assert client.get("/api/account", headers=local).json()["entitlement"] is True


def test_expired_free_grant_and_paid_overlap(service, monkeypatch):
    client, store, local, plugin = service
    now = int(time.time())
    with store.connection() as db:
        db.execute("INSERT INTO cloud_grants(account,role,started_at,ends_at) VALUES(?,?,?,?)",
                   ("owner", "audience", now - 10, now + 20))
    assert client.get("/api/account", headers=local).json()["entitlement"] is True
    monkeypatch.setattr("varyz.cloud.billing.time.time", lambda: now + 20)
    result = call(client, plugin).json()["result"]
    assert result["structuredContent"]["cloud_access"]["reason"] == "free_period_expired"
    # Even a write tool gets the same notice before it can change account data.
    assert call(client, plugin, name="remember_owner_rule").json()["result"]["isError"]
    assert event(client, stamp(now + 100)).status_code == 200
    assert client.get("/api/account", headers=local).json()["entitlement"] is True


@pytest.mark.parametrize("status,reason", [("canceled", "subscription_canceled"),
    ("past_due", "payment_past_due"), ("paused", "subscription_paused")])
def test_plugin_reports_actual_billing_reason(service, status, reason):
    client, _, _, plugin = service
    assert event(client, None, status=status).status_code == 200
    reply = call(client, plugin).json()["result"]
    assert reply["isError"] and reply["structuredContent"]["cloud_access"]["reason"] == reason
    # Discovery remains available without data or another plugin submission.
    for method in ("initialize", "tools/list"):
        reply = client.post("/mcp/plugin", headers=plugin,
                            json={"jsonrpc": "2.0", "id": 1, "method": method})
        assert reply.status_code == 200 and "result" in reply.json()


@pytest.mark.parametrize("bad", ["bad", "2026-10-16T12:00:00", 7, {}])
def test_malformed_deadline_retries_and_stale_webhook_cannot_restore_access(service, bad):
    client, _, local, _ = service
    assert event(client, bad).status_code == 422
    assert event(client, stamp(time.time() - 1)).status_code == 200
    assert event(client, stamp(time.time() + 100), identifier="evt_stale",
                 occurred=stamp(time.time() - 100)).status_code == 200
    assert client.get("/api/account", headers=local).json()["entitlement"] is False


def test_local_client_safe_reason_and_no_auth_refresh_on_billing_failure():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(402, json={"detail": {
            "reason": "free_period_expired", "message": "private server text"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = CloudClient("https://cloud.example.test", {
            "access_token": "token", "refresh_token": "refresh"}, http=http)
        with pytest.raises(CloudAccessInactive, match="free Cloud period has run out") as caught:
            client.request("GET", "/api/sync")
    assert "private server text" not in str(caught.value)
    assert calls == ["/api/sync"]
    assert client.tokens["refresh_token"] == "refresh"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as http:
        client = CloudClient("https://cloud.example.test", {"access_token": "token"}, http=http)
        with pytest.raises(CloudError, match="permission") as caught:
            client.request("GET", "/api/sync")
        assert not isinstance(caught.value, CloudAccessInactive)


def test_local_notification_deduplicates_and_rearms_after_recovery(tmp_path, monkeypatch):
    from varyz import desktop_notify
    from varyz.cloud import local_access

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    journal = config.with_name("config.cloud-sync.json")
    journal.write_text(json.dumps({"url": "https://cloud.example.test", "account_id": "owner",
                                   "records": {}, "enabled": True}), encoding="utf-8")
    original = journal.read_bytes()
    shown = []
    active = False

    class Cloud:
        url = "https://cloud.example.test"

        def request(self, method, path, **kwargs):
            if path == "/api/account":
                return {"id": "owner", "cloud_access": {"active": True} if active
                        else notice("free_period_expired")}
            return {"notices": []}

    monkeypatch.setattr(desktop_notify, "show", lambda *a, **kw: shown.append(a) or True)
    monkeypatch.setattr(local_access.time, "time", lambda: 100)
    assert desktop_notify.check(config, client=Cloud()) == 1
    assert desktop_notify.check(config, client=Cloud()) == 0
    assert "free Cloud period has run out" in shown[0][1]
    # A confirmed notice survives outages; it does not erase data or disable selection.
    monkeypatch.setattr(local_access, "check", lambda *a, **kw: local_access.read(config))
    assert local_access.briefing_notice(config)["reason"] == "free_period_expired"
    local_access.observe(config, {"active": True})
    assert local_access.briefing_notice(config) is None
    local_access.observe(config, notice("free_period_expired"))
    assert desktop_notify.check(config, client=Cloud()) == 1
    assert journal.read_bytes() == original
    journal.write_text(json.dumps({"url": "https://cloud.example.test", "account_id": "other",
                                   "records": {}}), encoding="utf-8")
    assert local_access.read(config) == {}


@pytest.mark.parametrize("available", [True, False, "missing"])
def test_legacy_subscription_reads_provider_and_fails_closed_during_outage(
    service, monkeypatch, available
):
    from varyz.cloud import app as cloud_app

    client, store, local, plugin = service
    client.app.state.settings.paddle_api_key = "test-key"
    with store.connection() as db:
        db.execute("UPDATE accounts SET status='active',subscription='sub_owner'")
    calls = []

    def provider(url, key, missing=None):
        calls.append(url)
        if available == "missing":  # e.g. a sandbox subscription after going live
            return missing
        return {"id": "sub_owner", "status": "active", "current_billing_period": {
            "ends_at": stamp(time.time() - 1)}} if available else None

    monkeypatch.setattr(cloud_app, "paddle_read", provider)
    reply = client.get("/api/account", headers=local).json()
    assert reply["entitlement"] is False
    if available == "missing":
        # Not "try later": the provider answered, and there is no paid subscription.
        assert reply["cloud_access"]["reason"] == "subscription_required"
        assert client.get("/api/sync", headers=local).status_code == 402
        result = call(client, plugin).json()["result"]
        assert "no active subscription" in result["content"][0]["text"]
        assert "could not be verified" not in result["content"][0]["text"]
    elif available:
        assert reply["cloud_access"]["reason"] == "subscription_expired"
        assert client.get("/api/sync", headers=local).status_code == 402
        with store.connection() as db:
            assert db.execute("SELECT subscription_ends_at FROM accounts").fetchone()[0]
    else:
        assert reply["cloud_access"]["active"] is None
        assert client.get("/api/sync", headers=local).status_code == 503
        result = call(client, plugin).json()["result"]
        assert "could not be verified" in result["content"][0]["text"]
        assert "run out" not in result["content"][0]["text"]
    assert len(calls) == 1


def test_expiry_during_long_poll_is_rechecked_before_returning(service, monkeypatch):
    client, store, local, _ = service
    now = int(time.time())
    assert event(client, stamp(now + 20)).status_code == 200
    clock = [now]
    monkeypatch.setattr("varyz.cloud.billing.time.time", lambda: clock[0])
    original = store.pull
    pulls = []

    def first_pull(*args, **kwargs):
        pulls.append(True)
        clock[0] = now + 20
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "pull", first_pull)
    response = client.get("/api/sync?wait=1", headers=local)
    assert response.status_code == 402
    assert response.json()["detail"]["reason"] == "subscription_expired"
    assert len(pulls) == 1  # Rejected after waiting, before another data read.
