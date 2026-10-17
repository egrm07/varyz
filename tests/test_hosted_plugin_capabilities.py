# ruff: noqa: F811
"""Packaged Cloud routes exercise real synthetic-account MCP behavior and grants."""

import base64
import hashlib
import json
import runpy
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_cloud_archive import hosted, payload  # noqa: F401
from varyz.cloud.archive import CONTEXT_SCOPE, MODEL, PLUGIN_SCOPE, Archive, Change, Chunk, Document

ROOT = Path(__file__).resolve().parents[1]


def rpc(client, headers, path, method, name=None, args=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if name:
        message["params"] = {"name": name, "arguments": args or {}}
    return client.post(path, headers=headers, json=message)


@pytest.mark.parametrize("package", ["chatgpt", "claude-cloud"])
def test_packaged_routes_cover_preferences_and_history_without_widening_grants(hosted, package):
    client, store, auth = hosted
    config = json.loads((ROOT / "plugins" / package / ".mcp.json").read_text())
    paths = {key: urlsplit(value["url"]).path for key, value in config["mcpServers"].items()}
    preferences = {
        "Authorization": "Bearer "
        + store.credential("alice", "access", 3600, package, scope=PLUGIN_SCOPE)
    }
    history = preferences
    assert paths == {"varyz": "/mcp/plugin"}
    expected = {
        "route_task",
        "start_task",
        "ask",
        "finish_check",
        "away",
        "owner_questions",
        "answer_owner_question",
        "remember_owner_rule",
        "remember_owner_workflow",
        "context",
        "search",
        "fetch",
        "timeline",
        "compare",
        "session_history",
        "record_progress",
        "claim_work",
    }
    assert rpc(client, preferences, paths["varyz"], "initialize").status_code == 200
    listed = rpc(client, preferences, paths["varyz"], "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == expected
    for tool in listed:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["openWorldHint"] is False
        if tool["name"] in {"context", "search", "fetch", "timeline", "compare", "session_history"}:
            assert tool["annotations"]["readOnlyHint"]
        if tool["name"] in {"remember_owner_rule", "remember_owner_workflow"}:
            assert tool["annotations"]["destructiveHint"]

    saved = rpc(
        client,
        preferences,
        paths["varyz"],
        "tools/call",
        "remember_owner_rule",
        {"statement": "Use short replies", "scope": "varyz"},
    )
    assert not saved.json()["result"].get("isError")
    started = rpc(
        client, preferences, paths["varyz"], "tools/call", "start_task", {"project": "varyz"}
    ).json()["result"]
    assert "Use short replies" in json.dumps(started)

    archive = Archive(store, "http://localhost")
    archive.push("alice", [Change(id="one", base_version=0, payload=payload())])
    archive.push(
        "alice",
        [
            Change(
                id="one",
                base_version=1,
                payload=payload(text="We now chose PostgreSQL for shared memory."),
            )
        ],
    )
    for tool, args, evidence in (
        ("context", {"project": "varyz"}, "Use short replies"),
        ("search", {"project": "varyz", "query": "PostgreSQL"}, "one"),
        ("fetch", {"id": "one"}, "PostgreSQL"),
        ("timeline", {"project": "varyz"}, "one"),
        ("compare", {"id": "one"}, "SQLite"),
    ):
        result = rpc(client, history, paths["varyz"], "tools/call", tool, args)
        assert evidence in json.dumps(result.json()["result"]["structuredContent"])
    for legacy in (
        auth(client_id=package),
        auth(readonly=True),
        {
            "Authorization": "Bearer "
            + store.credential("alice", "access", 3600, package, scope=CONTEXT_SCOPE)
        },
    ):
        assert rpc(client, legacy, paths["varyz"], "tools/list").status_code == 403
    for path in ("/mcp", "/mcp/context", "/mcp/archive"):
        assert rpc(client, preferences, path, "tools/list").status_code == 403
    assert client.get("/archive/one", headers=preferences).status_code == 200
    for path in ("/api/export", "/api/account"):
        assert client.get(path, headers=preferences).status_code in (403, 405)
    assert client.delete("/api/account", headers=preferences).status_code == 403
    assert client.post("/api/sync", headers=preferences, json={"records": []}).status_code == 403
    assert (
        client.post("/api/archive/sync", headers=history, json={"records": []}).status_code == 403
    )
    bob = {
        "Authorization": "Bearer "
        + store.credential("bob", "access", 3600, package, scope=PLUGIN_SCOPE)
    }
    assert rpc(client, bob, paths["varyz"], "tools/call", "fetch", {"id": "one"}).json()["result"][
        "isError"
    ]
    archive.revoke("alice", "varyz")
    after = rpc(
        client, history, paths["varyz"], "tools/call", "search", {"query": "PostgreSQL"}
    ).json()["result"]["structuredContent"]
    assert after["results"] == []


def test_plugin_discovery_precedes_oauth_without_exposing_account_context(hosted):
    client, store, _ = hosted
    private = "private-account-context-canary"
    Archive(store, "http://localhost").push("alice", [Change(
        id="private-record", base_version=0, payload=payload("private-record", text=private)
    )])
    initialized = rpc(client, {}, "/mcp/plugin", "initialize")
    assert initialized.status_code == 200
    assert "varyz-cloud" in initialized.text and private not in initialized.text
    listed = rpc(client, {}, "/mcp/plugin", "tools/list")
    assert listed.status_code == 200 and private not in listed.text
    tools = listed.json()["result"]["tools"]
    assert len(tools) == 17
    for tool in tools:
        expected = [{"type": "oauth2", "scopes": [PLUGIN_SCOPE]}]
        assert tool["securitySchemes"] == expected
        assert tool["_meta"]["securitySchemes"] == expected
        blocked = rpc(client, {}, "/mcp/plugin", "tools/call", tool["name"],
                      {"project": "varyz", "id": "private-record", "statement": private})
        assert blocked.status_code == 200
        result = blocked.json()["result"]
        assert result["isError"] and private not in blocked.text
        challenge = result["_meta"]["mcp/www_authenticate"][0]
        assert '/oauth-protected-resource/mcp/plugin"' in challenge
        assert 'scope="varyz.plugin"' in challenge
        assert 'error="invalid_token"' in challenge and 'error_description=' in challenge
        assert blocked.headers["www-authenticate"] == challenge
    for path in ("/mcp", "/mcp/context", "/mcp/archive"):
        assert rpc(client, {}, path, "tools/list").status_code == 401
    stale = {"Authorization": "Bearer expired-secret-canary"}
    assert rpc(client, stale, "/mcp/plugin", "tools/list").status_code == 401
    denied = rpc(client, stale, "/mcp/plugin", "tools/call", "start_task", {})
    assert denied.json()["result"]["isError"] and "expired-secret-canary" not in denied.text
    source = Archive(store, "http://localhost").fetch("alice", "private-record")
    assert source["text"] == private
    notifications = client.post("/mcp/plugin", json={
        "jsonrpc": "2.0", "method": "notifications/initialized"
    })
    assert notifications.status_code == 202


@pytest.mark.parametrize("package", ["chatgpt", "claude-cloud"])
def test_plugin_accounts_cannot_read_or_change_each_other_even_with_shared_ids(hosted, package):
    client, store, _ = hosted
    archive = Archive(store, "http://localhost")
    headers = {}
    markers = {"alice": "ALICE_PRIVATE_CANARY", "bob": "BOB_PRIVATE_CANARY"}
    questions = {}
    for owner, marker in markers.items():
        headers[owner] = {"Authorization": "Bearer " + store.credential(
            owner, "access", 3600, package, scope=PLUGIN_SCOPE
        )}
        store.push(owner, [{"kind": "rule", "id": "shared-rule", "base_version": 0,
                            "payload": {"statement": marker, "scope": "varyz"}}])
        questions[owner] = store.queue_question(owner, marker + " question?", "varyz")
        for version in range(2):
            archive.push(owner, [Change(id="shared-doc", base_version=version,
                payload=payload("shared-doc", text=f"SQLite {marker} version {version}."))])
        archive.push(owner, [Change(id=owner + "-only", base_version=0,
            payload=payload(owner + "-only", text=f"SQLite {marker} exclusive."))])
    for owner, marker in markers.items():
        other = "bob" if owner == "alice" else "alice"
        for tool, args in (
            ("start_task", {"project": "varyz"}),
            ("finish_check", {"project": "varyz"}),
            ("owner_questions", {}),
            ("context", {"project": "varyz"}),
            ("search", {"query": "SQLite"}),
            ("timeline", {"project": "varyz"}),
            ("fetch", {"id": "shared-doc"}),
            ("compare", {"id": "shared-doc"}),
        ):
            result = rpc(client, headers[owner], "/mcp/plugin", "tools/call", tool, args)
            assert result.status_code == 200
            assert marker in result.text, tool
            assert markers[other] not in result.text, tool
            spoofed = rpc(client, headers[owner], "/mcp/plugin", "tools/call", tool,
                          {**args, "account": other, "owner_id": other})
            assert markers[other] not in spoofed.text, tool
        for tool in ("fetch", "compare"):
            denied = rpc(client, headers[owner], "/mcp/plugin", "tools/call", tool,
                         {"id": other + "-only"}).json()["result"]
            assert denied["isError"]
        assert client.get("/archive/" + other + "-only", headers=headers[owner]).status_code == 404
        assert markers[other] not in client.get(
            "/archive/shared-doc", headers=headers[owner]).text
        denied = rpc(client, headers[owner], "/mcp/plugin", "tools/call",
                     "answer_owner_question", {"question_id": questions[other], "answer": "attack"})
        assert denied.json()["result"]["isError"]
        changed = rpc(client, headers[owner], "/mcp/plugin", "tools/call",
                      "remember_owner_rule", {"statement": marker + " updated",
                                              "scope": "varyz", "replaces": "shared-rule"})
        assert not changed.json()["result"].get("isError")
        assert marker not in json.dumps(store.knowledge(other, "varyz"))
        assert markers[other] in json.dumps(store.knowledge(other, "varyz"))


def test_claude_cloud_zip_has_http_routes_and_no_local_runtime_or_loose_secrets(tmp_path):
    builder = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    files = {
        tmp_path / path.relative_to(ROOT): value for path, value in builder["generated"]().items()
    }
    base = tmp_path / "plugins/claude-cloud"
    base.mkdir(parents=True)
    (base / "credentials.env").write_text("SECRET-CANARY")
    builder["hosted_zip"].__globals__["ROOT"] = tmp_path
    target = tmp_path / "cloud.zip"
    builder["hosted_zip"](files, target, "claude-cloud")
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert ".claude-plugin/plugin.json" in names
        assert "skills/varyz-cloud-archive/SKILL.md" in names
        assert "skills/varyz-hosted-context/references/capabilities.md" in names
        assert not any(name.startswith(("hooks/", "runtime/", "install/")) for name in names)
        assert not any("SECRET-CANARY" in archive.read(name).decode() for name in names)
        config = json.loads(archive.read(".mcp.json"))["mcpServers"]
        assert all(
            set(value) == {"type", "url"} and value["type"] == "http" for value in config.values()
        )


@pytest.mark.parametrize("semantic", [False, True])
def test_cloud_retrieval_returns_distinct_sources_despite_chunk_crowding(hosted, semantic):
    _, store, _ = hosted

    class Embedder:
        def embed_query(self, query):
            return [1.0] + [0.0] * 383

    archive = Archive(store, "http://localhost", embedder=Embedder() if semantic else None)
    records = []
    for index in range(6):
        text = f"SQLite project decision {index}."
        doc = Document.model_validate(payload(f"doc{index}", text=text))
        if semantic:
            doc.model = MODEL
        doc.chunks = [
            Chunk(text=text, vector=Embedder().embed_query(text) if semantic else None)
            for _ in range(64 if index == 0 else 1)
        ]
        records.append(Change(id=doc.id, base_version=0, payload=doc))
    archive.push("alice", records)
    result = archive.search("alice", "SQLite project decision", limit=5)
    assert len({row["id"] for row in result["results"]}) == 5
    assert result["search_mode"] == ("hybrid" if semantic else "keyword")
    assert archive.search("bob", "SQLite project decision")["results"] == []


def test_browser_oauth_error_is_clear_closable_and_does_not_echo_request(hosted):
    client, store, auth = hosted
    response = client.get("/oauth/authorize", params={
        "resource": "http://localhost/mcp/plugin", "scope": "varyz",
        "state": "private-state-canary", "redirect_uri": "https://evil.test/callback",
    })
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Varyz did not connect" in response.text
    assert "No access was granted" in response.text
    assert 'id="close-connection"' in response.text
    assert "window.close()" in response.text
    assert "private-state-canary" not in response.text
    assert "evil.test" not in response.text
    assert "location" not in response.headers
    nonce = response.text.split('<script nonce="')[1].split('"')[0]
    assert f"script-src 'nonce-{nonce}'" in response.headers["content-security-policy"]
    script_policy = response.headers["content-security-policy"].split("script-src")[1]
    assert "'unsafe-inline'" not in script_policy
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("requested_scope", [
    PLUGIN_SCOPE,
    "varyz varyz.archive.read varyz.context.read varyz.plugin",
    "varyz.plugin varyz.context.read",
])
def test_plugin_oauth_requires_explicit_resource_and_retains_scope_after_refresh(
    hosted, requested_scope
):
    client, store, auth = hosted
    verifier = "v" * 64
    params = {
        "response_type": "code",
        "client_id": "varyz-local",
        "redirect_uri": "http://127.0.0.1:8766/callback",
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("="),
        "state": "opaque-state",
        "scope": requested_scope,
        "resource": "http://localhost/mcp/plugin",
    }
    client.cookies.set("session", store.credential("alice", "session", 3600))
    # A prior dashboard click authorizes local preferences, never the broader plugin grant.
    client.cookies.set("computer_intent", store.credential("alice", "computer_intent", 600))
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200
    assert "approved synced project history and session reports" in consent.text
    assert "Allow connection" in consent.text
    assert "can also manage billing" not in consent.text
    assert 'name="scope" value="varyz.plugin"' in consent.text
    for resource in ("http://localhost/mcp", ""):
        assert (
            client.get("/oauth/authorize", params={**params, "resource": resource}).status_code
            == 400
        )
    assert client.get("/oauth/authorize", params={**params, "scope": "varyz"}).status_code == 400
    for invalid_scope in ("", "varyz.plugin admin", "varyz.context.read", "varyz.plugin "):
        assert client.get("/oauth/authorize", params={
            **params, "scope": invalid_scope
        }).status_code == 400
    params["csrf"] = client.cookies.get("csrf")
    consent = client.post("/oauth/authorize", data=params, follow_redirects=False)
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "varyz-local",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": params["redirect_uri"],
            "resource": params["resource"],
        },
    ).json()
    assert token["scope"] == PLUGIN_SCOPE
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "varyz-local",
            "refresh_token": token["refresh_token"],
        },
    ).json()
    assert refreshed["scope"] == PLUGIN_SCOPE
    headers = {"Authorization": "Bearer " + refreshed["access_token"]}
    assert rpc(client, headers, "/mcp/plugin", "tools/list").status_code == 200
    assert (
        client.post("/api/archive/sync", headers=headers, json={"records": []}).status_code == 403
    )
    assert client.delete("/api/account", headers=headers).status_code == 403
    assert client.get("/api/export", headers=headers).status_code == 403
    assert (
        client.post(
            "/oauth/revoke",
            data={
                "client_id": "varyz-local",
                "token": refreshed["refresh_token"],
            },
        ).status_code
        == 200
    )
    assert rpc(client, headers, "/mcp/plugin", "tools/list").status_code == 401


@pytest.mark.parametrize("origin", ["http://localhost", "https://cloud.example.test"])
def test_reviewer_seed_and_real_sign_in_consent_are_repeatable_without_owner_data(tmp_path, origin):
    from fastapi.testclient import TestClient

    from varyz.cloud.app import Settings, create_app

    reviewer = runpy.run_path(str(ROOT / "scripts/plugin/reviewer.py"))
    settings = Settings(database=str(tmp_path / "review.db"), public_url=origin)
    first, second = reviewer["seed"](settings), reviewer["seed"](settings)
    assert first["account"] != second["account"]
    app = create_app(settings)
    with app.state.store.connection() as db:
        accounts = db.execute("SELECT * FROM accounts").fetchall()
        assert len(accounts) == 2
        assert first["password"] not in accounts[0]["password"]
    reviewer["oauth"].__globals__["ORIGIN"] = settings.public_url
    with TestClient(app, base_url=settings.public_url, follow_redirects=False) as client:
        _, token = reviewer["oauth"](client, first["email"], first["password"])
        headers = {"Authorization": "Bearer " + token["access_token"]}
        assert "Short, plain replies" in json.dumps(
            rpc(
                client,
                headers,
                "/mcp/plugin",
                "tools/call",
                "ask",
                {"project": "demo", "question": "Which reply style did I choose for demo?"},
            ).json()
        )
        compared = rpc(
            client, headers, "/mcp/plugin", "tools/call", "compare", {"id": "demo-decision"}
        ).json()["result"]["structuredContent"]
        assert "retained-version comparisons" not in compared["original"]["text"]
        assert "retained-version comparisons" in compared["current"]["text"]
