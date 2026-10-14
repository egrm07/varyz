from types import SimpleNamespace

import httpx
import pytest

from tests.test_cloud_archive import local_entry
from varyz.cloud.archive_sync import ArchiveSync, approve, read_archive_journal
from varyz.cloud.client import CloudClient, CloudStorageLimit


def test_archive_replacements_remove_old_records_before_uploading(conn, tmp_path):
    local_entry(conn, "new")
    path = tmp_path / "archive.json"
    approve(conn, path, ["new"], "varyz")
    journal = read_archive_journal(path)
    journal["records"]["old"] = {"version": 1, "deleted": False}
    plan = ArchiveSync(conn, path).plan(journal)
    assert [(c["id"], c["payload"] is None) for c in plan["changes"]] == [
        ("old", True),
        ("new", False),
    ]


@pytest.mark.parametrize("endpoint", ["/api/archive/sync", "/api/reports/sync"])
def test_storage_rejection_is_actionable_and_never_exposes_response(endpoint):
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(413, json={"detail": "private server contents"})
        )
    )
    client = CloudClient("http://localhost", {"access_token": "test"}, http=http)
    with pytest.raises(CloudStorageLimit, match="Cloud storage is full") as error:
        client.request("POST", endpoint, json={"records": []})
    assert "private" not in str(error.value)
    http.close()


def test_setup_retains_safe_storage_failure_reason(tmp_path):
    from varyz.cloud.computer import ComputerConnection

    bridge = ComputerConnection(tmp_path / "config.toml")

    def full(_preview):
        raise CloudStorageLimit("private error text")

    bridge._finish(SimpleNamespace(finish=full), {})
    assert bridge.result == {"state": "sync_failed", "reason": "storage_limit"}
    assert "private" not in bridge.session_path.read_text(encoding="utf-8")


def test_percentage_includes_revisions_and_full_state():
    from varyz.cloud.ui import render_storage

    html = render_storage({"bytes": 60, "revision_bytes": 20, "quota_bytes": 100})
    assert "80% used / 100%" in html
    assert 'value="80.0"' in html
    assert "MB" not in html
    assert "storage is full" in render_storage({"bytes": 110, "quota_bytes": 100})
    assert render_storage({"bytes": 10}) == ""  # Unknown allowance is never shown as 0%.


def test_overall_storage_combines_bytes_and_allowances_in_one_meter():
    from varyz.cloud.ui import render_storage

    html = render_storage(
        {"bytes": 60, "revision_bytes": 20, "quota_bytes": 100},
        {"bytes": 10, "quota_bytes": 50},
        {"bytes": 10, "quota_bytes": 50},
    )
    assert "50% used / 100%" in html
    assert html.count("<progress") == 1
    assert 'aria-label="Overall Cloud storage used"' in html
    assert all(label not in html for label in ("History", "Reports", "Preferences", "MB"))


def test_overall_storage_does_not_hide_a_blocked_upload_allowance():
    from varyz.cloud.ui import render_storage

    html = render_storage(
        {"bytes": 100, "quota_bytes": 100}, {"bytes": 0, "quota_bytes": 100}
    )
    assert "50% used / 100%" in html
    assert "capacity limit" in html and "wait safely" in html
    assert "storage is full" not in html


def test_full_archive_keeps_pending_uploads_and_can_resume(conn, tmp_path):
    from varyz.cloud.archive_sync import fingerprint

    local_entry(conn, "waiting")
    path = tmp_path / "archive.json"
    approve(conn, path, ["waiting"], "varyz")

    class Client:
        url = "http://localhost"
        full = True

        def request(self, method, endpoint, **kwargs):
            if endpoint.endswith("/status"):
                return {"projects": []}
            if self.full:
                raise CloudStorageLimit("full")
            return {
                "records": [
                    dict(id=c["id"], version=1, fingerprint=fingerprint(c["payload"]))
                    for c in kwargs["json"]["records"]
                ]
            }

    client = Client()
    sync = ArchiveSync(conn, path)
    assert sync.sync(client, "owner")["storage_full"] is True
    assert len(sync.plan()["changes"]) == 1
    assert read_archive_journal(path)["records"] == {}
    client.full = False
    assert sync.sync(client, "owner")["uploaded"] == 1
    assert sync.plan()["changes"] == []
    assert not read_archive_journal(path).get("storage_full")


def test_full_preference_store_allows_existing_removals_but_not_new_tombstones(tmp_path):
    from varyz.cloud.store import QuotaExceeded, Store

    store = Store(str(tmp_path / "cloud.sqlite"))
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status) "
            "VALUES('owner','o@example.test','x','active')"
        )
    store.push(
        "owner",
        [
            dict(
                kind="rule",
                id="one",
                base_version=0,
                payload=dict(statement="Use plain language", scope="*"),
            )
        ],
    )
    store.max_storage_bytes = 1
    deleted = store.push("owner", [dict(kind="rule", id="one", base_version=1, payload=None)])
    assert deleted[0]["payload"] is None
    with pytest.raises(QuotaExceeded):
        store.push("owner", [dict(kind="rule", id="unknown", base_version=0, payload=None)])


def test_full_browser_save_keeps_dashboard_available_and_old_data(tmp_path):
    from fastapi.testclient import TestClient

    from varyz.cloud.app import Settings, create_app
    app = create_app(Settings(database=str(tmp_path / 'cloud.sqlite')))
    store = app.state.store
    with store.connection() as db:
        db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
                   "VALUES('owner','o@example.test','x','active',1)")
    store.push('owner', [dict(kind='rule', id='one', base_version=0,
                             payload=dict(statement='Use plain language', scope='*'))])
    store.max_storage_bytes = 1
    with TestClient(app, base_url='http://127.0.0.1:8787') as client:
        client.cookies.set('session', store.credential('owner','session',3600))
        assert client.get('/?view=dashboard').status_code == 200
        response = client.post('/account/action', data=dict(action='save',
            csrf=client.cookies.get('csrf'), kind='rule', id='one', base_version='1',
            statement='Changed text', scope='*'))
        assert response.status_code == 200
        assert 'This change was not saved' in response.text
    assert store.pull('owner',0,20)['records'][0]['payload']['statement'] == 'Use plain language'
