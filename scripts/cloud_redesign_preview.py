"""Real Cloud + local bridge browser acceptance, using isolated synthetic records only."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from varyz.cloud import cli, guided
from varyz.cloud import client as cloud_client
from varyz.cloud.app import Settings, create_app
from varyz.cloud.client import CloudClient
from varyz.cloud.computer import ComputerConnection, handle_computer_request
from varyz.db.connection import open_db
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.session_progress import Checkpoint, record


def run(live_fixture=None):
    root = Path(__file__).resolve().parents[1]
    out = root / (
        ".tmp/cloud-redesign-live-browser" if live_fixture else ".tmp/cloud-redesign-preview"
    )
    out.mkdir(parents=True, exist_ok=True)
    app = create_app(
        Settings(
            database=str(out / (uuid.uuid4().hex + ".sqlite")),
            public_url="https://cloud.varyzai.com",
            archive_encryption_key=Fernet.generate_key().decode(),
        )
    )
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('preview','preview@example.test','unused','active',1)"
        )
    session = (
        live_fixture["session"] if live_fixture else store.credential("preview", "session", 3600)
    )
    if live_fixture:
        from cloud_redesign_live_probe import fixture
    state = {"bridge": None, "oauth": 0, "keys": {}}

    def reset():
        if live_fixture:
            fixture("reset", live_fixture["identity"])
        folder = out / uuid.uuid4().hex
        folder.mkdir()
        config = folder / "config.toml"
        config.write_text('[storage]\ndata_dir="' + (folder / "data").as_posix() + '"\n')
        conn = open_db(folder / "data/varyz.db")
        caller = Caller(
            agent_id="owner:preview",
            display_name="Preview",
            purpose="owner_knowledge",
            device_id="preview",
            transport="local",
        )
        for scope, count in (("*", 1), ("alpha", 45), ("beta", 3)):
            for index in range(count):
                SQLitePrincipleStore(conn).add(
                    statement=f"Synthetic {scope} preference {index}",
                    scope=scope,
                    caller=caller,
                    device_id="preview",
                    source="owner",
                )
        for index, project in enumerate(("alpha-v14", "alpha-v15", "beta-v17")):
            record(
                conn,
                Checkpoint(
                    project=project,
                    conversation_id="preview-session",
                    checkpoint_id=str(index),
                    task="Synthetic project progress",
                    done=["Prepared example"],
                    pending=["Check setup"],
                ),
                agent_id="agent:codex:preview",
            )
        conn.close()
        state.update(bridge=ComputerConnection(config), oauth=0, keys={})
        with store.connection() as db:
            db.execute("DELETE FROM records WHERE account='preview'")
            db.execute("DELETE FROM changes WHERE account='preview'")
            db.execute("DELETE FROM credentials WHERE account='preview' AND kind!='session'")
            for table in (
                "cloud_session_reports",
                "session_report_permissions",
                "session_report_terms",
            ):
                db.execute("DELETE FROM " + table + " WHERE account='preview'")
        return {"ok": True}

    app.post("/__preview/reset")(reset)

    @app.post("/__preview/restart")
    def restart():
        # Simulate a full daemon restart after the browser capability has expired.
        state["bridge"].session_path.unlink(missing_ok=True)
        state["bridge"] = ComputerConnection(state["bridge"].config)
        if live_fixture:
            fixture("expire", live_fixture["identity"])
        with store.connection() as db:
            db.execute("UPDATE credentials SET expires=1 WHERE account='preview' AND kind='access'")
        return {"ok": True}

    @app.get("/__preview/state")
    def read_state():
        return {"oauth": state["oauth"], "state": state["bridge"].state}

    @app.post("/__preview/legacy_connection_hint")
    def legacy_connection_hint():
        # A legacy client label hides the server-side landing hint while leaving
        # the account's approved local configuration and valid token intact.
        if live_fixture:
            return fixture("legacy_connection_hint", live_fixture["identity"])
        with store.connection() as db:
            db.execute(
                "UPDATE credentials SET client='legacy-redesign-computer' "
                "WHERE account='preview' AND kind IN ('access','refresh')"
            )
        return {"ok": True}

    def payment_state(active):
        if live_fixture:
            return fixture(
                "payment_active" if active else "payment_pending", live_fixture["identity"]
            )
        with store.connection() as db:
            db.execute("UPDATE accounts SET status=? WHERE id='preview'",
                       ("active" if active else "inactive",))
        return {"ok": True}

    app.post("/__preview/payment_pending")(lambda: payment_state(False))
    app.post("/__preview/payment_active")(lambda: payment_state(True))

    def browser_connect(*args, **kwargs):
        state["oauth"] += 1
        return cloud_client.browser_connect(*args, **kwargs)

    def tokens(name):
        if name not in state["keys"]:
            raise ValueError("Cloud is disconnected; connect it before syncing.")
        return json.loads(state["keys"][name])

    cli.browser_connect = browser_connect
    cli.load_tokens = guided.load_tokens = tokens
    cli.keyring.set_password = lambda _service, name, value: state["keys"].__setitem__(name, value)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            handle_computer_request(self, state["bridge"])

        do_POST = do_OPTIONS = do_GET

        def log_message(self, *_args):
            pass

    reset()
    bridge_server = ThreadingHTTPServer(("127.0.0.1", 19389), Handler)
    bridge_thread = threading.Thread(target=bridge_server.serve_forever, daemon=True)
    bridge_thread.start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=19387, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    with TestClient(app, base_url="https://cloud.varyzai.com") as http:
        # The real PKCE callback and token exchange use this isolated Cloud app.
        if not live_fixture:
            cloud_client.httpx.post = lambda url, **kwargs: http.post(url, **kwargs)
            cli.CloudClient = guided.CloudClient = lambda url, value, **_kw: CloudClient(
                url, value, http=http
            )
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
            subprocess.run(
                [
                    str(runtime / "bin/node.exe"),
                    str(root / "scripts/cloud_redesign_preview.cjs"),
                    str(out),
                    str(runtime / "node_modules/playwright"),
                ],
                check=True,
                timeout=420,
                input=json.dumps({
                    "session": session, "live": bool(live_fixture),
                    "legacy_choices": "--legacy-choices" in sys.argv,
                }).encode(),
            )
        finally:
            server.should_exit = True
            thread.join(5)
            bridge_server.shutdown()
            bridge_server.server_close()


def main():
    if "--live" not in sys.argv:
        return run()
    from cloud_redesign_live_probe import fixture

    account = fixture("create")
    try:
        run(account)
    finally:
        fixture("cleanup", account["identity"])


if __name__ == "__main__":
    main()
