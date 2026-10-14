"""The real local ledgers projected through cloud CAS, including offline devices."""

import json

import pytest

from varyz import filesystem
from varyz.cloud.local_sync import (
    LocalSync,
    read_journal,
    save_journal,
    select_confirmed,
    selected_payloads,
)
from varyz.cloud.store import Conflict, Store
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.workflows import SQLiteWorkflowStore

OWNER = Caller(
    agent_id="owner:local",
    display_name="Owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="local",
)
COMMON = dict(caller=OWNER, device_id="device:test", source="owner")


def test_journal_retries_transient_windows_lock_without_losing_previous_value(
    tmp_path, monkeypatch
):
    path = tmp_path / "journal.json"
    previous = {"cursor": 1, "records": {}}
    updated = {"cursor": 2, "records": {}}
    save_journal(path, previous)
    replace = filesystem.os.replace
    attempts = []
    waits = []

    def locked_replace(source, destination):
        attempts.append((source, destination))
        assert json.loads(path.read_text()) == previous
        if len(attempts) < 3:
            error = PermissionError("Temporary scanner lock")
            error.winerror = 5
            raise error
        replace(source, destination)

    monkeypatch.setattr(filesystem.os, "replace", locked_replace)
    monkeypatch.setattr(filesystem.time, "sleep", waits.append)
    save_journal(path, updated)
    assert len(attempts) == 3
    assert waits == [0.05, 0.1]
    assert read_journal(path) == updated
    assert not path.with_suffix(".json.tmp").exists()


def test_journal_persistent_windows_lock_preserves_previous_value_and_staging(
    tmp_path, monkeypatch
):
    path = tmp_path / "journal.json"
    previous = {"cursor": 1, "records": {}}
    updated = {"cursor": 2, "records": {}}
    save_journal(path, previous)
    attempts = []

    def locked_replace(source, destination):
        attempts.append((source, destination))
        error = PermissionError("Persistent lock")
        error.winerror = 5
        raise error

    monkeypatch.setattr(filesystem.os, "replace", locked_replace)
    monkeypatch.setattr(filesystem.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="Persistent lock"):
        save_journal(path, updated)
    assert len(attempts) == 6
    assert read_journal(path) == previous
    assert json.loads(path.with_suffix(".json.tmp").read_text()) == updated


class Transport:
    def __init__(self, path):
        self.store = Store(path)

    def push(self, records):
        return self.store.push("test-account", records)

    def pull(self, cursor):
        return self.store.pull("test-account", cursor, 500)


@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "local.sqlite")
    yield db
    db.close()


def rule(conn, statement="Use short replies", **kwargs):
    return SQLitePrincipleStore(conn).add(statement=statement, **COMMON, **kwargs)


def syncer(conn, path):
    select_confirmed(path)
    return LocalSync(conn, path)


def test_projection_excludes_candidates_learned_workflows_and_provenance(conn):
    rule(conn, provenance={"snippet": "private raw transcript", "secret": "never upload"})
    SQLitePrincipleStore(conn).propose(statement="Unreviewed suggestion", **COMMON)
    steps = [{"step": "Draft"}, {"step": "Review"}]
    SQLiteWorkflowStore(conn).learn(name="Unconfirmed order", steps=steps, **COMMON)
    SQLiteWorkflowStore(conn).record(name="Website", steps=steps, **COMMON)
    SQLiteKnowledgeStore(conn).remember(
        question="Which tone?", answer="Calm", basis=AnswerBasis.EXPLICIT, **COMMON
    )
    payloads = selected_payloads(conn)
    assert len(payloads) == 3
    text = json.dumps(payloads)
    assert "private raw transcript" not in text
    assert "Unreviewed suggestion" not in text
    assert "Unconfirmed order" not in text
    assert "provenance" not in text


def test_project_selection_keeps_global_rules_and_excludes_other_projects(conn):
    rule(conn)
    rule(conn, "Use blue", scope="shop")
    rule(conn, "Use red", scope="other")
    assert len(selected_payloads(conn, ["shop"])) == 2


def test_local_correction_preserves_cloud_identity_for_reviewed_sharing(conn, tmp_path):
    original = rule(conn, "Use short replies", scope="website")
    transport = Transport(tmp_path / "cloud-lineage.sqlite")
    sync = syncer(conn, tmp_path / "lineage.json")
    sync.sync(transport)
    corrected = SQLitePrincipleStore(conn).add(
        statement="Use concise paragraphs", scope="website", supersedes_id=original, **COMMON
    )
    planned = sync.plan()
    assert len(planned) == 1 and planned[0]["id"] == original
    assert (
        planned[0]["base_version"] == 1
        and planned[0]["payload"]["statement"] == "Use concise paragraphs"
    )
    sync.sync(transport)
    records = transport.store.pull("test-account", 0, 20)["records"]
    assert records[-1]["id"] == original and records[-1]["version"] == 2
    assert sync.journal["records"]["rule:" + original]["local_id"] == corrected
    assert sync.plan() == []


def test_sync_is_off_until_explicitly_selected_and_preview_is_counts(conn, tmp_path):
    rule(conn)
    path = tmp_path / "journal.json"
    with pytest.raises(ValueError, match="uploads are off"):
        LocalSync(conn, path).plan()
    preview = syncer(conn, path).preview()
    assert preview["uploads"] == 1
    assert preview["sends_text"] is False
    assert "short replies" not in json.dumps(preview)


def test_two_devices_sync_and_remote_deletion_retracts_local_copy(conn, tmp_path):
    identity = rule(conn)
    transport = Transport(tmp_path / "cloud.sqlite")
    first = syncer(conn, tmp_path / "first.json")
    assert first.sync(transport)["uploaded"] == 1
    second_conn = open_db(tmp_path / "second.sqlite")
    try:
        second = syncer(second_conn, tmp_path / "second.json")
        second.sync(transport)
        assert [p.statement for p in SQLitePrincipleStore(second_conn).current()] == [
            "Use short replies"
        ]
        assert second.plan() == []
        SQLitePrincipleStore(conn).retract(identity, caller=OWNER, device_id="device:test")
        first.sync(transport)
        second.sync(transport)
        assert SQLitePrincipleStore(second_conn).current() == ()
        assert second.plan() == []
    finally:
        second_conn.close()


def test_remote_update_appends_correction_without_echo_upload(conn, tmp_path):
    identity = rule(conn)
    transport = Transport(tmp_path / "cloud.sqlite")
    local = syncer(conn, tmp_path / "journal.json")
    local.sync(transport)
    record = transport.pull(0)["records"][0]
    transport.push(
        [
            dict(
                kind="rule",
                id=identity,
                base_version=1,
                payload={**record["payload"], "statement": "Use detailed replies"},
            )
        ]
    )
    local.sync(transport)
    assert [r.statement for r in SQLitePrincipleStore(conn).current()] == ["Use detailed replies"]
    assert conn.execute("SELECT count(*) FROM owner_principles").fetchone()[0] == 2
    assert local.plan() == []


def test_lost_acknowledgement_retries_without_duplicate_server_versions(conn, tmp_path):
    rule(conn)
    transport = Transport(tmp_path / "cloud.sqlite")
    local = syncer(conn, tmp_path / "journal.json")
    changes = local.plan()
    transport.push(changes)  # server committed, connection dropped before client heard
    local.sync(transport)
    assert len(transport.pull(0)["records"]) == 1
    assert local.plan() == []


def test_conflict_keeps_unacknowledged_local_tombstone_and_journal(conn, tmp_path):
    identity = rule(conn)
    path = tmp_path / "journal.json"
    local = syncer(conn, path)
    transport = Transport(tmp_path / "cloud.sqlite")
    local.sync(transport)
    prior = read_journal(path)
    record = transport.pull(0)["records"][0]
    transport.push(
        [
            dict(
                kind="rule",
                id=identity,
                base_version=1,
                payload={**record["payload"], "statement": "A remote correction"},
            )
        ]
    )
    SQLitePrincipleStore(conn).retract(identity, caller=OWNER, device_id="device:test")
    with pytest.raises(Conflict):
        local.sync(transport)
    assert read_journal(path) == prior
    assert local.plan()[0]["payload"] is None


def test_different_configs_do_not_share_sync_journal(tmp_path):
    from varyz.cloud.cli import _paths

    first = tmp_path / "one.toml"
    second = tmp_path / "two.toml"
    for path in (first, second):
        path.write_text("[storage]\n", encoding="utf-8")
    assert _paths(first)[1] != _paths(second)[1]


def test_independent_duplicate_cloud_rules_never_delete_each_other(conn, tmp_path):
    transport = Transport(tmp_path / "cloud.sqlite")
    transport.push(
        [
            dict(
                kind="rule",
                id=identity,
                base_version=0,
                payload={"statement": "Use short replies", "scope": "*"},
            )
            for identity in ("first", "second")
        ]
    )
    local = syncer(conn, tmp_path / "journal.json")
    local.sync(transport)
    assert len(SQLitePrincipleStore(conn).current()) == 2
    assert local.plan() == []


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Renamed order"},
        {"scope": "shop"},
        {"rationale": "New reason"},
    ],
)
def test_workflow_remote_revisions_replace_previous_identity(conn, tmp_path, change):
    transport = Transport(tmp_path / "cloud.sqlite")
    payload = dict(
        name="Original order",
        steps=[{"step": "Draft"}, {"step": "Review"}],
        scope="*",
        rationale="Old reason",
        domain="",
    )
    transport.push([dict(kind="workflow", id="order", base_version=0, payload=payload)])
    local = syncer(conn, tmp_path / "journal.json")
    local.sync(transport)
    transport.push(
        [dict(kind="workflow", id="order", base_version=1, payload={**payload, **change})]
    )
    local.sync(transport)
    assert local.plan() == []
    assert len(selected_payloads(conn)) == 1
    active = next(iter(selected_payloads(conn).values()))
    assert all(active[k] == v for k, v in change.items())


@pytest.mark.parametrize("policy", ["local", "remote"])
def test_explicit_conflict_resolution_is_usable(conn, tmp_path, policy):
    from varyz.cloud.client import CloudConflict

    class APITransport(Transport):
        def push(self, records):
            try:
                return super().push(records)
            except Conflict as exc:
                raise CloudConflict(exc.record) from exc

    identity = rule(conn)
    transport = APITransport(tmp_path / "cloud.sqlite")
    local = syncer(conn, tmp_path / "journal.json")
    local.sync(transport)
    record = transport.pull(0)["records"][0]
    transport.push(
        [
            dict(
                kind="rule",
                id=identity,
                base_version=1,
                payload={**record["payload"], "statement": "Remote preference"},
            )
        ]
    )
    SQLitePrincipleStore(conn).retract(identity, caller=OWNER, device_id="device:test")
    local.sync(transport, conflict_policy=policy)
    active = SQLitePrincipleStore(conn).current()
    assert len(active) == (1 if policy == "remote" else 0)
    assert local.plan() == []


def test_offline_disconnect_always_disables_local_uploads(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from varyz.cloud import cli
    from varyz.cloud.local_sync import save_journal

    config = tmp_path / "config.toml"
    config.write_text("[storage]\n", encoding="utf-8")
    _, path = cli._paths(config)
    save_journal(
        path,
        dict(
            enabled=True, records={}, cursor=0, url="https://cloud.example.test", account_id="alice"
        ),
    )
    monkeypatch.setattr(cli, "load_tokens", lambda _name: {"access_token": "local-token"})
    deleted = []
    monkeypatch.setattr(cli.keyring, "delete_password", lambda *args: deleted.append(args))

    def unavailable(*_args, **_kwargs):
        raise ValueError("Cloud unavailable")

    monkeypatch.setattr(cli.CloudClient, "request", unavailable)
    result = CliRunner().invoke(cli.app, ["disconnect", "--config", str(config)])
    assert result.exit_code == 0
    assert read_journal(path)["enabled"] is False
    assert "url" not in read_journal(path)
    assert deleted
    assert "could not be confirmed" in result.stdout


def test_selected_credential_text_is_blocked_before_transport(conn, tmp_path):
    SQLiteKnowledgeStore(conn).remember(
        question="Which token?",
        answer="api_key=sk-" + "a" * 48,
        basis=AnswerBasis.EXPLICIT,
        **COMMON,
    )
    local = syncer(conn, tmp_path / "journal.json")
    with pytest.raises(ValueError, match="possible credential"):
        local.plan()


def test_real_api_client_local_projection_and_remote_answer_capture(conn, tmp_path):
    from fastapi.testclient import TestClient

    from varyz.cloud.app import Settings, create_app, password_hash
    from varyz.cloud.client import CloudClient

    app = create_app(Settings(database=str(tmp_path / "service.sqlite")))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,?)",
            ("alice", "alice@example.test", password_hash("correct-password"), "active", 1),
        )
    token = store.credential("alice", "access", 3600)
    http = TestClient(app, base_url="http://127.0.0.1:8787")
    client = CloudClient("http://127.0.0.1:8787", {"access_token": token}, http=http)
    rule(conn)
    local = syncer(conn, tmp_path / "journal.json")
    local.sync(client)
    headers = {"Authorization": "Bearer " + token}
    response = http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "start_task", "arguments": {"project": "shop"}},
        },
    )
    assert response.status_code == 200
    assert "Use short replies" in response.text
    assert local.plan() == []
