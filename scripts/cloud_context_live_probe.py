"""Run inside the service container; exercise HTTPS with disposable synthetic accounts."""

import json
import secrets

import httpx

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import CONTEXT_SCOPE, MODEL, READ_SCOPE, CachedEmbedder


def main():
    settings = Settings.from_env()
    app = create_app(settings)
    store = app.state.store
    identities = ["context_probe_" + secrets.token_hex(8) for _ in range(2)]
    tokens = []
    for identity in identities:
        with store.connection() as db:
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) "
                "VALUES(?,?,?,'active',1)",
                (identity, identity + "@example.test", "unused"),
            )
        tokens.append(store.credential(identity, "access", 600, "varyz-local"))
    uploader = {"Authorization": "Bearer " + tokens[0]}
    reader = {
        "Authorization": "Bearer "
        + store.credential(identities[0], "access", 600, "phone-probe", scope=CONTEXT_SCOPE)
    }
    old_reader = {
        "Authorization": "Bearer "
        + store.credential(identities[0], "access", 600, "old-probe", scope=READ_SCOPE)
    }
    other_reader = {
        "Authorization": "Bearer "
        + store.credential(identities[1], "access", 600, "other-probe", scope=CONTEXT_SCOPE)
    }
    text = "We chose SQLite so project memory works without internet."
    embedder = CachedEmbedder(settings.archive_embedding_cache)
    vector = embedder.embed_documents([text])[0]
    document = dict(
        id="synthetic-decision",
        project="demo",
        source="documents",
        external_id="synthetic/decision.md",
        title="Offline decision",
        text=text,
        occurred_at="2026-10-01T12:00:00+00:00",
        sensitivity="personal",
        model=MODEL,
        chunks=[dict(text=text, vector=vector)],
    )
    with httpx.Client(base_url=settings.public_url, timeout=30) as client:

        def tool(name, args, headers=reader):
            response = client.post(
                "/mcp/context",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
            response.raise_for_status()
            result = response.json()["result"]
            assert not result.get("isError"), name
            return result["structuredContent"]

        try:
            response = client.post(
                "/api/archive/sync",
                headers=uploader,
                json={"records": [dict(id=document["id"], base_version=0, payload=document)]},
            )
            assert response.status_code == 200
            # Retrieval runs independently of any local archive process.
            found = tool("search", {"query": "Which storage keeps our work usable offline?"})
            assert found["results"][0]["id"] == document["id"]
            assert tool("fetch", {"id": document["id"]})["text"] == text
            assert (
                tool("context", {"project": "demo"})["history"]["records"][0]["id"]
                == document["id"]
            )
            assert tool("timeline", {"project": "demo"})["records"][0]["id"] == document["id"]
            assert tool("search", {"query": "SQLite"}, other_reader)["results"] == []
            assert client.get("/api/export", headers=reader).status_code == 403
            assert (
                client.post(
                    "/mcp/context",
                    headers=old_reader,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ).status_code
                == 403
            )
            document["text"] += " We later added a project timeline."
            document["chunks"] = [dict(text=document["text"], vector=vector)]
            assert (
                client.post(
                    "/api/archive/sync",
                    headers=uploader,
                    json={"records": [dict(id=document["id"], base_version=1, payload=document)]},
                ).status_code
                == 200
            )
            compared = tool("compare", {"id": document["id"]})
            assert compared["comparison_available"] and compared["original"]["text"] == text
            original = client.get(compared["original"]["url"], headers=reader)
            assert original.status_code == 200 and text in original.text
            exported = client.get("/api/export", headers=uploader).json()
            assert len(exported["archive"]["records"]) == 1
            assert len(exported["archive"]["revisions"]) == 1
            assert (
                client.post(
                    "/api/archive/control", headers=uploader, json={"paused": True}
                ).status_code
                == 200
            )
            assert tool("search", {"query": "SQLite"})["paused"]
            assert (
                client.post(
                    "/api/archive/control", headers=uploader, json={"paused": False}
                ).status_code
                == 200
            )
            with store.connection() as db:
                row = db.execute(
                    "SELECT payload FROM archive_documents WHERE account=?", (identities[0],)
                ).fetchone()
                assert row["payload"].startswith("encrypted:") and text not in row["payload"]
            print(
                json.dumps(
                    {
                        "https_context": "passed",
                        "semantic_retrieval": "passed",
                        "isolation": "passed",
                        "export_versions_pause": "passed",
                        "encrypted_storage": "passed",
                        "real_phone_verified": False,
                    }
                )
            )
        finally:
            for token in tokens:
                response = client.delete(
                    "/api/account", headers={"Authorization": "Bearer " + token}
                )
                assert response.status_code == 200
            with store.connection() as db:
                assert not db.execute(
                    "SELECT id FROM accounts WHERE id IN (?,?)", identities
                ).fetchone()
            print("Synthetic accounts and records removed.")


if __name__ == "__main__":
    main()
