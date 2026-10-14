"""Check the retained synthetic pilot over HTTPS; erase temporary grants."""

import json

import httpx

from varyz.cloud.app import Settings, create_app
from varyz.cloud.sharing_service import SCOPE, identifier
from varyz.cloud.store import digest


def main(membership):
    app = create_app(Settings.from_env())
    store = app.state.store
    origin = app.state.settings.public_url
    assert origin == "https://cloud.varyzai.com"
    with store.connection() as db:
        actor = db.execute("SELECT id FROM accounts WHERE username='egr'").fetchone()["id"]
        app.state.sharing.member(db, actor, membership)
    grants = []
    try:
        session = store.credential(actor, "session", 300)
        grants.append(digest(session))
        with httpx.Client(base_url=origin, timeout=30) as client:
            client.cookies.set("__Host-session", session, domain="cloud.varyzai.com", path="/")
            url = "/api/sharing/v1/memberships/" + membership + "/connection"
            before = client.get(url)
            assert before.status_code == 200
            token = store.credential(actor, "access", 300, "setup-probe", scope=SCOPE)
            grants.append(digest(token))
            assert client.get(url).json()["stage"] in ("verify", "ready")
            result = client.post(
                "/mcp/shared",
                headers={"Authorization": "Bearer " + token},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "shared_brief",
                        "arguments": {
                            "membership_id": membership,
                            "conversation_id": "setup-probe-" + identifier(),
                            "expected_binding_revision": 0,
                        },
                    },
                },
            )
            assert result.status_code == 200
            assert not result.json()["result"].get("isError")
            assert not result.json()["result"]["structuredContent"]["truncated"]
            assert client.get(url).json()["stage"] == "ready"
            page = client.get("/account/shared/" + membership)
            assert page.status_code == 200 and "Ready to use" in page.text
            assert "data-setup-status" in page.text and "nonce=" in page.text
        print(
            json.dumps(
                {
                    "transport": "real HTTPS",
                    "readiness": True,
                    "server_rendered_setup": True,
                    "native_host_verified": False,
                }
            )
        )
    finally:
        with store.connection() as db:
            for value in grants:
                db.execute("DELETE FROM credentials WHERE hash=?", (value,))


if __name__ == "__main__":
    raise SystemExit("Run through the guarded rollout with the retained demo membership.")
