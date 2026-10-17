"""Creator Program: policy arithmetic, referral binding, commissions, payouts, admin access."""

import hashlib
import hmac
import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from varyz.cloud.creator_policy import (
    LedgerRow,
    commission_minor,
    grant_end,
    hold_until,
    payout_details,
    payout_hint,
    payout_preview,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app, password_hash

# Policy, ported from the program's policy.test.mjs.


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_commission_exact_half_up_and_refunds():
    assert commission_minor(10000) == 3000
    assert commission_minor(10000, 2500) == 2250
    assert commission_minor(10000, 10000) == 0
    assert commission_minor(10000, 12000) == 0
    assert commission_minor(0) == 0
    assert commission_minor(5) == 2
    assert commission_minor(10**20) == 3 * 10**19
    with pytest.raises(ValueError):
        commission_minor(10.5)
    with pytest.raises(ValueError):
        commission_minor(-1)


def test_hold_is_thirty_elapsed_days_in_utc():
    assert hold_until(at("2026-10-02T02:00:00+02:00")) == at("2026-11-01T00:00:00Z")
    with pytest.raises(ValueError):
        hold_until(datetime(2026, 10, 2))


def test_grants_use_calendar_months_across_leap_years():
    assert grant_end("creator", at("2026-01-31T12:00:00Z")) == at("2026-04-30T12:00:00Z")
    assert grant_end("audience", at("2028-01-31T12:00:00Z")) == at("2028-02-29T12:00:00Z")
    with pytest.raises(ValueError):
        grant_end("friend", at("2026-01-01T00:00:00Z"))


BASE = LedgerRow("txn_1", "creator", "USD", at("2026-10-01T00:00:00Z"), 10000)
AS_OF = at("2026-11-01T00:00:00Z")


def test_payout_boundary_and_holds():
    result = payout_preview(
        [
            BASE,
            replace(BASE, transaction_id="txn_2", completed_at=at("2026-10-02T00:00:01Z")),
            replace(BASE, transaction_id="txn_3", creator="other", disputed=True),
        ],
        AS_OF,
    )
    assert result["payouts"][0]["amount"] == 3000
    assert result["held"] == ["txn_2", "txn_3"]
    edge = replace(BASE, completed_at=at("2026-10-02T00:00:00Z"))
    assert payout_preview([edge], AS_OF)["payouts"][0]["amount"] == 3000


def test_open_adjustment_blocks_that_creator_and_currency():
    result = payout_preview(
        [
            BASE,
            replace(BASE, transaction_id="txn_dispute", disputed=True),
            replace(BASE, transaction_id="txn_eur", currency="EUR"),
        ],
        AS_OF,
    )
    assert result["blocked"] == [{"creator": "creator", "currency": "USD"}]
    assert [p["currency"] for p in result["payouts"]] == ["EUR"]


def test_refund_after_payout_becomes_debt_and_offsets():
    refunded = replace(BASE, approved_refund=10000, paid_commission=3000)
    renewal = replace(BASE, transaction_id="txn_renewal", net_revenue=20000)
    result = payout_preview([refunded, renewal], AS_OF)
    assert result["debts"][0]["amount"] == 3000
    assert result["payouts"][0]["amount"] == 3000
    assert result["payouts"][0]["lines"] == {"txn_1": -3000, "txn_renewal": 6000}


def test_paid_refunded_and_disputed_never_pay_again():
    assert payout_preview([replace(BASE, paid_commission=3000)], AS_OF)["payouts"] == []
    assert payout_preview([replace(BASE, approved_refund=10000)], AS_OF)["payouts"] == []
    assert payout_preview([replace(BASE, disputed=True)], AS_OF)["held"] == ["txn_1"]


def test_currencies_stay_separate_and_bad_input_fails_closed():
    rows = [BASE, replace(BASE, transaction_id="txn_2", currency="EUR")]
    assert len(payout_preview(rows, AS_OF)["payouts"]) == 2
    with pytest.raises(ValueError):
        payout_preview([BASE, BASE], AS_OF)
    with pytest.raises(ValueError):
        payout_preview([replace(BASE, currency="usd")], AS_OF)
    with pytest.raises(ValueError):
        payout_preview([BASE], at("2026-11-02T00:00:00Z"))


def test_payout_details_cover_each_method_and_never_cards():
    assert payout_details("paypal", {"address": "m@example.test"}) == {"email": "m@example.test"}
    assert payout_details("revolut", {"address": "@Maker_1"}) == {"revtag": "@maker_1"}
    iban = payout_details(
        "bank", {"holder": "Maker  Studio", "account": "nl91 abna 0417 1643 00"}
    )
    assert iban == {"holder": "Maker Studio", "account": "NL91ABNA0417164300",
                    "bank_code": "", "country": "NL"}
    assert payout_hint(iban) == "Maker Studio, account ending 4300"
    us = payout_details("bank", {"holder": "Maker", "account": "000123456789",
                                 "bank_code": "021000021", "country": "us"})
    assert us["country"] == "US" and us["bank_code"] == "021000021"
    for method, fields in [
        ("paypal", {"address": "not-an-email"}),
        ("bank", {"holder": "Maker", "account": "NL91ABNA0417164301"}),
        ("bank", {"holder": "Maker", "account": "000123456789", "country": "US"}),
        ("bank", {"holder": "Maker", "account": "000123456789", "bank_code": "12",
                  "country": "US"}),
        ("card", {"address": "4111111111111111"}),
    ]:
        with pytest.raises(ValueError):
            payout_details(method, fields)


# The program running inside Varyz Cloud.


@pytest.fixture
def cloud(tmp_path):
    return build_cloud(tmp_path)


def build_cloud(tmp_path, **extra):
    mail = []
    app = create_app(
        Settings(
            **extra,
            database=str(tmp_path / "cloud.sqlite"),
            paddle_secret="test-secret",
            paddle_prices=("pri_paid",),
            paddle_client_token="test_" + "a" * 24,
            email_sender=mail.append,
            signup_email_validator=lambda email: email,
            admin_emails=("owner@example.test",),
            creators_open=True,
        )
    )
    store = app.state.store
    with store.connection() as db:
        for owner, username in [("owner", "owner"), ("maker", "maker"), ("buyer", "buyer")]:
            db.execute(
                "INSERT INTO accounts(id,email,password,username,email_verified) "
                "VALUES(?,?,?,?,1)",
                (owner, owner + "@example.test", password_hash("correct-password"), username),
            )
    return TestClient(app, base_url="http://127.0.0.1:8787"), store, mail


def browser(client, store, owner):
    client.cookies.clear()  # Session renewal stores a second, host-scoped cookie.
    client.cookies.set("session", store.credential(owner, "session", 3600))
    client.cookies.set("csrf", "c" * 43)
    return "c" * 43


def approve_maker(client, store):
    token = browser(client, store, "maker")
    response = client.post("/creators/action", data={
        "csrf": token, "action": "apply", "name": "Maker Studio",
        "channel": "https://video.example.test/maker", "audience": "Developers",
        "plan": "A tutorial video", "terms": "yes",
    }, follow_redirects=False)
    assert response.status_code == 303
    browser(client, store, "owner")
    response = client.post("/admin/action", data={
        "csrf": token, "action": "approve", "account": "maker",
    }, follow_redirects=False)
    assert response.status_code == 303
    client.cookies.clear()


def paddle(client, event_type, data, identifier, occurred=None):
    raw = json.dumps({
        "event_id": identifier, "event_type": event_type,
        "occurred_at": occurred or datetime.now(UTC).isoformat(), "data": data,
    }).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"test-secret", stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post("/api/billing/webhook", content=raw,
                       headers={"Paddle-Signature": f"ts={stamp};h1={signature}"})


def purchase(identifier, owner="buyer", net=1000, tax=210):
    return {
        "id": identifier, "subscription_id": "sub_" + owner, "status": "completed",
        "currency_code": "EUR", "custom_data": {"varyz_account_id": owner},
        "items": [{"price": {"id": "pri_paid"}}],
        "details": {
            "totals": {"subtotal": str(net), "discount": "0", "tax": str(tax),
                       "grand_total": str(net + tax)},
            "line_items": [{"price_id": "pri_paid", "totals": {
                "subtotal": str(net), "discount": "0", "tax": str(tax)}}],
        },
        "billing_period": {"starts_at": datetime.now(UTC).isoformat()},
    }


def test_admin_is_owner_only(cloud):
    client, store, _ = cloud
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    browser(client, store, "buyer")
    assert client.get("/admin").status_code == 404
    token = browser(client, store, "buyer")
    assert client.post("/admin/action", data={
        "csrf": token, "action": "applications", "value": "closed"}).status_code == 404
    browser(client, store, "owner")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Varyz admin" in page.text and "Paying subscribers" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "<script" not in page.text.split("</header>", 1)[-1]


def test_creator_notifications_use_separate_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("VARYZ_CLOUD_CREATOR_NOTIFICATION_EMAIL", " Egor@varyzai.com ")
    recipient = Settings.from_env().creator_notification_email
    assert recipient == "egor@varyzai.com"
    client, store, mail = build_cloud(tmp_path, creator_notification_email=recipient)
    approve_maker(client, store)
    applications = [message for message in mail
                    if message["Subject"] == "New Varyz creator application"]
    assert len(applications) == 1
    assert applications[0]["To"] == recipient
    browser(client, store, "owner")
    assert client.get("/admin").status_code == 200


def test_creator_code_changes_preserve_old_links_and_pending_attribution(cloud):
    from varyz.cloud.creators import account_created, invite_creator, remember_signup_referral

    client, store, _ = cloud
    approve_maker(client, store)
    token = browser(client, store, "maker")
    with store.connection() as db:
        remember_signup_referral(db, "pending@example.test", "maker")
        invite_creator(db, "invited@example.test", "reserved")
    for code, status in [("bad-code", 422), ("reserved", 409), ("new_maker", 303)]:
        assert client.post("/creators/action", data={
            "csrf": token, "action": "code", "code": code,
        }, follow_redirects=False).status_code == status
    for code in ("maker", "new_maker"):
        assert "Invited by Maker Studio" in client.get("/r/" + code).text
    with store.connection() as db:
        account_created(db, "buyer", email="pending@example.test")
        assert db.execute("SELECT creator FROM creator_referrals WHERE account='buyer'").fetchone()[
            "creator"] == "maker"
        with pytest.raises(Exception, match="already assigned"):
            invite_creator(db, "other@example.test", "maker")
    # Old aliases cannot be claimed by another approved creator.
    with store.connection() as db:
        db.execute("INSERT INTO creator_profiles(account,code,status,name) "
                   "VALUES('buyer','buyer','approved','Other')")
    token = browser(client, store, "buyer")
    assert client.post("/creators/action", data={
        "csrf": token, "action": "code", "code": "maker",
    }).status_code == 409
    token = browser(client, store, "maker")
    assert client.post("/creators/action", data={
        "csrf": token, "action": "code", "code": "maker",
    }, follow_redirects=False).status_code == 303
    assert "Invited by Maker Studio" in client.get("/r/new_maker").text


def test_password_signup_through_link_binds_referral_and_free_month(cloud):
    client, store, mail = cloud
    approve_maker(client, store)
    assert "maker" in mail[-1]["To"] and "Creator Program" in mail[-1]["Subject"]
    invite = client.get("/r/maker")
    assert "Invited by Maker Studio" in invite.text
    client.get("/r/maker")  # A repeat visit in the same browser counts once.
    assert client.get("/r/nobody", follow_redirects=False).headers["location"] == "/signup"
    client.get("/signup")
    csrf = client.cookies.get("csrf")
    client.post("/signup", data={
        "csrf": csrf, "email": "fan@example.test", "password": "correct-password",
        "username": "fan",
    })
    raw = re.search(r"token=(\S+)", mail[-1].get_content()).group(1)
    client.cookies.clear()  # The email link may open in another browser.
    client.get("/verify?token=" + raw)
    csrf = client.cookies.get("csrf")
    assert client.post("/verify", data={"csrf": csrf, "token": raw},
                       follow_redirects=False).status_code == 303
    with store.connection() as db:
        fan = db.execute("SELECT id FROM accounts WHERE email='fan@example.test'").fetchone()[0]
        referral = db.execute("SELECT creator FROM creator_referrals WHERE account=?",
                              (fan,)).fetchone()
        grant = db.execute("SELECT role,ends_at FROM cloud_grants WHERE account=?",
                           (fan,)).fetchone()
        clicks = db.execute("SELECT SUM(clicks) FROM creator_clicks").fetchone()[0]
    assert referral["creator"] == "maker" and grant["role"] == "audience"
    assert 28 * 86400 <= grant["ends_at"] - time.time() <= 31 * 86400
    assert clicks == 1
    access = {"Authorization": "Bearer " + store.credential(fan, "access", 3600, "varyz-local")}
    assert client.get("/api/account", headers=access).json()["entitlement"] is True
    browser(client, store, fan)
    dashboard = client.get("/").text
    assert "Free Cloud until" in dashboard and 'value="checkout"' in dashboard


def test_creator_activation_once_and_commission_ledger(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    token = browser(client, store, "maker")
    portal = client.get("/creators").text
    assert 'id="cp-referral-prefix">http://127.0.0.1:8787/r/</span>' in portal
    assert 'id="cp-code-edit" name="code" value="maker"' in portal
    assert client.post("/creators/action", data={"csrf": token, "action": "activate"},
                       follow_redirects=False).status_code == 303
    assert client.post("/creators/action", data={"csrf": token, "action": "activate"}
                       ).status_code == 409
    with store.connection() as db:
        db.execute("INSERT INTO creator_referrals VALUES('buyer','maker',?)", (time.time(),))
    assert paddle(client, "transaction.completed", purchase("txn_a"), "evt_a").status_code == 200
    assert paddle(client, "transaction.completed", purchase("txn_a"), "evt_a2").status_code == 200
    with store.connection() as db:
        commission = db.execute("SELECT * FROM creator_commissions").fetchall()
        revenue = db.execute("SELECT net,tax FROM billing_revenue").fetchone()
    assert [(c["transaction_id"], c["net"]) for c in commission] == [("txn_a", 1000)]
    assert (revenue["net"], revenue["tax"]) == (1000, 210)
    portal = client.get("/creators").text
    assert "3.00 EUR" in portal and "On hold until" in portal
    paddle(client, "adjustment.created", {
        "id": "adj_1", "transaction_id": "txn_a", "action": "refund", "type": "partial",
        "status": "pending_approval", "currency_code": "EUR",
        "totals": {"subtotal": "500", "tax": "105", "total": "605"},
    }, "evt_r1")
    assert "In review" in client.get("/creators").text
    paddle(client, "adjustment.updated", {
        "id": "adj_1", "transaction_id": "txn_a", "action": "refund", "type": "partial",
        "status": "approved", "currency_code": "EUR",
        "totals": {"subtotal": "500", "tax": "105", "total": "605"},
    }, "evt_r2")
    assert "1.50 EUR" in client.get("/creators").text


def test_earnings_total_is_net_separates_currencies_and_lists_every_payment(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    browser(client, store, "maker")
    empty = client.get("/creators").text
    assert '<span>Total earned</span><strong>0</strong>' in empty
    with store.connection() as db:
        for index in range(27):
            db.execute(
                "INSERT INTO creator_commissions VALUES(?,?,?,?,?,?)",
                (f"txn_total_{index}", "maker", "buyer", "USD" if index == 26 else "EUR",
                 time.time() - 40 * 86400, 1000),
            )
        db.execute("INSERT INTO creator_payout_lines VALUES('test_payout','txn_total_0',300)")
        db.execute(
            "INSERT INTO billing_adjustments(id,transaction_id,action,status,amount,currency,"
            "updated) VALUES('refund_total','txn_total_0','refund','approved',1000,'EUR',?)",
            (time.time(),),
        )
    portal = client.get("/creators").text
    total = re.search(r'<div class="cp-earnings-total">(.*?)</div>', portal).group(1)
    assert re.findall(r'<strong>(.*?)</strong>', total) == ["75.00 EUR", "3.00 USD"]
    assert '<details class="cp-breakdown"><summary>View breakdown</summary>' in portal
    assert "All 27 commission payments" in portal
    assert len(re.findall(r'class="cp-payment-id"', portal)) == 27
    assert "txn_total_0" in portal and "Refunded" in portal


def test_payout_records_once_after_hold_and_needs_recent_sign_in(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    token = browser(client, store, "maker")
    client.post("/creators/action", data={
        "csrf": token, "action": "payout", "method": "wise", "address": "maker@example.test"})
    with store.connection() as db:
        db.execute("INSERT INTO creator_referrals VALUES('buyer','maker',?)", (time.time(),))
    paddle(client, "transaction.completed", purchase("txn_old"), "evt_old")
    now = datetime.now(UTC)
    cutoff = datetime(now.year, now.month, 1, tzinfo=UTC)
    with store.connection() as db:
        db.execute("UPDATE creator_commissions SET completed_at=?",
                   ((cutoff - timedelta(days=45)).timestamp(),))
    browser(client, store, "owner")
    page = client.get("/admin").text
    assert "3.00 EUR" in page and "maker@example.test" in page
    payout = {"csrf": token, "action": "payout", "creator": "maker", "currency": "EUR",
              "amount": "300", "as_of": str(int(cutoff.timestamp())), "reference": "WISE-1"}
    assert client.post("/admin/action", data={**payout, "amount": "299"}).status_code == 409
    with store.connection() as db:
        db.execute("UPDATE credentials SET challenge='0' WHERE kind='session'")
    assert client.post("/admin/action", data=payout).status_code == 403
    browser(client, store, "owner")
    assert client.post("/admin/action", data=payout, follow_redirects=False).status_code == 303
    assert client.post("/admin/action", data=payout).status_code == 409
    browser(client, store, "maker")
    assert "Paid" in client.get("/creators").text


def test_deleting_an_account_keeps_an_anonymous_ledger(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    with store.connection() as db:
        db.execute("INSERT INTO creator_referrals VALUES('buyer','maker',?)", (time.time(),))
    paddle(client, "transaction.completed", purchase("txn_d"), "evt_d")
    from varyz.cloud.creators import forget_account

    with store.connection() as db:
        forget_account(db, "buyer")
        forget_account(db, "maker")
        assert db.execute("SELECT account FROM creator_commissions").fetchone()[0] is None
        assert not db.execute("SELECT 1 FROM creator_referrals").fetchone()
        profile = db.execute("SELECT status,name,payout_address FROM creator_profiles").fetchone()
    assert tuple(profile) == ("closed", "", "")


def test_bank_payout_details_are_sealed_and_shown_in_full_only_on_payouts_due(tmp_path):
    from cryptography.fernet import Fernet

    client, store, _ = build_cloud(tmp_path, archive_encryption_key=Fernet.generate_key().decode())
    approve_maker(client, store)
    token = browser(client, store, "maker")
    response = client.post("/creators/action", data={
        "csrf": token, "action": "payout", "method": "bank", "holder": "Maker Studio",
        "account": "NL91 ABNA 0417 1643 00"}, follow_redirects=False)
    assert response.status_code == 303
    with store.connection() as db:
        stored = db.execute(
            "SELECT payout_address FROM creator_profiles WHERE account='maker'"
        ).fetchone()[0]
    assert stored.startswith("sealed:") and "0417164300" not in stored
    portal = client.get("/creators").text
    assert "ending 4300" in portal and "NL91ABNA0417164300" not in portal
    with store.connection() as db:
        db.execute("INSERT INTO creator_referrals VALUES('buyer','maker',?)", (time.time(),))
    paddle(client, "transaction.completed", purchase("txn_bank"), "evt_bank")
    now = datetime.now(UTC)
    with store.connection() as db:
        db.execute("UPDATE creator_commissions SET completed_at=?", (
            (datetime(now.year, now.month, 1, tzinfo=UTC) - timedelta(days=45)).timestamp(),))
    browser(client, store, "owner")
    admin = client.get("/admin").text
    assert "Bank transfer" in admin and "NL91ABNA0417164300" in admin
    assert "Maker Studio" in admin


def test_bank_details_need_the_encryption_key_and_cards_are_refused(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    token = browser(client, store, "maker")
    bank = {"csrf": token, "action": "payout", "method": "bank", "holder": "Maker Studio",
            "account": "NL91ABNA0417164300"}
    assert client.post("/creators/action", data=bank).status_code == 503
    card = {"csrf": token, "action": "payout", "method": "card", "address": "4111111111111111"}
    assert client.post("/creators/action", data=card).status_code == 422
    with store.connection() as db:
        assert not db.execute(
            "SELECT payout_address FROM creator_profiles WHERE account='maker'"
        ).fetchone()[0]


def test_admin_account_pages_list_signups_for_the_owner_only(cloud):
    client, store, _ = cloud
    approve_maker(client, store)
    with store.connection() as db:
        db.execute("INSERT INTO account_created VALUES('buyer',?)", (time.time(),))
        db.execute("INSERT INTO creator_referrals VALUES('buyer','maker',?)", (time.time(),))
    for path in ("/admin/accounts", "/admin/accounts/buyer", "/admin/accounts.csv"):
        browser(client, store, "buyer")
        assert client.get(path).status_code == 404
    browser(client, store, "owner")
    assert 'href="/admin/accounts?view=week"' in client.get("/admin").text
    week = client.get("/admin/accounts?view=week").text
    assert "buyer@example.test" in week and "maker@example.test" not in week
    everyone = client.get("/admin/accounts").text
    assert all(f"{name}@example.test" in everyone for name in ("owner", "maker", "buyer"))
    found = client.get("/admin/accounts", params={"q": "MAK"}).text
    assert "maker@example.test" in found and "buyer@example.test" not in found
    referred = client.get("/admin/accounts", params={"creator": "maker"}).text
    assert "buyer@example.test" in referred and "Maker Studio" in referred
    detail = client.get("/admin/accounts/buyer")
    assert detail.headers["cache-control"] == "no-store" and "Maker Studio" in detail.text
    assert "Referred sign-ups (1)" in client.get("/admin/accounts/maker").text
    assert client.get("/admin/accounts/nobody").status_code == 404
    export = client.get("/admin/accounts.csv?view=referred")
    assert export.headers["cache-control"] == "no-store"
    assert export.text.splitlines()[1].startswith("buyer@example.test")


def test_invited_creator_joins_without_review_when_applications_closed(cloud):
    client, store, mail = cloud
    token = browser(client, store, 'owner')
    response = client.post('/admin/action', data={
        'csrf': token, 'action': 'invite_creator', 'email': 'MAKER@example.test',
        'code': 'maker_invited', 'name': 'Maker Studio',
    }, follow_redirects=False)
    assert response.status_code == 303
    assert not mail
    client.post('/admin/action', data={'csrf': token, 'action': 'applications', 'value': 'closed'})
    token = browser(client, store, 'maker')
    portal = client.get('/creators').text
    assert 'maker_invited' in portal and 'accept_invitation' in portal
    assert 'Send application' not in portal
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation'}, follow_redirects=False).status_code == 422
    response = client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'}, follow_redirects=False)
    assert response.status_code == 303
    assert 'id="cp-code-edit" name="code" value="maker_invited"' in client.get('/creators').text
    with store.connection() as db:
        profile = db.execute("SELECT * FROM creator_profiles WHERE account='maker'").fetchone()
        invite = db.execute("SELECT * FROM creator_invitations").fetchone()
        assert profile['status'] == 'approved' and profile['terms_version']
        assert invite['account'] == 'maker' and invite['accepted_at']
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'activate'}, follow_redirects=False).status_code == 303
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
        follow_redirects=False).status_code == 403


def test_invitation_requires_verified_matching_email_and_admin(cloud):
    client, store, _ = cloud
    token = browser(client, store, 'buyer')
    assert client.post('/admin/action', data={
        'csrf': token, 'action': 'invite_creator', 'email': 'buyer@example.test',
        'code': 'stolen'}, follow_redirects=False).status_code == 404
    token = browser(client, store, 'owner')
    assert client.post('/admin/action', data={
        'csrf': token, 'action': 'invite_creator', 'email': 'maker@example.test',
        'code': 'reserved'}, follow_redirects=False).status_code == 303
    token = browser(client, store, 'buyer')
    assert 'accept_invitation' not in client.get('/creators').text
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes', 'code': 'reserved'},
        follow_redirects=False).status_code == 403
    with store.connection() as db:
        db.execute("UPDATE accounts SET email_verified=0 WHERE id='maker'")
    token = browser(client, store, 'maker')
    assert 'accept_invitation' not in client.get('/creators').text
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
        follow_redirects=False).status_code == 403


def test_reserved_codes_cannot_be_reassigned_or_used_by_applicants(cloud):
    client, store, _ = cloud
    token = browser(client, store, 'owner')
    data = {'csrf': token, 'action': 'invite_creator', 'email': 'new@example.test', 'code': 'maker'}
    assert client.post('/admin/action', data=data, follow_redirects=False).status_code == 303
    assert client.post('/admin/action', data=data, follow_redirects=False).status_code == 303
    assert client.post('/admin/action', data={**data, 'email': 'other@example.test'},
                       follow_redirects=False).status_code == 409
    assert client.post('/admin/action', data={**data, 'code': 'different'},
                       follow_redirects=False).status_code == 409
    token = browser(client, store, 'maker')
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'apply', 'name': 'Maker', 'channel': 'https://example.test',
        'audience': 'Developers', 'plan': 'Tutorial', 'terms': 'yes'},
        follow_redirects=False).status_code == 303
    with store.connection() as db:
        profile = db.execute(
            "SELECT code,status FROM creator_profiles WHERE account='maker'"
        ).fetchone()
        assert profile['code'] != 'maker' and profile['status'] == 'applied'


def test_invitation_does_not_restore_suspended_creator(cloud):
    client, store, _ = cloud
    token = browser(client, store, 'owner')
    client.post('/admin/action', data={
        'csrf': token, 'action': 'invite_creator', 'email': 'maker@example.test',
        'code': 'reserved'})
    token = browser(client, store, 'maker')
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
        follow_redirects=False).status_code == 303
    with store.connection() as db:
        db.execute("UPDATE creator_profiles SET status='suspended' WHERE account='maker'")
    token = browser(client, store, 'maker')
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
        follow_redirects=False).status_code == 403


def test_existing_applicant_accepts_invitation_with_reserved_code(cloud):
    client, store, _ = cloud
    token = browser(client, store, 'maker')
    client.post('/creators/action', data={
        'csrf': token, 'action': 'apply', 'name': 'Maker', 'channel': 'https://example.test',
        'audience': 'Developers', 'plan': 'Tutorial', 'terms': 'yes'})
    token = browser(client, store, 'owner')
    assert client.post('/admin/action', data={
        'csrf': token, 'action': 'invite_creator', 'email': 'maker@example.test',
        'code': 'chosen_code'}, follow_redirects=False).status_code == 303
    token = browser(client, store, 'maker')
    assert 'accept_invitation' in client.get('/creators').text
    assert client.post('/creators/action', data={
        'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
        follow_redirects=False).status_code == 303
    with store.connection() as db:
        profile = db.execute("SELECT * FROM creator_profiles WHERE account='maker'").fetchone()
        assert profile['code'] == 'chosen_code' and profile['status'] == 'approved'
        assert profile['channel'] == 'https://example.test'


@pytest.mark.parametrize('accepted', [False, True])
def test_account_deletion_removes_creator_invitation(cloud, accepted):
    from varyz.cloud.creators import forget_account, invite_creator
    client, store, _ = cloud
    with store.connection() as db:
        invite_creator(db, 'maker@example.test', 'reserved')
    if accepted:
        token = browser(client, store, 'maker')
        assert client.post('/creators/action', data={
            'csrf': token, 'action': 'accept_invitation', 'terms': 'yes'},
            follow_redirects=False).status_code == 303
    with store.connection() as db:
        forget_account(db, 'maker')
        assert not db.execute('SELECT 1 FROM creator_invitations').fetchone()
