"""Mailbox ownership, bounded signup work and domain-validation gates."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from varyz.cloud import signup as emails
from varyz.cloud.app import Settings, create_app
from varyz.cloud.store import digest


@pytest.fixture
def signup(tmp_path):
    mail = []
    app = create_app(
        Settings(
            database=str(tmp_path / "cloud.sqlite"),
            email_sender=mail.append,
            signup_email_validator=lambda email: email,
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        yield client, app.state.store, mail


def enroll(client, email="owner@gmail.com", username="owner_name"):
    client.get("/signup")
    return client.post(
        "/signup",
        data={
            "csrf": client.cookies.get("csrf"),
            "email": email,
            "username": username,
            "password": "correct-password",
        },
    )


def verify(client, message):
    token = re.search(r"token=([^\s]+)", message.get_content()).group(1)
    client.get("/verify", params={"token": token})
    return client.post(
        "/verify",
        data={
            "csrf": client.cookies.get("csrf"),
            "token": token,
        },
        follow_redirects=False,
    )


def test_account_created_only_after_verification_and_no_token_replay(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        pending = db.execute("SELECT * FROM pending_signups").fetchone()
        assert pending["password"] != "correct-password"
        assert pending["hash"] not in mail[0].get_content()
    assert verify(client, mail[0]).status_code == 303
    assert verify(client, mail[0]).status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT email_verified FROM accounts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM pending_signups").fetchone()[0] == 0


def test_repeat_signup_does_not_replace_original_password_username_or_link(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    assert enroll(client, username="attacker_name").status_code == 200
    assert len(mail) == 1
    with store.connection() as db:
        assert db.execute("SELECT username FROM pending_signups").fetchone()[0] == "owner_name"
    assert verify(client, mail[0]).status_code == 303


def test_expired_signup_cannot_verify_and_is_cleaned_by_normal_requests(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    with store.connection() as db:
        db.execute("UPDATE pending_signups SET expires=?", (time.time() - 1,))
    assert verify(client, mail[0]).status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pending_signups").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_pending_queue_ceiling_has_no_permanent_account_side_effect(signup):
    client, store, mail = signup
    with store.connection() as db:
        db.executemany(
            "INSERT INTO pending_signups VALUES(?,?,?,?,?)",
            [
                (f"a{i}@gmail.com", "pending", "hash", digest(str(i)), time.time() + 3600)
                for i in range(500)
            ],
        )
    assert enroll(client).status_code == 503
    assert not mail
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_final_account_capacity_rechecked_when_verifying(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    client.app.state.settings.max_accounts = 1
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,email_verified) "
            "VALUES('full','other@gmail.com','hash',1)"
        )
    assert verify(client, mail[0]).status_code == 503
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_unverified_requests_do_not_reserve_usernames(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    assert enroll(client, email="second@gmail.com").status_code == 200
    assert verify(client, mail[1]).status_code == 303
    assert verify(client, mail[0]).status_code == 409
    with store.connection() as db:
        assert db.execute("SELECT email FROM accounts").fetchone()[0] == "second@gmail.com"


def test_pending_recovery_resend_preserves_details_and_invalidates_old_link(signup):
    client, store, mail = signup
    assert enroll(client).status_code == 200
    client.get("/recover")
    assert (
        client.post(
            "/recover",
            data={
                "csrf": client.cookies.get("csrf"),
                "email": "owner@gmail.com",
                "kind": "verification",
            },
        ).status_code
        == 200
    )
    assert len(mail) == 2
    assert verify(client, mail[0]).status_code == 400
    assert verify(client, mail[1]).status_code == 303


def test_service_signup_budget_survives_ip_rotation(signup):
    client, store, mail = signup
    # Middleware and signup share the backend; different window lengths must not
    # delete the longer-window counters. Proxy IP rotation can't reset global admission.
    assert store.allow_request("signup-global-hour", 3600, 1)
    assert store.allow_request("other-short-window", 60, 1)
    assert not store.allow_request("signup-global-hour", 3600, 1)
    for _ in range(10):
        assert store.allow_request("signup-global-minute", 60, 10)
    assert enroll(client).status_code == 429
    assert not mail


@pytest.mark.parametrize(
    "email",
    [
        "a@ie342g",
        "a@ie342g.invalid",
        "a@localhost",
        "a@127.0.0.1",
        "a@@gmail.com",
        "a@-bad.com",
        "a@gma il.com",
        "a@foo..com",
        "a@foo.com\r\nBCC:x@y.com",
        "a@mailinator.com",
        "a@sub.mailinator.com",
        "a@10minutemail.com",
    ],
)
def test_invalid_and_disposable_addresses_rejected_before_dns(monkeypatch, email):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid/disposable domain must not cause DNS lookup")

    monkeypatch.setattr(emails, "validate_email_deliverability", unexpected)
    with pytest.raises(emails.InvalidSignupEmail):
        emails.validate_signup_email(email)


def test_valid_custom_domain_allowed_and_dns_unknown_is_retryable(monkeypatch):
    monkeypatch.setattr(
        emails, "validate_email_deliverability", lambda *a, **k: {"mx": [(0, "mx.ie342g.com")]}
    )
    assert emails.validate_signup_email("Owner@ie342g.com") == "owner@ie342g.com"
    assert emails.normalize_email("Owner@bücher.de") == "owner@xn--bcher-kva.de"
    assert emails.normalize_email("Owner@xn--bcher-kva.de") == "owner@xn--bcher-kva.de"
    monkeypatch.setattr(
        emails,
        "validate_email_deliverability",
        lambda *a, **k: {"unknown-deliverability": "timeout"},
    )
    with pytest.raises(emails.SignupDNSUnavailable):
        emails.validate_signup_email("owner@ie342g.com")


def test_international_domain_uses_same_address_at_signup_login_and_recovery(signup):
    client, store, mail = signup
    assert enroll(client, "owner@bücher.de").status_code == 200
    with store.connection() as db:
        assert (
            db.execute("SELECT email FROM pending_signups").fetchone()[0]
            == "owner@xn--bcher-kva.de"
        )
    assert verify(client, mail[0]).status_code == 303
    client.get("/login")
    assert (
        client.post(
            "/login",
            data={
                "csrf": client.cookies.get("csrf"),
                "email": "owner@bücher.de",
                "password": "correct-password",
            },
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
                "email": "owner@bücher.de",
                "kind": "reset",
            },
        ).status_code
        == 200
    )
    assert len(mail) == 2


def test_domain_with_no_mail_service_rejected(monkeypatch):
    from email_validator import EmailUndeliverableError

    def no_mail(*args, **kwargs):
        raise EmailUndeliverableError("Domain does not exist")

    monkeypatch.setattr(emails, "validate_email_deliverability", no_mail)
    with pytest.raises(emails.InvalidSignupEmail):
        emails.validate_signup_email("owner@ie342g.com")


def test_signup_uses_domain_validation_and_has_no_invalid_request_side_effects(signup):
    client, store, mail = signup
    client.app.state.settings.signup_email_validator = None
    assert enroll(client, "owner@ie342g").status_code == 422
    assert enroll(client, "owner@mailinator.com").status_code == 422
    assert not mail
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pending_signups").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
