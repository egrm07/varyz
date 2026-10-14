"""Synthetic HTTPS acceptance probe, run inside the deployed cloud container.

Never prints tokens or sends an owner's archive. Test rows are removed in finally.
Billing lifecycle uses signed synthetic events, not a provider purchase.
"""

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx

from varyz.cloud.app import LOCAL_CLIENT, Settings, password_hash
from varyz.cloud.store import Store


def main():
    settings = Settings.from_env()
    assert settings.paddle_sandbox, "Probe requires sandbox billing"
    store = Store(settings.database)
    prefix = "probe_" + secrets.token_hex(8)
    owners = [prefix + suffix for suffix in ("a", "b")]
    passed = []
    event_ids = []

    def check(label, condition):
        assert condition, label
        passed.append(label)
        print("PASS " + label, flush=True)

    def expected(response, status):
        assert response.status_code == status, (response.status_code, response.text[:200])
        return response

    with httpx.Client(base_url=settings.public_url, timeout=30) as client:
        try:
            for owner in owners:
                with store.connection() as db:
                    db.execute(
                        "INSERT INTO accounts(id,email,password,status,email_verified) "
                        "VALUES(?,?,?,'active',1)",
                        (owner, owner + "@example.test", password_hash("probe-password-123")),
                    )
            tokens = [store.credential(o, "access", 600, LOCAL_CLIENT) for o in owners]
            headers = {"Authorization": "Bearer " + tokens[0]}
            other = {"Authorization": "Bearer " + tokens[1]}
            check("HTTPS health", client.get("/health").json() == {"status": "ok"})
            check(
                "HSTS",
                "max-age=" in client.get("/health").headers.get("strict-transport-security", ""),
            )
            expected(client.get("/api/account"), 401)
            check("unauthenticated access denied", True)
            record = dict(
                kind="rule",
                id=prefix,
                base_version=0,
                payload=dict(statement="Use concise synthetic prose", scope="probe"),
            )
            first = expected(
                client.post("/api/sync", headers=headers, json={"records": [record]}), 200
            ).json()
            retry = expected(
                client.post("/api/sync", headers=headers, json={"records": [record]}), 200
            ).json()
            check("sync retry idempotent", retry == first)
            check(
                "tenant isolation", client.get("/api/sync", headers=other).json()["records"] == []
            )
            conflicting = {**record, "payload": dict(statement="Different", scope="probe")}
            expected(
                client.post("/api/sync", headers=headers, json={"records": [conflicting]}), 409
            )
            check("conflict rejection", True)
            expected(client.post("/api/sync", headers=headers, content=b"x" * 262145), 413)
            check("oversize body rejection", True)
            expected(
                client.get("/api/account", headers={**headers, "Origin": "https://evil.example"}),
                403,
            )
            check("cross-origin rejection", True)

            client.get("/login")
            expected(
                client.post(
                    "/login",
                    headers={"Origin": settings.public_url},
                    data={
                        "csrf": client.cookies.get("__Host-csrf"),
                        "email": owners[0] + "@example.test",
                        "password": "probe-password-123",
                    },
                ),
                303,
            )
            check("browser login and dashboard", client.get("/").status_code == 200)
            check(
                "secure session cookie",
                any(c.name == "__Host-session" and c.secure for c in client.cookies.jar),
            )
            session_cookie = next(c for c in client.cookies.jar if c.name == "__Host-session")
            check(
                "30-day persistent session",
                29 * 86400 < session_cookie.expires - time.time() <= 30 * 86400,
            )
            with httpx.Client(base_url=settings.public_url, timeout=30) as reopened:
                reopened.cookies.set("__Host-session", session_cookie.value)
                check(
                    "saved cookie signs in next session", reopened.get("/login").status_code == 303
                )
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            redirect = "http://127.0.0.1:8766/callback"
            query = dict(
                response_type="code",
                client_id=LOCAL_CLIENT,
                redirect_uri=redirect,
                code_challenge_method="S256",
                code_challenge=challenge,
                state=prefix,
            )
            expected(client.get("/oauth/authorize", params=query), 200)
            consent = expected(
                client.post(
                    "/oauth/authorize",
                    headers={"Origin": settings.public_url},
                    data={**query, "csrf": client.cookies.get("__Host-csrf")},
                ),
                303,
            )
            params = parse_qs(urlparse(consent.headers["location"]).query)
            grant = dict(
                grant_type="authorization_code",
                code=params["code"][0],
                client_id=LOCAL_CLIENT,
                redirect_uri=redirect,
                code_verifier=verifier,
            )
            oauth = expected(client.post("/oauth/token", data=grant), 200).json()
            expected(client.post("/oauth/token", data=grant), 400)
            check("OAuth PKCE and single-use code", params["state"] == [prefix])
            refreshed = expected(
                client.post(
                    "/oauth/token",
                    data=dict(
                        grant_type="refresh_token",
                        client_id=LOCAL_CLIENT,
                        refresh_token=oauth["refresh_token"],
                    ),
                ),
                200,
            ).json()
            expected(
                client.post(
                    "/oauth/token",
                    data=dict(
                        grant_type="refresh_token",
                        client_id=LOCAL_CLIENT,
                        refresh_token=oauth["refresh_token"],
                    ),
                ),
                400,
            )
            check("single-use refresh rotation", bool(refreshed["access_token"]))
            # Deliberate replay revokes this client's credentials. Continue the
            # independent MCP checks with a fresh synthetic credential.
            headers = {
                "Authorization": "Bearer "
                + store.credential(owners[0], "access", 600, LOCAL_CLIENT)
            }

            def mcp(name, args):
                response = expected(
                    client.post(
                        "/mcp",
                        headers=headers,
                        json=dict(
                            jsonrpc="2.0",
                            id=1,
                            method="tools/call",
                            params=dict(name=name, arguments=args),
                        ),
                    ),
                    200,
                ).json()
                assert not response["result"].get("isError"), response
                return json.loads(response["result"]["content"][0]["text"])

            briefing = mcp("start_task", {"project": "probe"})
            check(
                "hosted MCP retrieves selected rule",
                "Use concise synthetic prose" in json.dumps(briefing),
            )
            question = mcp("ask", {"project": "probe", "question": "Synthetic font?"})
            question_id = question["question_id"]
            mcp("answer_owner_question", {"question_id": question_id, "answer": "Synthetic Sans"})
            check(
                "hosted answer round trip",
                mcp("ask", {"project": "probe", "question": "Synthetic font?"})["answer"]
                == "Synthetic Sans",
            )
            mcp(
                "away",
                {
                    "action": "on",
                    "conversation_id": prefix,
                    "owner_words": "Keep working while I am away",
                },
            )
            check(
                "hosted away state",
                mcp("away", {"action": "status", "conversation_id": prefix})["away"],
            )
            mcp("away", {"action": "complete", "conversation_id": prefix})

            # Exercise verification/reset through real HTTPS using test-only tokens.
            verify = store.credential(owners[1], "verification", 600)
            expected(client.get("/verify", params={"token": verify}), 200)
            expected(
                client.post(
                    "/verify", data={"token": verify, "csrf": client.cookies.get("__Host-csrf")}
                ),
                303,
            )
            expected(
                client.post(
                    "/verify", data={"token": verify, "csrf": client.cookies.get("__Host-csrf")}
                ),
                400,
            )
            check("verification single use", True)
            reset = store.credential(owners[1], "reset", 600)
            expected(client.get("/reset", params={"token": reset}), 200)
            expected(
                client.post(
                    "/reset",
                    data={
                        "token": reset,
                        "csrf": client.cookies.get("__Host-csrf"),
                        "password": "new-probe-password-123",
                    },
                ),
                303,
            )
            expected(client.get("/api/account", headers=other), 401)
            check("password reset revokes credentials", True)

            # Consistent backup/restore, isolated from the running database.
            with (
                sqlite3.connect(settings.database) as source,
                sqlite3.connect(":memory:") as restored,
            ):
                source.backup(restored)
                check(
                    "SQLite backup integrity and restored synthetic record",
                    restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    and restored.execute(
                        "SELECT COUNT(*) FROM records WHERE account=?", (owners[0],)
                    ).fetchone()[0]
                    == 2,
                )

            if settings.paddle_secret:

                def webhook(kind, data, offset):
                    event_id = prefix + str(offset)
                    event_ids.append(event_id)
                    payload = json.dumps(
                        dict(
                            event_id=event_id,
                            event_type=kind,
                            occurred_at=(datetime.now(UTC) + timedelta(seconds=offset)).isoformat(),
                            data=data,
                        )
                    ).encode()
                    stamp = str(int(time.time()))
                    signature = hmac.new(
                        settings.paddle_secret.encode(),
                        stamp.encode() + b":" + payload,
                        hashlib.sha256,
                    ).hexdigest()
                    return expected(
                        client.post(
                            "/api/billing/webhook",
                            content=payload,
                            headers={"Paddle-Signature": "ts=" + stamp + ";h1=" + signature},
                        ),
                        200,
                    )

                data = dict(
                    id=prefix + "sub",
                    status="past_due",
                    custom_data={"varyz_account_id": owners[0]},
                    items=[{"price": {"id": settings.paddle_prices[0]}}],
                )
                webhook("subscription.past_due", data, 1)
                expected(client.get("/api/sync", headers=headers), 402)
                expected(client.get("/api/export", headers=headers), 200)
                webhook("subscription.activated", {**data, "status": "active"}, 2)
                check(
                    "signed billing lifecycle and unpaid export",
                    client.get("/api/sync", headers=headers).status_code == 200,
                )
            else:
                print("OPEN sandbox webhook secret missing", flush=True)
            with store.connection() as db:
                db.execute("UPDATE accounts SET subscription=NULL WHERE id=?", (owners[0],))
            expected(client.delete("/api/account", headers=headers), 200)
            expected(client.get("/api/account", headers=headers), 401)
            check("account deletion and credential revocation", True)
        finally:
            with store.connection() as db:
                for table in (
                    "credentials",
                    "records",
                    "changes",
                    "questions",
                    "away_state",
                    "account_usage",
                    "billing_checkouts",
                    "billing_payments",
                ):
                    for owner in owners:
                        db.execute("DELETE FROM " + table + " WHERE account=?", (owner,))
                for owner in owners:
                    db.execute("DELETE FROM accounts WHERE id=?", (owner,))
                for event_id in event_ids:
                    db.execute("DELETE FROM events WHERE id=?", (event_id,))
    print(json.dumps({"passed": len(passed)}))


if __name__ == "__main__":
    main()
