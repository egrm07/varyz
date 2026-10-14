"""Offline deployed-image acceptance: synthetic accounts, no owner data or network."""

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import PLUGIN_SCOPE, Archive, Change, Chunk, Document


def main():
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Settings(database=str(Path(directory) / "test.db"),
                                  public_url="http://localhost", archive_limit=2_000_000))
        store = app.state.store
        archive = Archive(store, "http://localhost", 2_000_000)
        client = TestClient(app, base_url="http://localhost")
        for owner in ("alice", "bob"):
            with store.connection() as db:
                db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
                           "VALUES(?,?,?,?,1)",
                           (owner, owner + "@example.test", "unused", "active"))
            for identity, project in (("teams", "varyz"), ("other", "another")):
                text = "Unrelated context. " * 100 + f"Teams for {owner}: shared billing."
                doc = Document(id=identity, project=project, source="documents",
                               external_id="notes.txt", title="Teams discussion", text=text,
                               occurred_at="2026-10-01T12:00:00+00:00",
                               sensitivity="personal", chunks=[Chunk(text=text)])
                archive.push(owner, [Change(id=identity, base_version=0, payload=doc)])
            token = store.credential(owner, "access", 3600, "probe", scope=PLUGIN_SCOPE)

            def call(task="Add the teams section to varyz", path="/mcp/plugin", bearer=token):
                args = {"project": "varyz"}
                if task is not None:
                    args["task"] = task
                response = client.post(path, headers={"Authorization": "Bearer " + bearer},
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "start_task", "arguments": args}})
                assert response.status_code == 200
                result = response.json()["result"]
                assert not result.get("isError"), result
                return json.loads(result["content"][0]["text"])["history_check"]

            check = call()
            assert check["status"] == "checked"
            assert [item["id"] for item in check["evidence"]] == ["teams"]
            assert f"Teams for {owner}" in check["evidence"][0]["text"]
            assert call(None)["status"] == "task_required"
            archive.control(owner, True)
            assert call()["status"] == "paused"
            archive.control(owner, False)
            archive.push(owner, [Change(id="teams", base_version=1, payload=None)])
            assert call()["evidence"] == []
            legacy = store.credential(owner, "access", 3600, "probe", scope="varyz")
            assert call(path="/mcp", bearer=legacy)["status"] == "access_required"
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM questions").fetchone()[0] == 0
        print(json.dumps({"passed": True, "accounts": 2, "owner_data_used": False,
                          "isolation": True, "long_excerpt": True, "pause_revoke": True,
                          "legacy_grants_unchanged": True, "questions_created": 0}))


if __name__ == "__main__":
    main()
