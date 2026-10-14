"""Isolated synthetic archive contract and bounded search load in the candidate image."""

import json
import resource
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import CONTEXT_SCOPE, MODEL, PLUGIN_SCOPE, Archive, CachedEmbedder, Change
from varyz.cloud.store import QuotaExceeded


def main():
    with tempfile.TemporaryDirectory() as folder:
        key = Fernet.generate_key().decode()
        embedder = CachedEmbedder("/opt/varyz/models")
        vector = embedder.embed_query("We chose SQLite so the project works offline")
        app = create_app(
            Settings(
                database=str(Path(folder) / "test.sqlite"),
                public_url="http://localhost",
                archive_encryption_key=key,
                archive_embedder=embedder,
            )
        )
        store = app.state.store
        with store.connection() as db:
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) "
                "VALUES('probe','probe@example.test','unused','active',1)"
            )
        archive = Archive(store, "http://localhost", embedder=embedder, encryption_key=key)
        document = dict(
            id="one",
            project="demo",
            source="documents",
            external_id="demo.md",
            title="Offline database decision",
            text="We chose SQLite so the project works offline",
            occurred_at="2026-10-01T12:00:00+00:00",
            sensitivity="personal",
            model=MODEL,
            chunks=[dict(text="We chose SQLite so the project works offline", vector=vector)],
        )
        archive.push("probe", [Change(id="one", base_version=0, payload=document)])
        token = store.credential("probe", "access", 3600, "chatgpt", scope=CONTEXT_SCOPE)
        headers = {"Authorization": "Bearer " + token}
        with TestClient(app, base_url="http://localhost") as client:
            plugin = {
                "Authorization": "Bearer "
                + store.credential("probe", "access", 3600, "plugin-probe", scope=PLUGIN_SCOPE)
            }
            listing = client.post(
                "/mcp/plugin",
                headers=plugin,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert listing.status_code == 200
            assert len(listing.json()["result"]["tools"]) == 15
            assert (
                client.post("/api/archive/sync", headers=plugin, json={"records": []}).status_code
                == 403
            )
            assert client.get("/api/export", headers=plugin).status_code == 403
            assert (
                client.post(
                    "/mcp/plugin",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ).status_code
                == 403
            )
            for name, arguments in [
                ("search", {"query": "How does this work without internet?"}),
                ("fetch", {"id": "one"}),
                ("context", {"project": "demo"}),
                ("timeline", {"project": "demo"}),
                ("compare", {"id": "one"}),
            ]:
                response = client.post(
                    "/mcp/context",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200, name
                assert not response.json()["result"].get("isError"), name
            assert client.get("/api/export", headers=headers).status_code == 403
        # Thousands of synthetic vectors test the bounded scan on server CPU/RAM.
        count = 0
        for index in range(355):
            identity = "load-" + str(index)
            text = "Synthetic database project decision offline memory. " * 100
            doc = dict(
                document,
                id=identity,
                text=text,
                chunks=[dict(text=text[:100], vector=vector) for _ in range(56)],
            )
            try:
                archive.push("probe", [Change(id=identity, base_version=0, payload=doc)])
            except QuotaExceeded:
                break
            count += 56
        durations = []
        for _ in range(5):
            start = time.monotonic()
            assert archive.search("probe", "SQLite offline database")["results"]
            durations.append(time.monotonic() - start)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        assert peak < 1200 * 1024**2, peak
        assert max(durations) < 15, durations
        archive.revoke("probe", "demo")
        assert archive.export("probe")["records"] == []
        print(
            json.dumps(
                {
                    "contract": "passed",
                    "synthetic_chunks": count,
                    "search_seconds": durations,
                    "peak_rss_bytes": peak,
                    "model": MODEL,
                    "production_data_uploaded": False,
                }
            )
        )


if __name__ == "__main__":
    main()
