"""Exercise Cloud setup in a local app with synthetic data and an intercepted bridge."""

import subprocess
import threading
import time
from pathlib import Path

import uvicorn

from varyz.cloud.app import Settings, create_app


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / ".tmp" / "cloud-setup-preview"
    out.mkdir(parents=True, exist_ok=True)
    app = create_app(
        Settings(database=str(out / "preview.sqlite"), public_url="https://cloud.varyzai.com")
    )
    with app.state.store.connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO accounts(id,email,password,status,email_verified) "
            "VALUES('preview','preview@example.test','unused','active',1)"
        )
    session = app.state.store.credential("preview", "session", 3600)

    @app.post("/__preview/reset")
    def reset_preview():
        with app.state.store.connection() as db:
            db.execute("DELETE FROM records WHERE account='preview'")
            db.execute("DELETE FROM credentials WHERE account='preview' AND kind!='session'")
        return {"ok": True}

    @app.post("/__preview/sync")
    def sync_preview():
        app.state.store.push(
            "preview",
            [
                {
                    "kind": "rule",
                    "id": "synthetic-rule",
                    "base_version": 0,
                    "payload": {
                        "statement": "Show one step at a time",
                        "scope": "*",
                        "kind": "rule",
                    },
                }
            ],
        )
        app.state.store.credential("preview", "access", 3600, client="varyz-local")
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=19387, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
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
                str(root / "scripts/cloud_setup_preview.cjs"),
                str(out),
                str(runtime / "node_modules/playwright"),
                session,
            ],
            check=True,
            timeout=90,
        )
    finally:
        server.should_exit = True
        thread.join(5)


if __name__ == "__main__":
    main()
