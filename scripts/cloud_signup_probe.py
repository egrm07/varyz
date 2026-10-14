"""Verify deployed signup protection without sending email to anyone.

Run inside the deployed container. Public rejection/confirmation checks use a
synthetic fixture; successful signup delivery is intercepted in memory in an
isolated database using the installed release and real DNS validation.
"""

import json
import re
import secrets
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app, password_hash
from varyz.cloud.signup import disposable_domains, validate_signup_email
from varyz.cloud.store import Store, digest


def main():
    settings = Settings.from_env()
    store = Store(settings.database)
    prefix = "signup_probe_" + secrets.token_hex(8)
    email = prefix + "@probe.invalid"
    results = []

    def check(label, value):
        assert value, label
        results.append(label)
        print("PASS " + label, flush=True)

    def form_token(response):
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    check(
        "real DNS accepts a permanent mail domain",
        validate_signup_email("probe@gmail.com") == "probe@gmail.com",
    )
    check("vendored disposable list packaged", len(disposable_domains()) > 1000)
    with store.connection() as db:
        original_count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    with httpx.Client(base_url=settings.public_url, timeout=20, follow_redirects=False) as client:
        try:
            for address, label in (
                (prefix + "@ie342g", "single-label random domain rejected"),
                (prefix + "@mailinator.com", "disposable mailbox rejected"),
                (
                    prefix + "@" + prefix.replace("_", "-") + ".com",
                    "nonexistent mail domain rejected",
                ),
            ):
                token = form_token(client.get("/signup"))
                response = client.post(
                    "/signup",
                    data={
                        "csrf": token,
                        "email": address,
                        "username": prefix,
                        "password": "synthetic-correct-password",
                    },
                )
                check(label, response.status_code == 422)
            with store.connection() as db:
                check(
                    "rejected signups consume no account slots",
                    db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == original_count,
                )
                check(
                    "rejected signups create no pending record",
                    not db.execute(
                        "SELECT 1 FROM pending_signups WHERE username=?", (prefix,)
                    ).fetchone(),
                )
                raw = secrets.token_urlsafe(48)
                db.execute(
                    "INSERT INTO pending_signups VALUES(?,?,?,?,?)",
                    (
                        email,
                        prefix,
                        password_hash("synthetic-correct-password"),
                        digest(raw),
                        time.time() + 3600,
                    ),
                )
            response = client.get("/verify", params={"token": raw})
            token = form_token(response)
            response = client.post("/verify", data={"csrf": token, "token": raw})
            check("public confirmation creates verified account", response.status_code == 303)
            with store.connection() as db:
                row = db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
                check(
                    "account is verified and pending row consumed",
                    row
                    and row["email_verified"] == 1
                    and not db.execute(
                        "SELECT 1 FROM pending_signups WHERE email=?", (email,)
                    ).fetchone(),
                )
            response = client.post("/verify", data={"csrf": token, "token": raw})
            check("public confirmation cannot replay", response.status_code == 400)
        finally:
            with store.connection() as db:
                row = db.execute("SELECT id FROM accounts WHERE email=?", (email,)).fetchone()
                if row:
                    db.execute("DELETE FROM credentials WHERE account=?", (row["id"],))
                    db.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
                db.execute("DELETE FROM pending_signups WHERE email=?", (email,))

    mail = []
    with tempfile.TemporaryDirectory(prefix="signup-verification-") as directory:
        isolated = replace(
            settings, database=str(Path(directory) / "check.sqlite"), email_sender=mail.append
        )
        app = create_app(isolated)
        with TestClient(app, base_url=settings.public_url) as client:
            token = form_token(client.get("/signup"))
            data = {
                "csrf": token,
                "email": "synthetic-probe@gmail.com",
                "username": prefix,
                "password": "synthetic-correct-password",
            }
            check(
                "installed release accepts valid signup with real DNS",
                client.post("/signup", data=data).status_code == 200 and len(mail) == 1,
            )
            with app.state.store.connection() as db:
                check(
                    "valid unverified signup uses zero account slots",
                    db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
                    and db.execute("SELECT COUNT(*) FROM pending_signups").fetchone()[0] == 1,
                )
            data["username"] = "attacker_name"
            check(
                "repeat signup cannot overwrite pending details",
                client.post("/signup", data=data).status_code == 200 and len(mail) == 1,
            )
            with app.state.store.connection() as db:
                check(
                    "original pending username retained",
                    db.execute("SELECT username FROM pending_signups").fetchone()[0] == prefix,
                )
            raw = re.search(r"token=([^\s]+)", mail[0].get_content()).group(1)
            token = form_token(client.get("/verify", params={"token": raw}))
            check(
                "new signup confirmation works end to end",
                client.post(
                    "/verify", data={"csrf": token, "token": raw}, follow_redirects=False
                ).status_code
                == 303,
            )
            check(
                "confirmed mailbox can sign in",
                client.post(
                    "/login",
                    data={
                        "csrf": token,
                        "email": "synthetic-probe@gmail.com",
                        "password": "synthetic-correct-password",
                    },
                    follow_redirects=False,
                ).status_code
                == 303,
            )
    with httpx.Client(base_url=settings.public_url, timeout=15) as client:
        check("service healthy after probes", client.get("/health").status_code == 200)
    print(json.dumps({"passed": len(results), "smtp_messages_sent": 0}), flush=True)


if __name__ == "__main__":
    main()
