"""Project export isolation and durable pauses across all upload lanes."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tests.test_cloud_archive import payload as archive_payload
from tests.test_cloud_dashboard_loading import seed
from tests.test_cloud_session_reports import report
from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import Change
from varyz.cloud.dashboard import export_project_preferences
from varyz.cloud.project_sync import ProjectSyncPaused
from varyz.cloud.session_reports import Change as ReportChange


@pytest.fixture
def cloud(tmp_path):
    settings = Settings(database=str(tmp_path / "project.sqlite"),
                        archive_encryption_key=Fernet.generate_key().decode(),
                        shared_varyz_enabled=True)
    app = create_app(settings)
    store = app.state.store
    with store.connection() as db:
        for owner in ("owner", "other"):
            db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
                       "VALUES(?,?,?,'active',1)", (owner, owner + "@example.test", "unused"))
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        client.cookies.set("session", store.credential("owner", "session", 3600))
        client.get("/?view=dashboard")
        yield app, client, settings


def action(client, name, project="san7"):
    return client.post("/account/action", data=dict(
        csrf=client.cookies.get("csrf"), action=name, project=project,
    ), follow_redirects=False)


def test_export_all_project_preferences_without_other_projects_or_accounts(cloud):
    app, client, _ = cloud
    store = app.state.store
    seed(store, "owner", "SAN7.14", 0, 32)
    seed(store, "owner", "san7", 32, 2)
    seed(store, "owner", "beta", 40, 1)
    seed(store, "owner", "*", 41, 1)
    seed(store, "other", "san7", 90, 1)
    assert client.post("/account/action", data={
        "action": "export_project", "project": "san7",
    }).status_code == 403
    result = action(client, "export_project", "SAN7.15")
    assert result.status_code == 200
    assert result.headers["content-disposition"].startswith("attachment;")
    assert "no-store" in result.headers["cache-control"]
    value = result.json()
    assert value["project"] == "san7" and value["format"] == "varyz-project-v1"
    assert len(value["records"]) == 34
    assert all(r["payload"]["scope"] == "san7" for r in value["records"])
    assert len(action(client, "export_project", "*").json()["records"]) == 1
    assert export_project_preferences(store, "other", "san7")["records"][0]["id"] == "r-90"


def test_pause_is_durable_scoped_preserves_reads_and_resume_catches_up(cloud):
    app, client, settings = cloud
    store = app.state.store
    seed(store, "owner", "san7", 0, 1)
    seed(store, "owner", "beta", 1, 1)
    assert client.post("/account/action", data={
        "action": "pause_project_sync", "project": "san7",
    }).status_code == 403
    assert action(client, "pause_project_sync", "SAN7.14").status_code == 303
    assert action(client, "pause_project_sync").status_code == 303
    assert store.project_sync.paused("owner") == {"san7"}
    assert store.project_sync.paused("other") == set()
    assert create_app(settings).state.store.project_sync.paused("owner") == {"san7"}
    home = client.get("/?view=dashboard").text
    assert "Resume syncing" in home and "Syncing paused" in home
    assert "View preferences" not in home and "Export project" in home
    assert len(action(client, "export_project").json()["records"]) == 1
    token = store.credential("owner", "access", 3600, "varyz-local")
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/api/project-sync", headers=headers).json() == {
        "paused_projects": ["san7"],
    }
    change = dict(kind="rule", id="r-0", base_version=1,
                  payload=dict(statement="New preference", scope="SAN7.15"))
    paused = client.post("/api/sync", headers=headers, json={"records": [change]})
    assert paused.status_code == 423 and paused.json()["project"] == "san7"
    assert action(client, "export_project").json()["records"][0]["version"] == 1
    seed(store, "owner", "beta", 2, 1)
    seed(store, "other", "san7", 90, 1)
    assert action(client, "resume_project_sync").status_code == 303
    assert client.post("/api/sync", headers=headers, json={"records": [change]}).status_code == 200
    assert action(client, "export_project").json()["records"][0]["version"] == 2


def test_pause_blocks_archive_and_reports_but_keeps_context_and_allows_erasure(cloud):
    app, client, _ = cloud
    reports = app.state.session_reports
    archive = reports.archive
    archive.push("owner", [Change(id="old", base_version=0,
                                  payload=archive_payload("old", project="san7"))])
    reports.permit("owner", "san7")
    reports.push("owner", [ReportChange(id="old", base_version=0,
                                       payload=report("old", project="san7"))])
    action(client, "pause_project_sync")
    assert archive.fetch("owner", "old")["id"] == "old"
    assert reports.fetch("owner", "old")
    with pytest.raises(ProjectSyncPaused):
        archive.push("owner", [Change(id="new", base_version=0,
                                      payload=archive_payload("new", project="san7"))])
    with pytest.raises(ProjectSyncPaused):
        reports.push("owner", [ReportChange(id="new", base_version=0,
                                           payload=report("new", project="san7"))])
    assert archive.status("owner")["paused_projects"] == ["san7"]
    assert reports.status("owner")["paused_projects"] == ["san7"]
    archive.push("owner", [Change(id="old", base_version=1, payload=None)])
    reports.push("owner", [ReportChange(id="old", base_version=1, payload=None)])
    assert archive.status("owner")["records"] == 0
    assert reports.status("owner")["records"] == 0


def test_local_sync_defers_paused_uploads_keeps_journal_and_syncs_other_projects(cloud, tmp_path):
    from tests.test_local_cloud_sync import rule, syncer
    from varyz.cloud.client import CloudClient
    from varyz.db.connection import open_db

    app, http, _ = cloud
    store = app.state.store
    token = store.credential("owner", "access", 3600, "varyz-local")
    client = CloudClient("http://127.0.0.1:8787", {"access_token": token}, http=http)
    conn = open_db(tmp_path / "local.sqlite")
    try:
        rule(conn, "Keep project alpha clear", scope="alpha")
        rule(conn, "Keep project beta clear", scope="beta")
        engine = syncer(conn, tmp_path / "sync.json")
        engine.sync(client)
        before = dict(engine.journal["records"])
        store.project_sync.set_paused("owner", "alpha", True)
        rule(conn, "Use short alpha headings", scope="alpha")
        rule(conn, "Use short beta headings", scope="beta")
        outcome = engine.sync(client)
        assert outcome["uploaded"] == 1
        assert len(export_project_preferences(store, "owner", "alpha")["records"]) == 1
        assert len(export_project_preferences(store, "owner", "beta")["records"]) == 2
        assert all(engine.journal["records"][key] == value for key, value in before.items())
        store.project_sync.set_paused("owner", "alpha", False)
        assert engine.sync(client)["uploaded"] == 1
        assert len(export_project_preferences(store, "owner", "alpha")["records"]) == 2
        assert engine.sync(client)["uploaded"] == 0
    finally:
        conn.close()
