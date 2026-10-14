"""Synthetic persistence, disk backup/restore and bounded HTTPS burst checks.

Run prepare inside the service, restart it, then run verify. No owner data is
exported; the consistent backup is restored only into a temporary container file.
"""

import argparse
import json
import secrets
import sqlite3
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from varyz.cloud.app import LOCAL_CLIENT, Settings, password_hash
from varyz.cloud.store import Store, digest

STATE = Path("/var/lib/varyz-cloud/infrastructure-probe.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "verify"])
    args = parser.parse_args()
    settings = Settings.from_env()
    assert settings.paddle_sandbox
    store = Store(settings.database)
    if args.action == "prepare":
        assert not STATE.exists(), "Prior probe needs cleanup"
        owner = "infra_probe_" + secrets.token_hex(8)
        with store.connection() as db:
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) "
                "VALUES(?,?,?,'active',1)",
                (owner, owner + "@example.test", password_hash(secrets.token_urlsafe(32))),
            )
        token = store.credential(owner, "access", 1200, LOCAL_CLIENT)
        store.push(
            owner,
            [
                dict(
                    kind="rule",
                    id=owner,
                    base_version=0,
                    payload=dict(statement="Synthetic restart rule", scope="probe"),
                )
            ],
        )
        STATE.write_text(json.dumps(dict(owner=owner, token=token)), encoding="utf-8")
        STATE.chmod(0o600)
        print("PASS synthetic restart fixture prepared")
        return
    state = json.loads(STATE.read_text())
    owner = state["owner"]
    try:
        with httpx.Client(base_url=settings.public_url, timeout=30) as client:
            headers = {"Authorization": "Bearer " + state["token"]}
            response = client.get("/api/sync", headers=headers)
            assert response.status_code == 200, response.status_code
            assert response.json()["records"][0]["payload"]["statement"] == "Synthetic restart rule"
            print("PASS account, credential and preference survive container restart", flush=True)
            with tempfile.TemporaryDirectory() as directory:
                backup = Path(directory) / "backup.sqlite"
                with (
                    sqlite3.connect(settings.database) as source,
                    sqlite3.connect(backup) as target,
                ):
                    source.backup(target)
                with sqlite3.connect(backup) as restored:
                    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert (
                        restored.execute(
                            "SELECT COUNT(*) FROM credentials WHERE hash=?",
                            (digest(state["token"]),),
                        ).fetchone()[0]
                        == 1
                    )
                    assert (
                        restored.execute(
                            "SELECT COUNT(*) FROM records WHERE account=?", (owner,)
                        ).fetchone()[0]
                        == 1
                    )
                print("PASS on-disk SQLite snapshot and isolated restore", flush=True)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=16) as executor:
                statuses = list(
                    executor.map(
                        lambda _: client.get("/api/account", headers=headers).status_code, range(40)
                    )
                )
            assert set(statuses) <= {200, 429, 503}, Counter(statuses)
            assert 200 in statuses, Counter(statuses)
            print(
                json.dumps(
                    {
                        "burst": dict(Counter(statuses)),
                        "requests": 40,
                        "concurrency": 16,
                        "seconds": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
    finally:
        with store.connection() as db:
            for table in ("credentials", "records", "changes", "account_usage"):
                db.execute("DELETE FROM " + table + " WHERE account=?", (owner,))
            db.execute("DELETE FROM accounts WHERE id=?", (owner,))
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
