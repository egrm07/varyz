# ruff: noqa: F811 -- imported pytest fixture is injected by name.
"""Default-branch checkouts share Cloud project views, with legacy data preserved."""

import json
import re

import pytest

from tests.test_cloud_archive import payload as archive_payload
from tests.test_cloud_dashboard_loading import seed
from tests.test_cloud_project_actions import cloud  # noqa: F401
from tests.test_cloud_session_reports import report
from varyz import scopes
from varyz.cloud.archive import Change
from varyz.cloud.dashboard import export_project_preferences, preference_page, project_summaries
from varyz.cloud.project_sync import ProjectSyncPaused
from varyz.cloud.session_reports import Change as ReportChange
from varyz.cloud.session_reports import Query
from varyz.scopes import normalize_scope


@pytest.mark.parametrize("alias", [
    "varyz-main", "Varyz-Main", "varyz-master", "varyz-main-v2", "varyz-v2-main",
    "varyz-main-master", "SAN7.14-main",
])
def test_checkout_names_have_one_idempotent_project_scope(alias):
    expected = "san7" if alias.startswith("SAN7") else "varyz"
    assert normalize_scope(alias) == expected
    assert normalize_scope(normalize_scope(alias)) == expected


@pytest.mark.parametrize("name", [
    "main", "master", "varyz-feature", "varyz-mainland", "varyz-main-backup",
    "other-varyz", "general-main", "all-projects-main", "org:varyz-main",
    "published:varyz-main", "-main",
])
def test_unrelated_projects_foreign_scopes_and_general_boundaries_are_preserved(name):
    assert normalize_scope(name) == name


def test_existing_aliases_form_one_card_and_all_preferences_remain_exportable(cloud, monkeypatch):
    app, client, _ = cloud
    store = app.state.store
    archive = app.state.session_reports.archive
    reports = app.state.session_reports
    # Seed the spellings exactly as a release before checkout normalization did.
    with monkeypatch.context() as old:
        old.setattr(scopes, "_CHECKOUT_TAIL", re.compile(r"(?!)"))
        for i, name in enumerate(("varyz", "varyz-main", "varyz-master")):
            seed(store, "owner", name, i, 1)
            archive.push("owner", [Change(
                id=f"a-{i}", base_version=0, payload=archive_payload(f"a-{i}", project=name),
            )])
            reports.permit("owner", name)
            reports.push("owner", [ReportChange(
                id=f"p-{i}", base_version=0, payload=report(f"p-{i}", project=name),
            )])
    seed(store, "owner", "beta", 10, 1)
    seed(store, "other", "varyz-main", 20, 1)
    groups = project_summaries(store, "owner")
    assert [(g["scope"], g["count"]) for g in groups] == [("varyz", 3), ("beta", 1)]
    for alias in ("varyz", "varyz-main", "varyz-master"):
        assert len(preference_page(store, "owner", alias)["records"]) == 3
        exported = export_project_preferences(store, "owner", alias)
        assert exported["project"] == "varyz"
        assert len(exported["records"]) == 3
        assert all(r["payload"]["scope"] == "varyz" for r in exported["records"])
    page = client.get("/?view=dashboard").text
    assert 'data-project="varyz-main"' not in page
    assert 'data-project="varyz-master"' not in page
    assert 'data-project="varyz"' in page
    assert '<span class="cloud-tile-number">3</span>' in page
    assert "3 history records" in page and "3 work reports" in page
    # Reading merged views does not rewrite source bytes, audit versions or consent.
    with store.connection() as db:
        row = db.execute("SELECT payload,version FROM records WHERE account=? AND id=?",
                         ("owner", "r-1")).fetchone()
        assert json.loads(row["payload"])["scope"] == "varyz-main" and row["version"] == 1
    assert len(archive.permissions("owner")) == 3
    assert len(reports.status("owner")["projects"]) == 3
    assert len(export_project_preferences(store, "other", "varyz")["records"]) == 1


def test_legacy_archive_aliases_are_found_from_either_project_name(cloud, monkeypatch):
    app, _, _ = cloud
    archive = app.state.session_reports.archive
    with monkeypatch.context() as old:
        old.setattr(scopes, "_CHECKOUT_TAIL", re.compile(r"(?!)"))
        archive.push("owner", [Change(
            id="legacy", base_version=0, payload=archive_payload("legacy", project="varyz-main"),
        )])
    archive.push("owner", [Change(
        id="current", base_version=0, payload=archive_payload("current", project="varyz"),
    )])
    for alias in ("varyz", "varyz-main"):
        assert archive.fetch("owner", "legacy", project=alias)["id"] == "legacy"
        assert {r["id"] for r in archive.search("owner", "SQLite", project=alias)["results"]} == {
            "legacy", "current",
        }
        assert {r["id"] for r in archive.timeline("owner", project=alias)["records"]} == {
            "legacy", "current",
        }
    assert archive.search("owner", "SQLite", project="beta")["results"] == []
    assert archive.search("other", "SQLite", project="varyz")["results"] == []


def test_legacy_archive_revocation_cannot_be_bypassed_by_a_checkout_alias(cloud, monkeypatch):
    app, _, _ = cloud
    archive = app.state.session_reports.archive
    with monkeypatch.context() as old:
        old.setattr(scopes, "_CHECKOUT_TAIL", re.compile(r"(?!)"))
        archive.permit("owner", "varyz-main")
        archive.revoke("owner", "varyz-main")
    with pytest.raises(ValueError, match="revoked"):
        archive.push("owner", [Change(
            id="new", base_version=0, payload=archive_payload("new", project="varyz"),
        )])
    archive.permit("owner", "varyz")
    archive.push("owner", [Change(
        id="approved", base_version=0, payload=archive_payload("approved", project="varyz"),
    )])
    assert archive.fetch("owner", "approved")["id"] == "approved"


def test_archive_revocation_removes_all_legacy_spellings_and_preserves_other_projects(
    cloud, monkeypatch,
):
    app, _, _ = cloud
    archive = app.state.session_reports.archive
    with monkeypatch.context() as old:
        old.setattr(scopes, "_CHECKOUT_TAIL", re.compile(r"(?!)"))
        for i, name in enumerate(("varyz", "varyz-main", "varyz-master", "beta")):
            archive.push("owner", [Change(
                id=f"a-{i}", base_version=0, payload=archive_payload(f"a-{i}", project=name),
            )])
        archive.push("other", [Change(
            id="other", base_version=0, payload=archive_payload("other", project="varyz-main"),
        )])
    assert archive.revoke("owner", "varyz-main") == 3
    assert archive.fetch("owner", "a-3")["id"] == "a-3"
    assert archive.fetch("other", "other")["id"] == "other"
    assert {p["project"] for p in archive.permissions("owner") if not p["revoked"]} == {"beta"}
    for i in range(3):
        with pytest.raises(KeyError):
            archive.fetch("owner", f"a-{i}")


def test_legacy_alias_pause_blocks_all_spellings_and_resume_clears_aliases(cloud):
    app, _, _ = cloud
    store = app.state.store
    with store.connection() as db:
        db.execute("INSERT INTO cloud_project_sync VALUES(?,?)", ("owner", "varyz-main"))
        db.execute("INSERT INTO cloud_project_sync VALUES(?,?)", ("owner", "varyz-master"))
    assert store.project_sync.paused("owner") == {"varyz"}
    with store.connection() as db:
        for alias in ("varyz", "varyz-main", "varyz-master"):
            with pytest.raises(ProjectSyncPaused):
                store.project_sync.check(db, "owner", alias)
        store.project_sync.check(db, "other", "varyz")
        store.project_sync.check(db, "owner", "beta")
    store.project_sync.set_paused("owner", "varyz", False)
    assert store.project_sync.paused("owner") == set()


def test_confirmed_numbered_family_is_durable_and_does_not_guess_other_numbered_names(cloud):
    from varyz.cloud.app import create_app

    app, _, settings = cloud
    identities = app.state.store.project_identity
    identities.register("owner", "sanity5", "sanity", numbered=True)
    names = identities.names("owner")
    for alias in ("sanity", "SANITY5", "sanity6", "sanity_7", "sanity-v8", "sanity (9)",
                  "sanity5-main", "sanity-v5-master", "sanity5.14"):
        assert names(alias) == names(names(alias)) == "sanity"
    for other in ("san7", "python3", "gpt4", "web3", "app2", "insanity5", "sanity-tools5",
                  "sanity5-backup", "org:sanity5", "published:sanity5", "*"):
        assert names(other) == other
    assert identities.names("other")("sanity5") == "sanity5"
    assert create_app(settings).state.store.project_identity.names("owner")("sanity17") == "sanity"


def test_alias_registry_handles_renames_chains_cycles_and_reserved_scope_boundaries(cloud):
    registry = cloud[0].state.store.project_identity
    registry.register("owner", "old checkout", "sanity5")
    registry.register("owner", "sanity5", "sanity", numbered=True)
    registry.register("owner", "sanity5", "sanity", numbered=True)
    assert registry.names("owner")("old checkout") == "sanity"
    with pytest.raises(ValueError, match="cycle"):
        registry.register("owner", "sanity", "old checkout")
    assert registry.names("owner")("old checkout") == "sanity"
    for source, target in (("*", "sanity"), ("sanity", "General"),
                           ("org:team", "sanity"), ("sanity", "published:someone")):
        with pytest.raises(ValueError, match="owner projects"):
            registry.register("owner", source, target)


def test_numbered_aliases_merge_saved_preferences_history_reports_and_future_copies(cloud):
    app, client, _ = cloud
    store = app.state.store
    archive = app.state.session_reports.archive
    reports = app.state.session_reports
    for i, name in enumerate(("sanity", "sanity5", "sanity6-main", "beta")):
        seed(store, "owner", name, i, 1)
        archive.push("owner", [Change(
            id=f"a-{i}", base_version=0, payload=archive_payload(f"a-{i}", project=name),
        )])
        reports.permit("owner", name)
        reports.push("owner", [ReportChange(
            id=f"p-{i}", base_version=0, payload=report(f"p-{i}", project=name,
                                                     seq=i+1, agent="codex"),
        )])
    seed(store, "other", "sanity5", 20, 1)
    store.project_identity.register("owner", "sanity5", "sanity", numbered=True)
    for alias in ("sanity", "sanity5", "sanity7", "sanity6-main"):
        assert {r["id"] for r in store.knowledge("owner", alias)} == {"r-0", "r-1", "r-2"}
        assert len(export_project_preferences(store, "owner", alias)["records"]) == 3
        assert len(preference_page(store, "owner", alias)["records"]) == 3
        assert {r["id"] for r in archive.timeline("owner", project=alias)["records"]} == {
            "a-0", "a-1", "a-2",
        }
        assert len(archive.search("owner", "SQLite", project=alias)["results"]) == 3
        assert archive.fetch("owner", "a-1", project=alias)["id"] == "a-1"
        history = reports.history("owner", Query(project=alias, latest=False, agent="codex"))
        assert {r["reference"] for r in history["reports"]} == {
            "varyz-progress:p-0", "varyz-progress:p-1", "varyz-progress:p-2",
        }
    latest = reports.history("owner", Query(project="sanity5", topic="SEO", agent="codex"))
    assert latest["reports"][0]["reference"] == "varyz-progress:p-2"
    first = reports.history("owner", Query(project="sanity", conversation_id="seo-session",
                                           latest=False, limit=1))
    second = reports.history("owner", Query(project="sanity5", conversation_id="seo-session",
                                            latest=False, limit=1, cursor=first["next_cursor"]))
    assert first["reports"][0]["reference"] != second["reports"][0]["reference"]
    assert store.projects("owner") == ["beta", "sanity"]
    assert store.projects("other") == ["sanity5"]
    page = client.get("/?view=dashboard").text
    assert 'data-project="sanity5"' not in page and 'data-project="sanity6"' not in page
    assert 'data-project="sanity"' in page
    assert "3 history records" in page and "3 work reports" in page
    with store.connection() as db:
        original = db.execute("SELECT payload,version FROM records WHERE account=? AND id=?",
                              ("owner", "r-1")).fetchone()
        assert json.loads(original["payload"])["scope"] == "sanity5" and original["version"] == 1
    assert len(reports.status("owner")["projects"]) == 4
    reports.revoke("owner", "sanity5")
    assert reports.history("owner", Query(project="sanity"))["approved"] is False


def test_numbered_family_preserves_pauses_revocations_and_exact_report_upload_consent(cloud):
    app, _, _ = cloud
    store = app.state.store
    archive = app.state.session_reports.archive
    reports = app.state.session_reports
    store.project_sync.set_paused("owner", "sanity5", True)
    archive.revoke("owner", "sanity5")
    reports.permit("owner", "sanity")
    store.project_identity.register("owner", "sanity5", "sanity", numbered=True)
    assert store.project_sync.paused("owner") == {"sanity"}
    with store.connection() as db:
        with pytest.raises(ProjectSyncPaused):
            store.project_sync.check(db, "owner", "sanity9")
        store.project_sync.check(db, "owner", "beta")
    store.project_sync.set_paused("owner", "sanity", False)
    assert store.project_sync.paused("owner") == set()
    with pytest.raises(ValueError, match="revoked"):
        archive.push("owner", [Change(
            id="new", base_version=0, payload=archive_payload("new", project="sanity9"),
        )])
    with pytest.raises(ValueError, match="not approved"):
        reports.push("owner", [ReportChange(
            id="new", base_version=0, payload=report("new", project="sanity9"),
        )])
    assert reports.history("other", Query(project="sanity"))["found"] == 0


def test_deleting_a_confirmed_family_removes_its_aliases_and_keeps_other_accounts(cloud):
    from tests.test_cloud_project_actions import action

    app, client, _ = cloud
    store = app.state.store
    for i, name in enumerate(("sanity", "sanity5", "sanity6-main", "beta")):
        seed(store, "owner", name, i, 1)
        app.state.session_reports.archive.push("owner", [Change(
            id=f"a-{i}", base_version=0, payload=archive_payload(f"a-{i}", project=name),
        )])
        app.state.session_reports.permit("owner", name)
        app.state.session_reports.push("owner", [ReportChange(
            id=f"p-{i}", base_version=0, payload=report(f"p-{i}", project=name),
        )])
    seed(store, "other", "sanity5", 20, 1)
    store.project_identity.register("owner", "sanity5", "sanity", numbered=True)
    assert action(client, "delete_project", "sanity5").status_code == 422
    response = client.post("/account/action", data=dict(
        csrf=client.cookies.get("csrf"), action="delete_project", project="sanity",
        confirm_action="1",
    ), follow_redirects=False)
    assert response.status_code == 303
    assert store.projects("owner") == ["beta"]
    assert store.projects("other") == ["sanity5"]
    assert app.state.session_reports.archive.status("owner")["records"] == 1
    assert app.state.session_reports.status("owner")["records"] == 1
