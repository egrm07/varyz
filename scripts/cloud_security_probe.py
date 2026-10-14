"""Exercise security fixes over live HTTPS using disposable synthetic accounts.

Run inside the deployed container. Output never contains credentials or owner data.
"""

import json
import secrets
import socket
import ssl
import time
from urllib.parse import urlparse

import httpx

from varyz.cloud.app import LOCAL_CLIENT, Settings, password_hash
from varyz.cloud.store import Store


def main():
    settings = Settings.from_env()
    assert settings.database.startswith("/"), "Live probe requires the deployed SQLite backend"
    store = Store(settings.database)
    prefix = "security_probe_" + secrets.token_hex(8)
    owners = [prefix + "a", prefix + "b"]
    results = []

    def check(label, condition):
        assert condition, label
        results.append(label)
        print("PASS " + label, flush=True)

    with httpx.Client(base_url=settings.public_url, timeout=20, follow_redirects=False) as client:
        try:
            with store.connection() as db:
                for owner in owners:
                    db.execute(
                        "INSERT INTO accounts(id,email,password,status,email_verified) "
                        "VALUES(?,?,?,'active',1)",
                        (owner, owner + "@example.test", password_hash("synthetic-password-123")),
                    )
            access = [store.credential(owner, "access", 600, LOCAL_CLIENT) for owner in owners]
            headers = {"Authorization": "Bearer " + access[0]}
            other = {"Authorization": "Bearer " + access[1]}
            check("unauthenticated account denied", client.get("/api/account").status_code == 401)
            check(
                "fake bearer denied",
                client.get(
                    "/api/account", headers={"Authorization": "Bearer audit-invalid"}
                ).status_code
                == 401,
            )
            check(
                "foreign origin denied",
                client.get("/api/account", headers={"Origin": "https://evil.example"}).status_code
                == 403,
            )
            check(
                "unsigned webhook denied",
                client.post("/api/billing/webhook", json={}).status_code == 401,
            )
            record = {
                "kind": "rule",
                "id": prefix,
                "base_version": 0,
                "payload": {"statement": "Synthetic security test", "scope": "security-probe"},
            }
            check(
                "authorized sync works",
                client.post("/api/sync", headers=headers, json={"records": [record]}).status_code
                == 200,
            )
            check(
                "other account cannot read record",
                not client.get("/api/sync", headers=other).json()["records"],
            )
            old_refresh = store.credential(owners[0], "refresh", 600, "security-connector")
            fields = {
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": "security-connector",
            }
            response = client.post("/oauth/token", data=fields)
            check("refresh rotation works", response.status_code == 200)
            rotated = response.json()
            rotated_headers = {"Authorization": "Bearer " + rotated["access_token"]}
            check(
                "rotated access works",
                client.get("/api/account", headers=rotated_headers).status_code == 200,
            )
            check(
                "refresh replay rejected",
                client.post("/oauth/token", data=fields).status_code == 400,
            )
            check(
                "refresh replay revokes current access",
                client.get("/api/account", headers=rotated_headers).status_code == 401,
            )
            check(
                "refresh replay revokes current refresh",
                client.post(
                    "/oauth/token", data={**fields, "refresh_token": rotated["refresh_token"]}
                ).status_code
                == 400,
            )
            check(
                "other account survives replay",
                client.get("/api/account", headers=other).status_code == 200,
            )
            token = store.credential(owners[0], "access", 600, "revocation-connector")
            refresh = store.credential(owners[0], "refresh", 600, "revocation-connector")
            check(
                "refresh revocation succeeds",
                client.post(
                    "/oauth/revoke", data={"token": refresh, "client_id": "revocation-connector"}
                ).status_code
                == 200,
            )
            check(
                "companion access revoked",
                client.get("/api/account", headers={"Authorization": "Bearer " + token}).status_code
                == 401,
            )
            check(
                "malformed redirect rejected",
                client.post(
                    "/oauth/register", json={"redirect_uris": ["https://[broken/callback"]}
                ).status_code
                == 422,
            )
            check(
                "ambiguous redirect rejected",
                client.post(
                    "/oauth/register", json={"redirect_uris": ["https://connector.test\n/callback"]}
                ).status_code
                == 422,
            )
            check(
                "malformed form rejected",
                client.post("/oauth/token", content=b"\xff").status_code == 400,
            )
            client.get("/login")
            csrf_cookie = next(c for c in client.cookies.jar if c.name == "__Host-csrf")
            check(
                "host-protected CSRF cookie",
                csrf_cookie.secure and not csrf_cookie.domain_specified,
            )
            check(
                "unicode CSRF rejected",
                client.post("/login", data={"csrf": "\u00e9"}).status_code == 403,
            )
            client.cookies.set("session", store.credential(owners[0], "session", 600))
            check("unprefixed session ignored", client.get("/login").status_code == 200)
            old_time = str(time.time() - 901)
            stale = store.credential(owners[0], "refresh", 600, LOCAL_CLIENT, challenge=old_time)
            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": stale,
                    "client_id": LOCAL_CLIENT,
                },
            )
            check("stale refresh still supports sync", response.status_code == 200)
            stale_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            check(
                "stale authentication cannot delete account",
                client.delete("/api/account", headers=stale_headers).status_code == 403,
            )
            check(
                "oversized body rejected",
                client.post("/mcp", content=b"x" * (settings.body_limit + 1)).status_code == 413,
            )
            host = urlparse(settings.public_url).hostname
            with (
                socket.create_connection((host, 443), timeout=15) as transport,
                ssl.create_default_context().wrap_socket(
                    transport, server_hostname=host
                ) as connection,
            ):
                connection.sendall(
                    (
                        f"POST /mcp HTTP/1.1\r\nHost: {host}\r\n"
                        "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n1\r\n{\r\n"
                    ).encode()
                )
                line = connection.recv(4096).split(b"\r\n", 1)[0]
                check("slow upload expires at application deadline", b" 408 " in line)
            check(
                "service healthy after adversarial probes", client.get("/health").status_code == 200
            )
            print(json.dumps({"passed": len(results)}), flush=True)
        finally:
            with store.connection() as db:
                for owner in owners:
                    for table in (
                        "credentials",
                        "records",
                        "changes",
                        "questions",
                        "away_state",
                        "account_usage",
                    ):
                        db.execute(f"DELETE FROM {table} WHERE account=?", (owner,))
                    db.execute("DELETE FROM accounts WHERE id=?", (owner,))


if __name__ == "__main__":
    main()
