"""Offline archive privacy, isolation, lifecycle, search and sync contract tests."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import MODEL, READ_SCOPE, Archive, Change, Chunk, Document
from varyz.cloud.archive_sync import ArchiveSync, allow_project, approve, read_archive_journal
from varyz.cloud.client import CloudClient
from varyz.cloud.store import QuotaExceeded


def payload(identity="one", text="We chose SQLite for offline project memory.", project="varyz"):
    return Document(
        id=identity,
        project=project,
        source="documents",
        external_id="notes/decision.txt",
        title="Database decision",
        text=text,
        occurred_at="2026-10-01T12:00:00+00:00",
        sensitivity="personal",
        chunks=[Chunk(text=text)],
    ).model_dump()


@pytest.fixture
def hosted(tmp_path):
    app = create_app(
        Settings(
            database=str(tmp_path / "cloud.db"),
            archive_limit=2_000_000,
            public_url="http://localhost",
        )
    )
    store = app.state.store
    with store.connection() as db:
        for name, status in (("alice", "active"), ("bob", "active"), ("unpaid", "inactive")):
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
                (name, name + "@example.test", "not-used", status),
            )
    client = TestClient(app, base_url="http://localhost")

    def auth(name="alice", client_id="varyz-local", readonly=False):
        token = store.credential(
            name, "access", 3600, client_id, scope=READ_SCOPE if readonly else "varyz"
        )
        return {"Authorization": "Bearer " + token}

    return client, store, auth


def push(client, auth, data=None, version=0, identity="one"):
    return client.post(
        "/api/archive/sync",
        headers=auth,
        json={
            "records": [
                {"id": identity, "base_version": version, "payload": data or payload(identity)}
            ]
        },
    )


def mcp(client, auth, name, arguments):
    return client.post(
        "/mcp/archive",
        headers=auth,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def test_readonly_connector_and_legacy_credentials(hosted):
    client, _, auth = hosted
    local = auth()
    assert push(client, local).status_code == 200
    reader = auth(client_id="chatgpt", readonly=True)
    value = mcp(client, reader, "search", {"query": "offline SQLite"}).json()["result"]
    assert value["structuredContent"]["results"][0]["id"] == "one"
    assert json.loads(value["content"][0]["text"]) == value["structuredContent"]
    assert push(client, reader).status_code == 403
    assert client.post("/api/sync", headers=reader, json={"records": []}).status_code == 403
    assert client.get("/api/export", headers=reader).status_code == 403
    assert mcp(client, reader, "remember_owner_rule", {"statement": "bad"}).json()["result"][
        "isError"
    ]
    assert mcp(client, auth(client_id="legacy"), "search", {"query": "SQLite"}).status_code == 403
    assert client.get("/archive/one", headers=auth(client_id="legacy")).status_code == 403
    assert client.get("/archive/one", headers=reader).status_code == 200
    listed = client.post(
        "/mcp",
        headers=reader,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        },
    ).json()["result"]["tools"]
    assert {t["name"] for t in listed} == {"search", "fetch", "session_history"}
    assert all(t["annotations"]["readOnlyHint"] for t in listed)


def test_tenant_project_dates_source_context_and_citation(hosted):
    client, _, auth = hosted
    assert push(client, auth()).status_code == 200
    assert (
        push(client, auth(), payload("other", project="memoir"), identity="other").status_code
        == 200
    )
    bob = auth("bob", "chatgpt", readonly=True)
    assert (
        mcp(client, bob, "search", {"query": "SQLite"}).json()["result"]["structuredContent"][
            "results"
        ]
        == []
    )
    assert mcp(client, bob, "fetch", {"id": "one"}).json()["result"]["isError"]
    reader = auth(client_id="chatgpt", readonly=True)
    result = mcp(client, reader, "search", {"query": "SQLite", "project": "varyz"}).json()["result"]
    assert [r["id"] for r in result["structuredContent"]["results"]] == ["one"]
    assert mcp(client, reader, "fetch", {"id": "one", "project": "memoir"}).json()["result"][
        "isError"
    ]
    result = mcp(client, reader, "fetch", {"id": "one"}).json()["result"]["structuredContent"]
    assert result["text"] == payload()["text"]
    assert result["metadata"]["provenance"] == "external"
    assert "synced_at" in result["metadata"]
    assert result["url"].endswith("/archive/one")
    assert client.get("/archive/one", headers=bob).status_code == 404
    assert client.get("/archive/one", headers=reader).status_code == 200
    assert client.get("/archive/one").status_code == 401
    result = mcp(client, reader, "search", {"query": "SQLite", "before": "2026-09-01T00:00:00Z"})
    assert result.json()["result"]["structuredContent"]["results"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("sensitivity", "sensitive"),
        ("sensitivity", "unclassified"),
        ("source", "whatsapp"),
        ("source", "telegram"),
        ("occurred_at", "2026-10-01"),
        ("private_extra", "secret"),
    ],
)
def test_fail_closed_payload_validation(hosted, field, value):
    client, _, auth = hosted
    data = payload()
    data[field] = value
    response = push(client, auth(), data)
    assert response.status_code == 422
    assert "secret" not in response.text


def test_credentials_extra_chunks_and_vector_validation(hosted):
    client, _, auth = hosted
    data = payload()
    # Repeat a character so the publication gate can identify this as synthetic.
    data["text"] = "AWS secret AKIA" + "Q" * 16
    data["chunks"] = [{"text": data["text"], "vector": None}]
    assert push(client, auth(), data).status_code == 422
    data = payload()
    data["chunks"][0]["text"] = "Text absent from approved source"
    assert push(client, auth(), data).status_code == 422
    data = payload()
    data["model"] = MODEL
    data["chunks"][0]["vector"] = [1.0]
    assert push(client, auth(), data).status_code == 422
    assert push(client, auth("unpaid")).status_code == 402


def test_idempotency_update_conflict_delete_and_quota_rollback(hosted):
    client, store, auth = hosted
    headers = auth()
    assert push(client, headers).json() == push(client, headers).json()
    assert push(client, headers, payload(text="Different approved decision")).status_code == 409
    assert (
        push(client, headers, payload(text="Use PostgreSQL for distributed storage"), 1).status_code
        == 200
    )
    deletion = {"id": "one", "base_version": 2, "payload": None}
    assert (
        client.post("/api/archive/sync", headers=headers, json={"records": [deletion]}).status_code
        == 200
    )
    assert (
        mcp(client, headers, "search", {"query": "SQLite"}).json()["result"]["structuredContent"][
            "results"
        ]
        == []
    )
    assert mcp(client, headers, "fetch", {"id": "one"}).json()["result"]["isError"]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM archive_postings").fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) AS n FROM archive_chunks").fetchone()["n"] == 0
        assert (
            "SQLite"
            not in db.execute("SELECT payload FROM archive_documents").fetchone()["payload"]
        )
    archive = Archive(store, "http://localhost", quota=100)
    with pytest.raises(Exception, match="quota"):
        archive.push(
            "alice",
            [Change(id="new", base_version=0, payload=Document.model_validate(payload("new")))],
        )
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_documents WHERE id='new'").fetchone()["n"]
            == 0
        )


def local_entry(conn, identity="local-one", sensitivity="personal"):
    conn.execute(
        "INSERT INTO entries(id,kind,source,external_id,content_hash,title,body,ingested_at,"
        "occurred_start,sensitivity) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            "document",
            "documents",
            identity,
            "h",
            "Archive decision",
            "SQLite keeps memory offline",
            "2026-10-01T00:00:00Z",
            "2026-10-01T00:00:00Z",
            sensitivity,
        ),
    )
    conn.commit()


def test_full_archive_still_accepts_removals_when_remaining_data_is_over_limit(hosted):
    _client, store, _auth = hosted
    archive = Archive(store, "http://localhost", quota=1_000_000)
    for identity in ("one", "two"):
        archive.push("alice", [Change(id=identity, base_version=0,
                                     payload=Document.model_validate(payload(identity)))])
    archive.quota = 1
    archive.push("alice", [Change(id="one", base_version=1, payload=None)])
    assert archive.fetch("alice", "two")["text"]
    with pytest.raises(QuotaExceeded):
        archive.push("alice", [Change(id="new", base_version=0,
                                     payload=Document.model_validate(payload("new")))])
    with pytest.raises(KeyError):
        archive.fetch("alice", "new")


def test_explicit_approval_edit_revocation_and_pause(conn, tmp_path):
    local_entry(conn)
    path = tmp_path / "archive.json"
    sync = ArchiveSync(conn, path)
    assert sync.preview()["approved"] == 0
    approve(conn, path, ["local-one"], "varyz")
    assert sync.preview()["uploads"] == 1
    assert "SQLite" not in path.read_text()
    conn.execute("UPDATE entries SET body='Changed text' WHERE id='local-one'")
    conn.commit()
    assert sync.preview()["uploads"] == 0
    assert sync.preview()["blocked_ids"] == ["local-one"]
    assert sync.control("pause")["enabled"] is False
    with pytest.raises(ValueError, match="paused"):
        sync.sync(None, "alice")


def test_sync_end_to_end_resume_and_account_binding(conn, tmp_path, hosted):
    local_entry(conn)
    local_entry(conn, "local-two")
    path = tmp_path / "archive.json"
    approve(conn, path, ["local-one", "local-two"], "varyz")
    testclient, store, auth = hosted
    token = auth()["Authorization"].split()[1]
    calls = 0

    def transport(request):
        nonlocal calls
        calls += request.url.path == "/api/archive/sync"
        if calls == 2 and request.url.path == "/api/archive/sync":
            raise httpx.ConnectError("interrupted")
        response = testclient.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, content=response.content)

    client = CloudClient(
        "http://localhost",
        {"access_token": token},
        http=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    sync = ArchiveSync(conn, path)
    with pytest.raises(ValueError):
        sync.sync(client, "alice", batch_size=1)
    assert len(read_archive_journal(path)["records"]) == 1
    assert sync.sync(client, "alice")["uploaded"] == 1
    assert sync.sync(client, "alice")["uploaded"] == 0
    with pytest.raises(ValueError, match="another"):
        sync.sync(client, "bob")
    sync.control("revoke", ["local-one"])
    assert sync.sync(client, "alice")["deleted"] == 1
    sync.control("resume")
    approve(conn, path, ["local-one"], "varyz")
    assert sync.sync(client, "alice")["uploaded"] == 1
    assert store.lookup(token, "access")


def test_oauth_read_scope_survives_pkce_and_refresh(hosted):
    import base64
    import hashlib

    client, store, _ = hosted
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    with store.connection() as db:
        db.execute(
            "INSERT INTO clients VALUES(?,?)", ("chatgpt", '["https://example.test/callback"]')
        )
    code = store.credential(
        "alice",
        "code",
        300,
        "chatgpt",
        "https://example.test/callback",
        challenge,
        scope=READ_SCOPE,
    )
    tokens = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "chatgpt",
            "code": code,
            "redirect_uri": "https://example.test/callback",
            "code_verifier": verifier,
        },
    ).json()
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "chatgpt",
            "refresh_token": tokens["refresh_token"],
        },
    ).json()
    reader = {"Authorization": "Bearer " + refreshed["access_token"]}
    assert client.get("/api/export", headers=reader).status_code == 403
    assert mcp(client, reader, "search", {"query": "anything"}).status_code == 200
    with store.connection() as db:
        db.execute(
            "UPDATE credentials SET expires=? WHERE hash=?",
            (time.time() - 1, hashlib.sha256(refreshed["access_token"].encode()).hexdigest()),
        )
    assert mcp(client, reader, "search", {"query": "anything"}).status_code == 401


def test_project_approval_future_records_general_and_revocation(conn, tmp_path):
    path = tmp_path / "project-archive.json"
    local_entry(conn, "first")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',body=? WHERE id='first'",
        ("From: me\nConversation: Codex — varyz\n\nKeep memory local",),
    )
    conn.commit()
    allow_project(path, "varyz")
    worker = ArchiveSync(conn, path)
    assert [c["id"] for c in worker.plan()["changes"]] == ["first"]
    local_entry(conn, "future")
    local_entry(conn, "general")
    local_entry(conn, "other")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',body=? WHERE id='future'",
        ("From: me\nConversation: Codex — varyz\n\nNew design decision",),
    )
    conn.execute(
        "UPDATE entries SET source='ai_conversations',body=? WHERE id='other'",
        ("From: me\nConversation: Codex — memoir\n\nUnapproved project",),
    )
    conn.commit()
    assert {c["id"] for c in worker.plan()["changes"]} == {"first", "future"}
    allow_project(path, "General")
    assert {c["id"] for c in worker.plan()["changes"]} == {"first", "future", "general"}
    conn.execute("UPDATE entries SET sensitivity='sensitive' WHERE id='future'")
    conn.commit()
    assert "future" in worker.plan()["blocked_ids"]
    worker.control("revoke", project="varyz")
    assert {c["id"] for c in worker.plan()["changes"]} == {"general"}


def test_document_root_boundary_and_nested_repository(conn, tmp_path):
    root = tmp_path / "varyz"
    root.mkdir()
    other = tmp_path / "varyz-other"
    other.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    for identity, file in (
        ("inside", root / "README.md"),
        ("outside", other / "README.md"),
        ("nested", nested / "README.md"),
    ):
        local_entry(conn, identity)
        conn.execute("UPDATE entries SET external_id=? WHERE id=?", (str(file), identity))
    conn.commit()
    path = tmp_path / "archive.json"
    allow_project(path, "varyz", root)
    assert {c["id"] for c in ArchiveSync(conn, path).plan()["changes"]} == {"inside"}


def test_encryption_readback_hashed_index_and_kill_switch(hosted):
    from cryptography.fernet import Fernet

    _, store, _ = hosted
    key = Fernet.generate_key().decode()
    archive = Archive(store, "http://localhost", encryption_key=key)
    data = Document.model_validate(payload())
    archive.push("alice", [Change(id=data.id, base_version=0, payload=data)])
    with store.connection() as db:
        encoded = db.execute("SELECT payload FROM archive_documents").fetchone()["payload"]
        assert "SQLite" not in encoded and encoded.startswith("encrypted:")
        terms = [r["term"] for r in db.execute("SELECT term FROM archive_postings")]
        assert "sqlite" not in terms
    assert archive.fetch("alice", "one")["text"] == data.text
    assert archive.search("alice", "SQLite")["results"][0]["id"] == "one"
    assert archive.search("bob", "SQLite")["results"] == []
    archive.control("alice", True)
    assert archive.search("alice", "SQLite")["paused"] is True
    with pytest.raises(KeyError):
        archive.fetch("alice", "one")
    archive.control("alice", False)
    assert archive.fetch("alice", "one")["text"] == data.text
    archive.push("alice", [Change(id="one", base_version=1, payload=None)])
    assert archive.search("alice", "SQLite")["results"] == []


def test_semantic_vectors_are_local_and_missing_model_falls_back(hosted):
    _, store, _ = hosted

    class Embedder:
        def embed_query(self, text):
            return [1.0] + [0.0] * 383

    doc = Document.model_validate(payload())
    doc.model = MODEL
    doc.chunks[0].vector = [1.0] + [0.0] * 383
    archive = Archive(store, "http://localhost", embedder=Embedder())
    archive.push("alice", [Change(id=doc.id, base_version=0, payload=doc)])
    assert archive.search("alice", "persistent recollection")["results"][0]["id"] == "one"
    assert archive.search("bob", "persistent recollection")["results"] == []

    class Broken:
        def embed_query(self, text):
            raise RuntimeError("Local model missing")

    archive.embedder = Broken()
    assert archive.search("alice", "SQLite")["search_mode"] == "keyword_semantic_unavailable"


def test_github_repository_owner_and_entry_revocation(conn, tmp_path):
    path = tmp_path / "github-archive.json"
    allow_project(path, "varyz", repository="approved/varyz")
    allow_project(path, "General")
    for identity, thread in (
        ("allowed", "gh:approved/varyz#1"),
        ("other-owner", "gh:other/varyz#1"),
    ):
        local_entry(conn, identity)
        conn.execute(
            "UPDATE entries SET source='github',thread_id=? WHERE id=?", (thread, identity)
        )
    conn.commit()
    worker = ArchiveSync(conn, path)
    assert {c["id"] for c in worker.plan()["changes"]} == {"allowed"}
    worker.control("revoke", identities=["allowed"])
    assert not worker.plan()["changes"]


def test_archive_cli_pause_resume_reaches_cloud(conn, tmp_path, monkeypatch):
    import contextlib

    from typer.testing import CliRunner

    from varyz.cloud import archive_cli

    config = tmp_path / "varyz.toml"
    connection_path = tmp_path / "connection.json"
    config.write_text("")
    calls = []

    class Client:
        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return kwargs["json"]

    @contextlib.contextmanager
    def connected(_):
        yield config, connection_path, Client()

    monkeypatch.setattr(archive_cli, "_paths", lambda _: (config, connection_path))
    monkeypatch.setattr(archive_cli, "_connected", connected)
    monkeypatch.setattr(
        archive_cli, "load_config", lambda _: type("Config", (), {"db_path": "x"})()
    )

    # The fixture connection remains owned by the test.
    class Connection:
        def execute(self, *args, **kwargs):
            return conn.execute(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(archive_cli, "open_db", lambda _: Connection())
    runner = CliRunner()
    for action, paused in (("pause", True), ("resume", False)):
        result = runner.invoke(archive_cli.app, ["control", action])
        assert result.exit_code == 0, result.output
        assert calls[-1] == ("POST", "/api/archive/control", {"json": {"paused": paused}})


def test_archive_discovery_requires_only_read_scope(hosted):
    client, _, _ = hosted
    response = client.post("/mcp/archive", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].endswith('/mcp/archive"')
    metadata = client.get("/.well-known/oauth-protected-resource/mcp/archive").json()
    assert metadata["scopes_supported"] == [READ_SCOPE]


def test_production_uploads_fail_closed_without_storage_key(tmp_path):
    app = create_app(
        Settings(database=str(tmp_path / "secure.db"), public_url="https://cloud.example.test")
    )
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('alice','alice@example.test','unused','active',1)"
        )
    token = store.credential("alice", "access", 3600, "varyz-local")
    with TestClient(app, base_url="https://cloud.example.test") as client:
        response = push(client, {"Authorization": "Bearer " + token})
        assert response.status_code == 503
        with store.connection() as db:
            assert not db.execute("SELECT id FROM archive_documents").fetchall()


def test_draft_and_runtime_dependency_exports_are_reproducible(tmp_path):
    import runpy
    import zipfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    builder = runpy.run_path(str(root / "scripts/plugin/build_archive.py"))
    files = builder["generated"]()
    assert set(files) == {
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "assets/icon.svg",
        "skills/varyz-cloud-archive/SKILL.md",
    }
    for name, content in files.items():
        assert (root / "plugins/chatgpt-archive" / name).read_bytes() == content
    config = json.loads(files[".mcp.json"])
    assert config["mcpServers"]["varyz-history"]["url"].endswith("/mcp/context")
    builder["archive_zip"](files, tmp_path / "draft.zip")
    with zipfile.ZipFile(tmp_path / "draft.zip") as archive:
        assert set(archive.namelist()) == set(files)
    exporter = runpy.run_path(str(root / "scripts/export_cloud_requirements.py"))
    for search, name in ((False, "requirements.txt"), (True, "requirements-search.txt")):
        result = exporter["export"](search)
        assert (root / "deploy/cloud" / name).read_text() == result
        assert "cryptography==" in result
        assert ("fastembed==" in result) is search
        assert "openai==" not in result and "anthropic==" not in result


def test_general_is_a_category_in_retrieval_results(hosted):
    client, _, auth = hosted
    assert push(client, auth(), payload(project="General")).status_code == 200
    reader = auth(client_id="chatgpt", readonly=True)
    value = mcp(client, reader, "search", {"query": "SQLite", "project": "General"})
    assert value.json()["result"]["structuredContent"]["results"][0]["project"] == "General"
    value = mcp(client, reader, "fetch", {"id": "one"})
    assert value.json()["result"]["structuredContent"]["metadata"]["project"] == "General"


def test_mcp_arguments_are_validated_without_relying_on_client_schema(hosted):
    client, _, auth = hosted
    reader = auth(client_id="chatgpt", readonly=True)
    assert push(client, auth()).status_code == 200
    for args in (
        {"query": "SQLite", "limit": 1.5},
        {"query": "SQLite", "limit": True},
        {"query": "SQLite", "before": 7},
        {"query": "SQLite", "project": []},
    ):
        response = mcp(client, reader, "search", args)
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
    response = mcp(client, reader, "fetch", {"id": ["one"]})
    assert response.json()["result"]["isError"] is True
