"""Synthetic loopback-only Public Varyz browser harness; never deploy this module."""

from pathlib import Path
from tempfile import mkdtemp

import uvicorn
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from varyz.cloud.app import Settings, create_app
from varyz.cloud.browser import set_cookie
from varyz.cloud.sharing_service import identifier

root = Path(__file__).resolve().parents[1] / ".tmp"
root.mkdir(exist_ok=True)
database = Path(mkdtemp(prefix="public-preview-", dir=root)) / "cloud.sqlite"
app = create_app(
    Settings(
        database=str(database),
        public_url="http://127.0.0.1:8823",
        shared_varyz_enabled=True,
        archive_encryption_key=Fernet.generate_key().decode(),
    )
)
store = app.state.store
following = app.state.sharing.following
with store.connection() as db:
    for name, status in (("egr", "active"), ("ann", "active"), ("guest", "inactive")):
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified,username) "
            "VALUES(?,?,?,?,1,?)",
            (name, name + "@example.test", "unused", status, name),
        )
for name in ("egr", "ann"):
    store.push(
        name,
        [
            dict(
                kind="rule",
                id="review",
                base_version=0,
                payload=dict(
                    statement="In code review, read the whole diff before commenting.", scope="*"
                ),
            ),
            dict(
                kind="rule",
                id="risks",
                base_version=0,
                payload=dict(
                    statement="In code review, explain the user impact of each issue.", scope="*"
                ),
            ),
            dict(
                kind="answer",
                id="tests",
                base_version=0,
                payload=dict(
                    question="Should pull requests include tests?",
                    answer="Yes, for changes in behavior.",
                    scope="*",
                ),
            ),
            dict(
                kind="rule",
                id="private",
                base_version=0,
                payload=dict(statement="PRIVATE_PROJECT_SENTINEL", scope="private-project"),
            ),
        ],
    )
preview = following.preview(
    "ann", "synthetic", "code review", [{"kind": "rule", "id": "review", "version": 1}]
)
following.publish("ann", "synthetic", preview["preview_id"], identifier())


@app.get("/preview/as/{name}")
def identity(name: str, request: Request):
    if request.client.host not in ("127.0.0.1", "::1") or name not in ("egr", "guest"):
        raise HTTPException(404)
    with store.connection() as db:
        db.execute("DELETE FROM rate_limits")  # This isolated synthetic database only.
    response = RedirectResponse("/account/public" if name == "egr" else "/@egr", 303)
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8823, access_log=False)
