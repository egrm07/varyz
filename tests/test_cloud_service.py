"""Security and consistency gates for the hosted service (no network)."""

import base64
import hashlib
import hmac
import json
import re
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app, password_hash


@pytest.fixture
def cloud(tmp_path):
    app = create_app(
        Settings(
            database=str(tmp_path / "cloud.sqlite"),
            paddle_secret="test-secret",
            paddle_prices=("pri_paid",),
            paddle_client_token="test_" + "a" * 24,
            email_sender=lambda message: None,
            signup_email_validator=lambda email: email,
        )
    )
    store = app.state.store
    with store.connection() as db:
        for owner, status in [("alice", "active"), ("bob", "active"), ("unpaid", "inactive")]:
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (owner, owner + "@example.test", password_hash("correct-password"), status),
            )
    client = TestClient(app, base_url="http://127.0.0.1:8787")

    def auth(owner):
        return {"Authorization": "Bearer " + store.credential(owner, "access", 3600, "varyz-local")}

    return client, store, auth


def item(identifier="one", version=0, payload=None):
    return {
        "kind": "rule",
        "id": identifier,
        "base_version": version,
        "payload": payload or {"statement": "Use plain words", "scope": "*"},
    }


def test_tenant_sync_and_unpaid_export(cloud):
    client, store, auth = cloud
    assert (
        client.post("/api/sync", headers=auth("alice"), json={"records": [item()]}).status_code
        == 200
    )
    assert client.get("/api/sync", headers=auth("bob")).json()["records"] == []
    assert client.get("/api/sync", headers=auth("unpaid")).status_code == 402
    assert client.get("/api/export", headers=auth("unpaid")).status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False


def test_conflict_rollback_retry_tombstone(cloud):
    client, store, auth = cloud
    headers = auth("alice")
    first = client.post("/api/sync", headers=headers, json={"records": [item()]}).json()
    retry = client.post("/api/sync", headers=headers, json={"records": [item()]}).json()
    assert retry == first
    conflict = item(payload={"statement": "Different", "scope": "*"})
    response = client.post(
        "/api/sync", headers=headers, json={"records": [item("second"), conflict]}
    )
    assert response.status_code == 409
    assert len(client.get("/api/sync", headers=headers).json()["records"]) == 1
    tombstone = {"kind": "rule", "id": "one", "base_version": 1, "payload": None}
    assert (
        client.post("/api/sync", headers=headers, json={"records": [tombstone]}).status_code == 200
    )
    assert client.get("/api/sync?cursor=1", headers=headers).json()["records"][0]["payload"] is None
    assert store.knowledge("alice", "project") == []


def test_payload_archive_rejection_and_limits(cloud):
    client, _, auth = cloud
    bad = item()
    bad["payload"]["archive_text"] = "secret"
    assert (
        client.post("/api/sync", headers=auth("alice"), json={"records": [bad]}).status_code == 422
    )
    assert client.post("/api/sync", headers=auth("alice"), content=b"x" * 262145).status_code == 413


def test_exact_scoped_answer_and_unknown(cloud):
    client, store, auth = cloud
    store.push(
        "alice",
        [
            {
                "kind": "answer",
                "id": "a",
                "base_version": 0,
                "payload": {"question": "Which font?", "answer": "Inter", "scope": "private"},
            }
        ],
    )

    def ask(project):
        response = client.post(
            "/mcp",
            headers=auth("alice"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ask",
                    "arguments": {"question": "Which font?", "project": project},
                },
            },
        )
        result = response.json()["result"]
        if result.get("isError"):
            return result
        return json.loads(result["content"][0]["text"])

    assert ask("private")["answer"] == "Inter"
    assert ask("another")["status"] == "ask_owner"
    assert client.post("/mcp", headers=auth("unpaid"), json={}).status_code == 402


def test_oauth_pkce_redirect_reuse_and_refresh(cloud):
    client, store, _ = cloud
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    redirect = "http://127.0.0.1:8766/callback"
    code = store.credential("alice", "code", 300, "varyz-local", redirect, challenge)
    form = {
        "grant_type": "authorization_code",
        "client_id": "varyz-local",
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": "w" * 64,
    }
    assert client.post("/oauth/token", data=form).status_code == 400
    form["code_verifier"] = verifier
    form["redirect_uri"] = redirect + "?evil=yes"
    assert client.post("/oauth/token", data=form).status_code == 400
    form["redirect_uri"] = redirect
    response = client.post("/oauth/token", data=form)
    assert response.status_code == 200
    assert client.post("/oauth/token", data=form).status_code == 400
    old = response.json()["refresh_token"]
    refresh = {"grant_type": "refresh_token", "client_id": "varyz-local", "refresh_token": old}
    assert client.post("/oauth/token", data=refresh).status_code == 200
    assert client.post("/oauth/token", data=refresh).status_code == 400
    assert store.lookup(old, "refresh") is None


def billing(
    client, status, event_id="evt_1", occurred="2026-10-02T12:00:00Z", secret="test-secret"
):
    event = {
        "event_id": event_id,
        "event_type": "subscription.updated",
        "occurred_at": occurred,
        "data": {
            "id": "sub_1",
            "status": status,
            "custom_data": {"varyz_account_id": "unpaid"},
            "items": [{"price": {"id": "pri_paid"}}],
        },
    }
    raw = json.dumps(event).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/billing/webhook", content=raw, headers={"Paddle-Signature": f"ts={ts};h1={sig}"}
    )


def test_billing_verified_only_no_trials_replay_or_stale(cloud):
    client, _, auth = cloud
    assert billing(client, "active", secret="attacker").status_code == 401
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False
    assert billing(client, "trialing").status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False
    assert billing(client, "active", "evt_2", "2026-10-02T12:01:00Z").status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True
    assert billing(client, "canceled", "evt_3", "2026-10-02T12:02:00Z").status_code == 200
    assert billing(client, "active", "evt_2", "2026-10-02T12:01:00Z").json()["duplicate"] is True
    assert billing(client, "active", "evt_4", "2026-10-02T12:01:30Z").status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False


def paddle_event(client, event_type, data, identifier, occurred="2026-10-02T12:02:00Z"):
    raw = json.dumps(
        {"event_id": identifier, "event_type": event_type, "occurred_at": occurred, "data": data}
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"test-secret", stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/billing/webhook",
        content=raw,
        headers={"Paddle-Signature": f"ts={stamp};h1={signature}"},
    )


def paid_transaction(identifier="txn_first", start="2026-10-02T12:00:00Z"):
    return {
        "id": identifier,
        "subscription_id": "sub_1",
        "status": "completed",
        "custom_data": {"varyz_account_id": "unpaid"},
        "items": [{"price": {"id": "pri_paid"}}],
        "details": {"totals": {"grand_total": "500"}},
        "billing_period": {"starts_at": start},
    }


@pytest.mark.parametrize("bad", [{"items": [None]}, {"custom_data": ["bad"]}, {"id": []}])
def test_malformed_subscription_can_retry_without_consuming_event(cloud, bad):
    client, store, _ = cloud
    data = {
        "id": "sub_1",
        "status": "active",
        "custom_data": {"varyz_account_id": "unpaid"},
        "items": [{"price": {"id": "pri_paid"}}],
    }
    assert (
        paddle_event(client, "subscription.updated", {**data, **bad}, "evt_bad").status_code == 422
    )
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM events WHERE id=?", ("evt_bad",)).fetchone()
    assert paddle_event(client, "subscription.updated", data, "evt_bad").status_code == 200


def test_subscription_without_custom_data_uses_existing_association(cloud):
    client, _, auth = cloud
    assert billing(client, "active").status_code == 200
    data = {
        "id": "sub_1",
        "status": "past_due",
        "custom_data": None,
        "items": [{"price": {"id": "pri_paid"}}],
    }
    assert paddle_event(client, "subscription.past_due", data, "evt_due").status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False


def full_refund(status="approved", transaction="txn_first"):
    return {
        "id": "adj_first",
        "transaction_id": transaction,
        "subscription_id": "sub_1",
        "action": "refund",
        "type": "full",
        "status": status,
    }


@pytest.mark.parametrize("refund_first", [True, False])
def test_full_refund_order_replay_and_later_renewal(cloud, refund_first):
    client, _, auth = cloud
    billing(client, "active")
    payment = ("transaction.completed", paid_transaction(), "evt_paid")
    refund = ("adjustment.created", full_refund(), "evt_refund")
    for arguments in [refund, payment] if refund_first else [payment, refund]:
        assert paddle_event(client, *arguments).status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False
    assert client.get("/api/sync", headers=auth("unpaid")).status_code == 402
    assert client.get("/api/export", headers=auth("unpaid")).status_code == 200
    # A later lifecycle notification must not regrant a refunded period.
    billing(client, "active", "evt_later_active", "2026-10-02T12:03:00Z")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False
    renewed = paid_transaction("txn_renewal", "2026-11-02T12:00:00Z")
    renewed["custom_data"] = None  # Renewals can use the known subscription association.
    assert paddle_event(client, "transaction.completed", renewed, "evt_renewed").status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True
    assert paddle_event(client, *refund).json()["duplicate"] is True
    # Even a separate notification about the older refund leaves the newer period paid.
    paddle_event(client, "adjustment.updated", full_refund(), "evt_old_refund")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True


@pytest.mark.parametrize("status", ["pending_approval", "rejected"])
def test_refund_only_ends_access_when_approved(cloud, status):
    client, _, auth = cloud
    billing(client, "active")
    paddle_event(client, "transaction.completed", paid_transaction(), "evt_paid")
    paddle_event(client, "adjustment.created", full_refund(status), "evt_pending")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True
    paddle_event(client, "adjustment.updated", full_refund(), "evt_approved")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False


def test_partial_refund_and_other_tenant_do_not_revoke(cloud):
    client, _, auth = cloud
    billing(client, "active")
    paddle_event(client, "transaction.completed", paid_transaction(), "evt_paid")
    partial = {**full_refund(), "type": "partial"}
    paddle_event(client, "adjustment.created", partial, "evt_partial")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True
    paddle_event(client, "adjustment.updated", full_refund(), "evt_full")
    assert client.get("/api/account", headers=auth("alice")).json()["entitlement"] is True


@pytest.mark.parametrize("refund_first", [True, False])
@pytest.mark.parametrize(
    "amount,currency,revoked", [("605", "USD", True), ("100", "USD", False), ("605", "EUR", False)]
)
def test_dashboard_item_refund_compares_actual_paid_total(
    cloud, refund_first, amount, currency, revoked
):
    client, _, auth = cloud
    billing(client, "active")
    payment = paid_transaction()
    payment["details"]["totals"]["grand_total"] = "605"
    payment["currency_code"] = "USD"
    refund = {
        **full_refund(),
        "type": "partial",
        "currency_code": currency,
        "totals": {"total": amount},
        "items": [{"type": "full"}],
    }
    events = [
        ("transaction.completed", payment, "evt_payment"),
        ("adjustment.updated", refund, "evt_refund"),
    ]
    for arguments in reversed(events) if refund_first else events:
        assert paddle_event(client, *arguments).status_code == 200
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is not revoked


def test_billing_timestamps_compare_instants_not_strings(cloud):
    client, _, auth = cloud
    billing(client, "active", "evt_active", "2026-10-02T12:00:00Z")
    billing(client, "canceled", "evt_cancel", "2026-10-02T14:01:00+02:00")
    billing(client, "active", "evt_stale", "2026-10-02T12:00:30Z")
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False


def test_cancel_keeps_paid_period_but_deletion_cancels_immediately(cloud, monkeypatch):
    client, store, auth = cloud
    billing(client, "active")
    client.app.state.settings.paddle_api_key = "test-only"
    requests = []

    async def post(self, url, **kwargs):
        requests.append(kwargs["json"])
        import httpx

        return httpx.Response(200, json={"data": {}}, request=httpx.Request("POST", url))

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    response = client.post("/api/billing/cancel", headers=auth("unpaid"))
    assert response.json()["effective_from"] == "next_billing_period"
    assert requests[-1] == {"effective_from": "next_billing_period"}
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is True
    paddle_event(client, "transaction.completed", paid_transaction(), "evt_paid")
    paddle_event(client, "adjustment.created", full_refund(), "evt_refund")
    assert client.delete("/api/account", headers=auth("unpaid")).status_code == 200
    assert requests[-1] == {"effective_from": "immediately"}
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM billing_payments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM billing_refunds").fetchone()[0] == 0


def test_signup_csrf_and_session_auth(cloud):
    client, store, auth = cloud
    mail = []
    client.app.state.settings.email_sender = mail.append
    assert (
        client.post(
            "/signup", data={"email": "new@example.test", "password": "correct-password"}
        ).status_code
        == 403
    )
    client.get("/signup")
    csrf = client.cookies.get("csrf")
    assert (
        client.post(
            "/signup",
            data={
                "csrf": csrf,
                "email": "new@example.test",
                "password": "correct-password",
                "username": "New_Owner",
            },
            follow_redirects=False,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/login",
            data={"csrf": csrf, "email": "new@example.test", "password": "incorrect-password"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/login",
            data={"csrf": csrf, "email": "new@example.test", "password": "correct-password"},
            follow_redirects=False,
        ).status_code
        == 401
    )
    link = re.search(r"http://[^\s]+", mail[0].get_content()).group()
    raw = link.split("token=")[1]
    client.get(link)
    csrf = client.cookies.get("csrf")
    assert (
        client.post(
            "/verify", data={"csrf": csrf, "token": raw}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/verify", data={"csrf": csrf, "token": raw}, follow_redirects=False
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/login",
            data={"csrf": csrf, "email": "new@example.test", "password": "correct-password"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert "new@example.test" in client.get("/").text
    assert "@new_owner" in client.get("/").text
    with store.connection() as db:
        owner = db.execute("SELECT id FROM accounts WHERE email='new@example.test'").fetchone()[0]
    assert client.get("/api/account", headers=auth(owner)).json()["username"] == "new_owner"
    assert client.get("/api/export", headers=auth(owner)).json()["username"] == "new_owner"


def test_brand_assets_load_under_csp_without_exposing_arbitrary_files(cloud):
    client, _, _ = cloud
    for route in ("/signup", "/login", "/recover"):
        response = client.get(route)
        assert response.status_code == 200
        assert "font-src 'self'" in response.headers["Content-Security-Policy"]
        assert 'href="/favicon.svg"' in response.text
        assert "cloud-brand" in response.text
    for filename in ("VaryzCardSerif-Regular.ttf", "VaryzCardSans-Regular.ttf"):
        font = client.get("/fonts/" + filename)
        assert font.status_code == 200
        assert font.headers["content-type"] == "font/ttf"
        assert len(font.content) > 10000
    assert client.get("/fonts/OFL-IBMPlexSans.txt").status_code == 404
    assert client.get("/fonts/%2e%2e%2fapp.py").status_code == 404
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize("username", ["", "ab", "a" * 31, "with space", "a.b", "a-b", "égor"])
def test_signup_rejects_invalid_username_without_account_or_email(cloud, username):
    client, store, _ = cloud
    mail = []
    client.app.state.settings.email_sender = mail.append
    page = client.get("/signup")
    assert 'name="username"' in page.text
    response = client.post(
        "/signup",
        data={
            "csrf": client.cookies.get("csrf"),
            "email": "invalid@example.test",
            "password": "correct-password",
            "username": username,
        },
    )
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert not mail
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM accounts WHERE email='invalid@example.test'"
        ).fetchone()


def test_signup_username_uniqueness_and_existing_email_privacy(cloud):
    client, store, _ = cloud
    mail = []
    client.app.state.settings.email_sender = mail.append

    def signup(email, username):
        client.get("/signup")
        return client.post(
            "/signup",
            data={
                "csrf": client.cookies.get("csrf"),
                "email": email,
                "password": "correct-password",
                "username": username,
            },
        )

    assert signup("first@example.test", "Egor_07").status_code == 200
    # The username becomes reserved only after mailbox ownership is confirmed.
    link = re.search(r"http://[^\s]+", mail[0].get_content()).group()
    client.get(link)
    assert (
        client.post(
            "/verify",
            data={"csrf": client.cookies.get("csrf"), "token": link.split("token=")[1]},
            follow_redirects=False,
        ).status_code
        == 303
    )
    duplicate = signup("second@example.test", "EGOR_07")
    assert duplicate.status_code == 409
    assert "already taken" in duplicate.text
    assert 'name="csrf"' in duplicate.text
    assert len(mail) == 1
    # Existing email response remains generic and cannot change its handle.
    assert signup("first@example.test", "Other_name").status_code == 200
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT username FROM accounts WHERE email=?", ("first@example.test",)
            ).fetchone()[0]
            == "egor_07"
        )
        assert not db.execute(
            "SELECT 1 FROM accounts WHERE email=?", ("second@example.test",)
        ).fetchone()


def test_delete_revokes_and_clears_all_tenant_records(cloud):
    client, store, auth = cloud
    headers = auth("unpaid")
    store.push("unpaid", [item()])
    assert client.delete("/api/account", headers=headers).status_code == 200
    assert client.get("/api/account", headers=headers).status_code == 401
    assert store.pull("unpaid", 0, 100)["records"] == []
    assert client.get("/api/account", headers=auth("bob")).status_code == 200


def test_pending_question_answer_batch_and_away(cloud):
    client, _, auth = cloud

    def call(owner, name, args):
        response = client.post(
            "/mcp",
            headers=auth(owner),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        if response.status_code != 200:
            return response
        result = response.json()["result"]
        if result.get("isError"):
            return result
        return json.loads(result["content"][0]["text"])

    call(
        "alice",
        "away",
        {"action": "on", "conversation_id": "task1", "owner_words": "Keep working while I am away"},
    )
    result = call(
        "alice",
        "ask",
        {"questions": ["Font?", "Colour?"], "project": "test", "conversation_id": "task1"},
    )
    assert all(r["status"] == "queued" for r in result["results"])
    assert call("bob", "owner_questions", {})["questions"] == []
    question_id = result["results"][0]["question_id"]
    assert call("bob", "answer_owner_question", {"question_id": question_id, "answer": "Inter"})[
        "isError"
    ]
    assert (
        call("alice", "answer_owner_question", {"question_id": question_id, "answer": "Inter"})[
            "status"
        ]
        == "recorded"
    )
    assert call("alice", "ask", {"question": "Font?", "project": "test"})["answer"] == "Inter"
    assert len(call("alice", "owner_questions", {})["questions"]) == 1


@pytest.mark.parametrize("away", [False, True])
def test_hosted_delegation_checks_answers_without_queuing_routine_unknowns(cloud, away):
    client, store, auth = cloud

    def call(name, args):
        response = client.post("/mcp", headers=auth("alice"), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        assert response.status_code == 200, response.text
        return json.loads(response.json()["result"]["content"][0]["text"])

    if away:
        call("away", {"action": "on", "conversation_id": "quick-questions",
                      "owner_words": "Keep working while I am away."})
    before = store.knowledge("alice", "test")
    batch = call("ask", {"project": "test", "conversation_id": "quick-questions",
                         "questions": [
        {"question": "Which test runner?", "decision_kind": "delegated"},
        {"question": "Which colour?", "decision_kind": "preference"},
        {"question": "Publish publicly?", "decision_kind": "permission"},
    ]})
    assert batch["results"][0]["status"] == "not_known"
    assert batch["results"][0]["agent_decides"]
    assert [result["status"] for result in batch["results"][1:]] == [
        "queued" if away else "ask_owner",
    ] * 2
    assert len(call("owner_questions", {})["questions"]) == 2
    assert store.knowledge("alice", "test") == before
    colour = batch["results"][1]
    call("answer_owner_question", {"question_id": colour["question_id"], "answer": "Teal"})
    known = call("ask", {"project": "test", "question": "Which colour?",
                         "decision_kind": "delegated"})
    assert known["answer"] == "Teal" and "agent_decides" not in known


@pytest.mark.parametrize("queue_full", [False, True])
def test_away_upload_blocker_preserves_unattended_work_until_return(cloud, monkeypatch, queue_full):
    from varyz.cloud.store import QuotaExceeded
    from varyz.resolution.away import AWAY_POLICY

    client, store, auth = cloud

    def call(name, args, owner="alice"):
        response = client.post("/mcp", headers=auth(owner), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    task = {"conversation_id": "overnight"}
    activated = call("away", {**task, "action": "on", "owner_words": "/away"})
    assert activated["away_mode"]["how"] == AWAY_POLICY
    for name in ("start_task", "finish_check", "owner_questions"):
        assert call(name, {**task, "project": "test"})["away_mode"]["how"] == AWAY_POLICY
    # Task and tenant isolation remain intact.
    assert not call("away", {"conversation_id": "other", "action": "status"})["away"]
    assert not call("away", {**task, "action": "status"}, "bob")["away"]

    if queue_full:
        def full(*args, **kwargs):
            raise QuotaExceeded("full")
        monkeypatch.setattr(store, "queue_question", full)
    request = {**task, "project": "test", "question": "May I upload this package?",
               "decision_kind": "permission"}
    blocked = call("ask", request)
    assert blocked["status"] == ("deferred_unstored" if queue_full else "queued")
    assert blocked["how"] == AWAY_POLICY
    assert "agent_decides" not in blocked and "answer" not in blocked
    if queue_full:
        assert blocked["question_id"] is None and "not stored" in blocked["reason"]
    assert call("away", {**task, "action": "status"})["away"]
    # A permission blocker cannot turn independent delegated work into an owner question.
    independent = call("ask", {**request, "question": "Which test runner?",
                               "decision_kind": "delegated"})
    assert independent["agent_decides"]
    call("away", {**task, "action": "off"})
    assert call("ask", request)["status"] == "ask_owner"


def test_hosted_question_schema_teaches_brief_choices_and_supports_old_batches(cloud):
    client, _, auth = cloud
    response = client.post("/mcp", headers=auth("alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    ask = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "ask")
    assert "one short plain-language sentence" in ask["description"]
    assert "permission questions must still name" in ask["description"]
    choices = ask["inputSchema"]["properties"]["questions"]["items"]["oneOf"]
    assert [choice["type"] for choice in choices] == ["string", "object"]


def test_browser_review_mutation_csrf_and_export(cloud):
    client, store, _ = cloud
    client.cookies.set("session", store.credential("alice", "session", 3600))
    record = store.push("alice", [item()])[0]
    home = client.get("/")
    assert "Save changes" not in home.text and "Download my account data" in home.text
    preferences = client.get("/account/preferences?project=*")
    assert "Save changes" in preferences.text
    token = client.cookies.get("csrf")
    fields = {
        "action": "delete_record",
        "confirm_action": "1",
        "kind": "rule",
        "id": record["id"],
        "base_version": record["version"],
    }
    assert client.post("/account/action", data=fields).status_code == 403
    fields["csrf"] = token
    assert client.post("/account/action", data=fields, follow_redirects=False).status_code == 303
    assert store.knowledge("alice", "test") == []
    assert (
        client.post("/account/action", data={"csrf": token, "action": "export"}).status_code == 200
    )


def test_payment_return_page_polls_billing_status_until_entitlement(cloud):
    client, store, _ = cloud
    assert client.get("/account/billing-status").status_code == 401
    client.cookies.set("session", store.credential("unpaid", "session", 3600))
    waiting = client.get("/?payment=received")
    assert "Waiting for payment confirmation" in waiting.text
    nonce = re.search(r'<script nonce="([^"]+)" src="/payment-wait.js"', waiting.text)[1]
    policy = waiting.headers["Content-Security-Policy"]
    assert f"script-src 'nonce-{nonce}'" in policy and "connect-src 'self'" in policy
    assert "/account/billing-status" in client.get("/payment-wait.js").text
    assert client.get("/account/billing-status").json() == {"active": False, "status": "inactive"}
    assert "payment-wait.js" not in client.get("/").text
    with store.connection() as db:
        db.execute("UPDATE accounts SET status='active' WHERE id='unpaid'")
    assert client.get("/account/billing-status").json()["active"] is True
    assert "payment-wait.js" not in client.get("/?payment=received").text


def test_dashboard_names_existing_connections_and_disconnects_the_selected_client(cloud):
    client, store, _ = cloud
    client.cookies.set("session", store.credential("alice", "session", 3600))
    registered = {}
    for name, host in [("Claude", "claude.ai"), ("ChatGPT", "chatgpt.com")]:
        identifier = client.post(
            "/oauth/register", json={"redirect_uris": [f"https://{host}/callback"]}
        ).json()["client_id"]
        registered[name] = identifier
        store.credential("alice", "refresh", 3600, identifier)
        store.credential("alice", "access", 3600, identifier)
    store.credential("alice", "access", 3600, "varyz-local")
    store.credential("bob", "access", 3600, "other-owner-client")
    store.credential("alice", "access", -1, "expired-client")
    home = client.get("/")
    assert home.status_code == 200
    assert home.text.count("<h4>Claude</h4>") == 1
    assert home.text.count("<h4>ChatGPT</h4>") == 1
    assert "Unidentified sign-ins" in home.text
    assert "Connected AI tool" not in home.text
    assert "other-owner-client" not in home.text and "expired-client" not in home.text
    response = client.post(
        "/account/action",
        data={
            "action": "revoke_connection",
            "client_id": registered["Claude"],
            "csrf": client.cookies.get("csrf"),
            "confirm_action": "1",
        },
    )
    assert response.status_code == 200
    assert "<h4>Claude</h4>" not in response.text
    assert "<h4>ChatGPT</h4>" in response.text


@pytest.mark.parametrize(
    ("redirects", "expected"),
    [
        (["https://claude.ai/api/mcp/auth_callback"], "Claude"),
        (["https://chat.openai.com/callback", "https://chatgpt.com/callback"], "ChatGPT"),
        (["https://claude.ai.evil.test/callback"], "claude.ai.evil.test"),
        (["https://evilclaude.ai/callback"], "evilclaude.ai"),
        (["https://chatgpt.com.evil.test/callback"], "chatgpt.com.evil.test"),
        (["https://claude.ai/callback", "https://other.test/callback"], "Claude, other.test"),
        (["https://tool.example.test/callback"], "tool.example.test"),
        (["com.example.client:/callback"], "com.example.client"),
    ],
)
def test_connection_names_use_destinations_instead_of_claimed_brands(redirects, expected):
    from varyz.cloud.app import connection_label

    assert connection_label("client-id", redirects) == expected


def test_multiline_answer_and_secret_rejection(cloud):
    client, _, auth = cloud
    payload = {
        "question": "Which steps?",
        "answer": "First line\n\n    code line\nLast line",
        "scope": "TEST",
    }
    record = {"kind": "answer", "id": "multiline", "base_version": 0, "payload": payload}
    response = client.post("/api/sync", headers=auth("alice"), json={"records": [record]})
    assert response.json()["records"][0]["payload"]["answer"] == payload["answer"]
    assert response.json()["records"][0]["payload"]["scope"] == "test"
    bad = item("credential", payload={"statement": "Use sk-ant-api03-" + ("A" * 90), "scope": "*"})
    assert (
        client.post("/api/sync", headers=auth("alice"), json={"records": [bad]}).status_code == 422
    )


def test_oauth_browser_continuation_and_all_device_logout(cloud):
    client, store, auth = cloud
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = {
        "response_type": "code",
        "client_id": "varyz-local",
        "redirect_uri": "http://127.0.0.1:8766/callback",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": "opaque-state",
    }
    client.get("/oauth/authorize", params=params)
    assert client.cookies.get("auth_flow")
    csrf = client.cookies.get("csrf")
    response = client.post(
        "/login",
        data={"csrf": csrf, "email": "alice@example.test", "password": "correct-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"].startswith(
        "/oauth/authorize?"
    )
    consent = client.get(response.headers["location"])
    assert "Allow connection" in consent.text
    params["csrf"] = client.cookies.get("csrf")
    response = client.post("/oauth/authorize", data=params, follow_redirects=False)
    assert response.headers["location"].startswith("http://127.0.0.1:8766/callback?code=")
    access_headers = auth("alice")
    client.get("/")
    assert (
        client.post(
            "/account/action",
            data={
                "csrf": client.cookies.get("csrf"),
                "action": "revoke_all",
                "confirm_action": "1",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.get("/api/account", headers=access_headers).status_code == 401


def test_password_reset_single_use_revokes_old_tokens(cloud):
    client, _, auth = cloud
    messages = []
    client.app.state.settings.email_sender = messages.append
    access_headers = auth("alice")
    client.get("/recover")
    csrf = client.cookies.get("csrf")
    response = client.post(
        "/recover", data={"csrf": csrf, "email": "alice@example.test", "kind": "reset"}
    )
    assert response.status_code == 200
    token = re.search(r"token=([^\s]+)", messages[-1].get_content()).group(1)
    client.get("/reset", params={"token": token})
    fields = {
        "csrf": client.cookies.get("csrf"),
        "token": token,
        "password": "replacement-password",
    }
    assert client.post("/reset", data=fields, follow_redirects=False).status_code == 303
    assert client.post("/reset", data=fields, follow_redirects=False).status_code == 400
    assert client.get("/api/account", headers=access_headers).status_code == 401
    assert (
        client.post(
            "/login",
            data={
                "csrf": fields["csrf"],
                "email": "alice@example.test",
                "password": "correct-password",
            },
            follow_redirects=False,
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/login",
            data={
                "csrf": fields["csrf"],
                "email": "alice@example.test",
                "password": "replacement-password",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_malformed_json_and_origin_host_validation(cloud):
    client, _, auth = cloud
    headers = auth("alice")
    assert client.post("/mcp", headers=headers, content="{broken").status_code == 400
    assert client.post("/mcp", headers=headers, json=[]).status_code == 400
    assert (
        client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/account", headers={**headers, "Origin": "https://attacker.test"}
        ).status_code
        == 403
    )
    assert (
        client.get("/api/account", headers={**headers, "Host": "attacker.test"}).status_code == 400
    )


def test_capacity_batch_rollback_and_retry_no_extra_usage(cloud):
    client, store, auth = cloud
    store.max_storage_bytes = 1000
    headers = auth("alice")
    assert (
        client.post(
            "/api/sync", headers=headers, json={"records": [item("first"), item("second")]}
        ).status_code
        == 507
    )
    assert store.pull("alice", 0, 100)["records"] == []
    assert (
        client.post("/api/sync", headers=headers, json={"records": [item("first")]}).status_code
        == 200
    )
    assert (
        client.post("/api/sync", headers=headers, json={"records": [item("first")]}).status_code
        == 200
    )
    assert (
        client.post("/api/sync", headers=headers, json={"records": [item("second")]}).status_code
        == 507
    )
    assert client.get("/api/export", headers=headers).status_code == 200


def test_checkout_reuses_reservation_and_rejects_trials(cloud, monkeypatch):
    import httpx

    client, store, auth = cloud
    client.app.state.settings.paddle_api_key = "offline-test-key"
    calls = []
    trial = [False]

    class Provider:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "trial_period": {"interval": "day", "frequency": 14} if trial[0] else None,
                        "unit_price": {"amount": "500"},
                        "billing_cycle": {"interval": "month", "frequency": 1},
                    }
                },
            )

        async def post(self, *args, **kwargs):
            calls.append(kwargs["json"])
            return httpx.Response(
                201,
                json={
                    "data": {"checkout": {"url": "http://127.0.0.1:8787/checkout?_ptxn=txn_probe"}}
                },
            )

    monkeypatch.setattr("varyz.cloud.app.httpx.AsyncClient", Provider)
    trial[0] = True
    assert (
        client.post(
            "/api/billing/checkout", headers=auth("unpaid"), json={"price_id": "pri_paid"}
        ).status_code
        == 503
    )
    assert calls == []
    trial[0] = False
    first = client.post(
        "/api/billing/checkout", headers=auth("unpaid"), json={"price_id": "pri_paid"}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/billing/checkout", headers=auth("unpaid"), json={"price_id": "pri_paid"}
    )
    assert second.json() == first.json() and len(calls) == 1
    assert calls[0]["checkout"]["url"] == "http://127.0.0.1:8787/checkout"
    assert "trial" not in json.dumps(calls)


def test_checkout_client_token_environment_and_script_isolation(cloud):
    client, _, auth = cloud
    settings = client.app.state.settings
    response = client.get("/checkout")
    assert response.status_code == 200
    assert 'successUrl:"http://127.0.0.1:8787/?payment=received#connect-varyz"' in response.text
    assert 'event.name === "checkout.completed"' in response.text
    assert (
        'window.location.replace("http://127.0.0.1:8787/?payment=received#connect-varyz")'
        in response.text
    )
    # Returning from checkout is navigation only, never proof of payment.
    assert client.get("/api/account", headers=auth("unpaid")).json()["entitlement"] is False
    assert 'Paddle.Environment.set("sandbox")' in response.text
    nonce = re.search(r'<script nonce="([^"]+)"', response.text)[1]
    assert f"'nonce-{nonce}'" in response.headers["Content-Security-Policy"]
    assert "script-src" not in client.get("/login").headers["Content-Security-Policy"]
    assert nonce not in client.get("/checkout").text
    settings.paddle_sandbox = False
    assert client.get("/checkout").status_code == 503  # Never mix sandbox tokens with live.
    settings.paddle_client_token = "live_" + "b" * 24
    live = client.get("/checkout")
    assert live.status_code == 200 and "Environment.set" not in live.text
    settings.paddle_client_token = ""
    settings.paddle_api_key = "offline-test-key"
    assert client.get("/checkout").status_code == 503
    assert (
        client.post(
            "/api/billing/checkout", headers=auth("unpaid"), json={"price_id": "pri_paid"}
        ).status_code
        == 503
    )


def test_checkout_ambiguous_provider_result_never_retries(cloud, monkeypatch):
    import httpx

    client, store, auth = cloud
    client.app.state.settings.paddle_api_key = "offline-test-key"
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "trial_period": None,
                        "unit_price": {"amount": "500"},
                        "billing_cycle": {"interval": "month"},
                    }
                },
            )

        async def post(self, *args, **kwargs):
            calls.append(1)
            raise httpx.ReadTimeout("No provider response")

    monkeypatch.setattr("varyz.cloud.app.httpx.AsyncClient", Provider)
    headers = auth("unpaid")
    assert (
        client.post(
            "/api/billing/checkout", headers=headers, json={"price_id": "pri_paid"}
        ).status_code
        == 502
    )
    assert (
        client.post(
            "/api/billing/checkout", headers=headers, json={"price_id": "pri_paid"}
        ).status_code
        == 409
    )
    assert len(calls) == 1


def test_enrollment_ceiling_preserves_recovery_and_login(cloud):
    client, _, _ = cloud
    client.app.state.settings.max_accounts = 3
    client.get("/signup")
    csrf = client.cookies.get("csrf")
    assert (
        client.post(
            "/signup",
            data={
                "csrf": csrf,
                "email": "overlimit@example.test",
                "password": "correct-password",
                "username": "overlimit",
            },
        ).status_code
        == 503
    )
    assert (
        client.post(
            "/login",
            data={"csrf": csrf, "email": "alice@example.test", "password": "correct-password"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    client.get("/recover")
    assert (
        client.post(
            "/recover",
            data={
                "csrf": client.cookies.get("csrf"),
                "email": "alice@example.test",
                "kind": "reset",
            },
        ).status_code
        == 200
    )


def test_pending_question_capacity_and_reuse(cloud):
    client, store, auth = cloud
    store.max_storage_bytes = 300
    headers = auth("alice")

    def ask(question):
        return client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ask",
                    "arguments": {"question": question, "project": "project"},
                },
            },
        )

    first = ask("First question?")
    assert first.status_code == 200
    assert ask("First question?").status_code == 200
    second = ask("Second question?")
    assert second.status_code == 200
    result = json.loads(second.json()["result"]["content"][0]["text"])
    assert result["status"] == "ask_owner" and result["question_id"] is None
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM questions WHERE account=?", ("alice",)).fetchone()[0]
            == 1
        )


def test_daily_email_delivery_limit_keeps_recovery_generic(cloud):
    client, _, _ = cloud
    messages = []
    client.app.state.settings.email_sender = messages.append
    client.app.state.settings.email_daily_limit = 1
    client.get("/recover")
    csrf = client.cookies.get("csrf")
    replies = []
    for email in ("alice@example.test", "bob@example.test", "unknown@example.test"):
        response = client.post("/recover", data={"csrf": csrf, "email": email, "kind": "reset"})
        assert response.status_code == 200
        replies.append(response.text)
    assert len(messages) == 1
    assert len(set(replies)) == 1


def test_connector_registration_ceiling(cloud):
    client, store, _ = cloud
    client.app.state.settings.max_registered_clients = 1
    response = client.post(
        "/oauth/register", json={"redirect_uris": ["https://connector.example.test/callback"]}
    )
    assert response.status_code == 503
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
