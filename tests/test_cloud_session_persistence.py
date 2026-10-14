"""Real session aging, renewal, browser restart and routine account actions."""

import time

from fastapi.testclient import TestClient

from varyz.cloud.app import SESSION_TTL, Settings, create_app


def test_old_session_renews_and_survives_browser_and_app_restart(tmp_path):
    settings = Settings(database=str(tmp_path / "cloud.sqlite"))
    app = create_app(settings)
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('alice','alice@example.test','unused','active',1)"
        )
    authenticated = str(time.time() - 20 * 86400)
    session = store.credential("alice", "session", 10 * 86400, challenge=authenticated)
    connector = store.credential("alice", "access", 3600, "a-connector")
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    client.cookies.set("session", session)
    page = client.get("/?view=dashboard")
    assert page.status_code == 200
    cookies = page.headers.get_list("set-cookie")
    assert any(c.startswith("session=") and f"Max-Age={SESSION_TTL}" in c for c in cookies)
    assert any(c.startswith("csrf=") and f"Max-Age={SESSION_TTL}" in c for c in cookies)
    saved = store.lookup(session, "session")
    assert saved["expires"] > time.time() + 29 * 86400
    assert saved["challenge"] == authenticated
    restarted = TestClient(create_app(settings), base_url="http://127.0.0.1:8787")
    restarted.cookies.set("session", session)
    assert restarted.get("/?view=dashboard").status_code == 200
    result = restarted.post(
        "/account/action",
        data={
            "csrf": restarted.cookies.get("csrf"),
            "action": "revoke_connection",
            "client_id": "a-connector",
            "confirm_action": "1",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303 and result.headers["location"] == "/"
    assert store.lookup(connector, "access") is None
    assert store.lookup(session, "session") is not None
    # Lifetime renewal is not a new authentication for irreversible actions.
    result = restarted.post(
        "/account/action",
        data={
            "csrf": restarted.cookies.get("csrf"),
            "action": "delete_account",
            "confirm": "DELETE",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303 and result.headers["location"] == "/login?reauth=1"
    assert "Sign in again" not in result.text


def test_logout_does_not_renew_or_resurrect_an_old_session(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('alice','alice@example.test','unused','active',1)"
        )
    session = store.credential("alice", "session", 10 * 86400)
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    client.cookies.set("session", session)
    client.get("/")
    with store.connection() as db:
        db.execute("UPDATE credentials SET expires=? WHERE kind='session'", (time.time() + 100,))
    response = client.post(
        "/account/action",
        data={
            "csrf": client.cookies.get("csrf"),
            "action": "logout",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.lookup(session, "session") is None
    assert any("Max-Age=0" in c for c in response.headers.get_list("set-cookie"))
