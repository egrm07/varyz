"""Loopback-only synthetic browser acceptance harness; never deploy this module."""

import time
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from varyz.cloud.app import Settings, create_app
from varyz.cloud.browser import set_cookie
from varyz.cloud.sharing_service import SCOPE

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/shared-invite-preview"
OUT.mkdir(parents=True, exist_ok=True)
database = OUT / "preview.sqlite"
key_path = OUT / "encryption.key"
if not key_path.exists():
    key_path.write_bytes(Fernet.generate_key())
app = create_app(
    Settings(
        database=str(database),
        public_url="http://127.0.0.1:8822",
        shared_varyz_enabled=True,
        shared_varyz_plugin_url="https://chatgpt.com/plugins/plugins_6ac82ff6995c8191a12c507ae9e30b03",
        archive_encryption_key=key_path.read_text(),
    )
)
store = app.state.store
with store.connection() as db:
    for name, status in [("egr", "active"), ("joemama", "inactive"), ("outsider", "inactive")]:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified,username) "
            "VALUES(?,?,?, ?,1,?) ON CONFLICT(id) DO NOTHING",
            (name, name + "@example.test", "unused", status, name),
        )
    present = db.execute("SELECT 1 FROM records WHERE account='egr'").fetchone()
if not present:
    store.push(
        "egr",
        [
            dict(
                kind="rule",
                id="calm",
                base_version=0,
                payload=dict(
                    statement="Keep the page calm. Give the typography room to breathe.",
                    scope="website",
                ),
            ),
            dict(
                kind="answer",
                id="pricing",
                base_version=0,
                payload=dict(
                    question="Include pricing?",
                    answer="Yes. Explain that Cloud is optional.",
                    scope="website",
                ),
            ),
            dict(
                kind="rule",
                id="general",
                base_version=0,
                payload=dict(statement="General guidance is private until selected.", scope="*"),
            ),
        ],
    )


@app.get("/preview/as/{name}")
def identity(name: str, request: Request):
    assert request.client.host in ("127.0.0.1", "::1") and name in ("egr", "joemama", "outsider")
    # Each synthetic browser run starts without a leftover AI connection.
    with store.connection() as db:
        db.execute("DELETE FROM rate_limits")  # Synthetic loopback harness only.
        if name == "egr":
            db.execute("DELETE FROM shared_projects WHERE owner='egr'")
        db.execute(
            "DELETE FROM credentials WHERE account=? AND kind IN ('access','refresh')", (name,)
        )
    response = RedirectResponse(
        "/account/shares/new?project=website" if name == "egr" else "/account/shared",
        status_code=303,
    )
    set_cookie(
        response,
        "session",
        store.credential(name, "session", 3600),
        secure=False,
        httponly=True,
        samesite="lax",
        max_age=3600,
    )
    return response


@app.get("/preview/access/{name}")
def access(name: str, request: Request):
    assert request.client.host in ("127.0.0.1", "::1") and name in ("joemama", "outsider")
    return JSONResponse(
        {"access_token": store.credential(name, "access", 900, "browser-acceptance", scope=SCOPE)}
    )


@app.get("/preview/stale")
def stale(request: Request):
    assert request.client.host in ("127.0.0.1", "::1")
    with store.connection() as db:
        db.execute(
            "UPDATE credentials SET challenge=? WHERE kind='session'", (str(time.time() - 1000),)
        )
    return {"synthetic_sessions_stale": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8822, access_log=False)
