"""End-to-end guided selection and consent through an isolated real Cloud API."""

import json

import pytest
from fastapi.testclient import TestClient

from varyz.cloud import guided
from varyz.cloud.app import Settings, create_app
from varyz.cloud.client import CloudClient, CloudConflict
from varyz.cloud.computer import CLOUD_ORIGIN
from varyz.cloud.guided import GuidedSetup, PreviewChanged
from varyz.cloud.local_sync import read_journal, save_journal
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
    device_id="test",
    transport="local",
)
COMMON = {"caller": OWNER, "device_id": "test", "source": "owner"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir="{(tmp_path / "data").as_posix()}"\n', encoding="utf-8")
    connection = open_db(tmp_path / "data/varyz.db")
    wizard = GuidedSetup(config, "owner")
    save_journal(
        wizard.path,
        {
            "url": CLOUD_ORIGIN,
            "account_id": "owner",
            "enabled": False,
            "projects": [],
            "records": {},
            "cursor": 0,
        },
    )
    from cryptography.fernet import Fernet

    app = create_app(
        Settings(
            database=str(tmp_path / "cloud.db"),
            public_url=CLOUD_ORIGIN,
            archive_encryption_key=Fernet.generate_key().decode(),
        )
    )
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
            ("owner", "owner@example.test", "unused", "active"),
        )
    token = store.credential("owner", "access", 3600, "varyz-local")
    requests = []
    with TestClient(app, base_url=CLOUD_ORIGIN) as http:

        class Client(CloudClient):
            def request(self, method, path, **kwargs):
                requests.append((method, path))
                return super().request(method, path, **kwargs)

        monkeypatch.setattr(
            guided,
            "CloudClient",
            lambda *a, **kw: Client(
                CLOUD_ORIGIN,
                {"access_token": token},
                http=http,
            ),
        )
        monkeypatch.setattr(guided, "load_tokens", lambda name: {"access_token": token})
        yield wizard, connection, store, requests
    connection.close()


def rule(conn, statement="Use clear language", scope="*"):
    return SQLitePrincipleStore(conn).add(statement=statement, scope=scope, **COMMON)


def current(store):
    with store.connection() as db:
        return [
            store.record(row)
            for row in db.execute(
                "SELECT * FROM records WHERE account='owner' AND payload!='null'",
            )
        ]


def test_setup_completion_includes_empty_general_only_approval_and_checks_account(setup):
    wizard, _conn, _store, _requests = setup
    assert wizard.choices()["setup_complete"] is False
    preview = wizard.preview_projects([])
    assert wizard.choices()["setup_complete"] is False  # A preview is not approval.
    assert wizard.finish(preview)["state"] == "complete_empty"
    choices = wizard.choices()
    assert choices["setup_complete"] is True
    assert choices["general"] == 0 and choices["selected"] == []
    assert GuidedSetup(wizard.config, "owner").choices()["setup_complete"] is True
    with pytest.raises(ValueError, match="account shown on this page"):
        GuidedSetup(wizard.config, "another-account").choices()


def test_picker_and_preview_send_no_text_and_enable_nothing(setup):
    wizard, conn, _, requests = setup
    rule(conn)
    rule(conn, "Keep Alpha simple", "alpha")
    rule(conn, "Use Beta style", "beta")
    SQLitePrincipleStore(conn).propose(statement="Unconfirmed", scope="secret-project", **COMMON)
    choices = wizard.choices()
    assert choices["general"] == 1
    assert choices["projects"] == [{"name": "alpha", "count": 1}, {"name": "beta", "count": 1}]
    original = read_journal(wizard.path)
    preview = wizard.preview(GuidedSetup.scopes(["alpha"], False))
    assert preview["uploads"] == 2 and preview["deletions"] == 0
    assert "Keep Alpha simple" not in json.dumps(preview)
    assert read_journal(wizard.path) == original and requests == []


def test_general_only_is_not_all_projects_and_finishes_with_hosted_rule(setup):
    wizard, conn, store, requests = setup
    rule(conn)
    rule(conn, "Project only", "alpha")
    preview = wizard.preview(GuidedSetup.scopes([], False))
    assert preview["projects"] == ["*"] and preview["uploads"] == 1
    result = wizard.finish(preview)
    assert result["state"] == "complete" and result["uploaded"] == 1
    assert len(current(store)) == 1
    assert read_journal(wizard.path)["projects"] == ["*"]
    assert ("POST", "/mcp") in requests


def test_unified_projects_authorize_future_context_only_after_approval(setup):
    wizard, conn, store, requests = setup
    rule(conn)
    rule(conn, "Alpha preference", "alpha")
    rule(conn, "Beta preference", "beta")
    preview = wizard.preview_projects(["alpha"])
    assert preview["projects"] == ["alpha"]
    assert preview["history_projects"] == preview["report_projects"] == ["alpha"]
    assert current(store) == [] and requests == []
    result = wizard.finish(preview)
    assert result["state"] == "complete"
    from varyz.cloud.archive_sync import read_archive_journal
    from varyz.cloud.session_report_sync import read_journal as read_reports

    assert set(read_archive_journal(wizard.archive_path)["projects"]) == {"alpha"}
    report_path = wizard.config.with_suffix(".cloud-reports.json")
    assert set(read_reports(report_path)["projects"]) == {"alpha"}
    assert len(current(store)) == 2  # General preferences plus alpha.
    with pytest.raises(ValueError, match="available projects"):
        wizard.preview_projects(["unknown"])
    # A fresh preview can remove a previously approved project.
    removal = wizard.preview_projects([])
    assert removal["deletions"] == 1
    wizard.finish(removal)
    assert set(read_archive_journal(wizard.archive_path)["projects"]) == set()
    assert len(current(store)) == 1


def test_legacy_global_label_is_general_in_picker_and_preview(setup):
    wizard, conn, _, requests = setup
    general = rule(conn, "Avoid neon accents")
    project = rule(conn, "Keep Alpha simple", "alpha")
    # Reproduce a scope written by older Varyz without changing the rule itself.
    conn.execute("DROP TRIGGER owner_principles_no_update")
    conn.execute("UPDATE owner_principles SET scope = 'all_projects' WHERE id = ?", (general,))
    conn.commit()
    journal = read_journal(wizard.path)
    journal.update(enabled=True, projects=["all_projects", "alpha"])
    save_journal(wizard.path, journal)

    choices = wizard.choices()
    assert choices["general"] == 1
    assert choices["projects"] == [{"name": "alpha", "count": 1}]
    assert choices["selected"] == ["alpha"]
    assert choices["all_projects"] is False
    preview = wizard.preview(GuidedSetup.scopes([], False))
    assert preview["uploads"] == 1
    selected = guided.selected_payloads(conn, ["*"])
    assert selected[f"rule:{general}"]["scope"] == "*"
    assert f"rule:{project}" not in selected
    assert requests == [] and read_journal(wizard.path) == journal
    assert (
        conn.execute("SELECT scope FROM owner_principles WHERE id = ?", (general,)).fetchone()[0]
        == "all_projects"
    )


def test_changed_preview_does_not_enable_or_upload(setup):
    wizard, conn, store, requests = setup
    rule(conn)
    preview = wizard.preview(["alpha"])
    rule(conn, "New confirmed rule", "alpha")
    with pytest.raises(PreviewChanged):
        wizard.finish(preview)
    assert read_journal(wizard.path)["enabled"] is False
    assert current(store) == []
    assert requests == [("GET", "/api/account")]


def test_journal_change_requires_another_preview(setup):
    wizard, conn, store, _ = setup
    rule(conn)
    preview = wizard.preview(["*"])
    journal = read_journal(wizard.path)
    journal["cursor"] = 5
    save_journal(wizard.path, journal)
    with pytest.raises(PreviewChanged):
        wizard.finish(preview)
    assert read_journal(wizard.path) == journal and current(store) == []


def test_other_devices_unselected_projects_are_not_removed(setup):
    wizard, conn, store, _ = setup
    rule(conn, scope="alpha")
    store.push(
        "owner",
        [
            {
                "id": "other-project",
                "kind": "rule",
                "base_version": 0,
                "payload": {"statement": "Keep Beta", "scope": "beta"},
            }
        ],
    )
    assert wizard.finish(wizard.preview(["alpha"]))["state"] == "complete"
    assert len(current(store)) == 2
    assert wizard.preview(["alpha"])["deletions"] == 0
    assert len(SQLitePrincipleStore(conn).current()) == 1


def test_only_selected_projects_are_scanned_for_credential_text(setup):
    wizard, conn, _, requests = setup
    rule(conn)
    rule(conn, "api_key=sk-" + "a" * 48, "beta")
    assert wizard.choices()["projects"] == [{"name": "beta", "count": 1}]
    assert wizard.preview(["*"])["uploads"] == 1
    with pytest.raises(ValueError, match="possible credential"):
        wizard.preview(["beta"])
    assert requests == [] and not read_journal(wizard.path)["enabled"]


def test_inactive_account_and_disconnect_block_approval(setup):
    wizard, conn, store, _ = setup
    rule(conn)
    preview = wizard.preview(["*"])
    with store.connection() as db:
        db.execute("UPDATE accounts SET status='inactive' WHERE id='owner'")
    with pytest.raises(ValueError, match="Cloud access"):
        wizard.finish(preview)
    assert not read_journal(wizard.path)["enabled"]
    journal = read_journal(wizard.path)
    journal.pop("url")
    save_journal(wizard.path, journal)
    with pytest.raises(ValueError, match="Reconnect"):
        wizard.finish(preview)


def test_all_projects_and_deletions_have_explicit_preview(setup):
    wizard, conn, store, _ = setup
    identity = rule(conn)
    rule(conn, "Use Alpha words", "alpha")
    assert wizard.finish(wizard.preview(GuidedSetup.scopes([], True)))["state"] == "complete"
    SQLitePrincipleStore(conn).retract(identity, caller=OWNER, device_id="test")
    preview = wizard.preview([])
    assert preview["deletions"] == 1 and preview["uploads"] == 0
    wizard.finish(preview)
    assert len(current(store)) == 1
    changed = wizard.preview(["*"])
    assert changed["deletions"] == 1 and changed["uploads"] == 0
    assert len(current(store)) == 1
    wizard.finish(changed)
    assert current(store) == []


def test_conflict_is_never_resolved_automatically(setup):
    wizard, conn, store, _ = setup
    identity = rule(conn)
    preview = wizard.preview(["*"])
    store.push(
        "owner",
        [
            {
                "id": identity,
                "kind": "rule",
                "base_version": 0,
                "payload": {"statement": "Remote version", "scope": "*"},
            }
        ],
    )
    with pytest.raises(CloudConflict):
        wizard.finish(preview)
    assert current(store)[0]["payload"]["statement"] == "Remote version"
    assert SQLitePrincipleStore(conn).current()[0].statement == "Use clear language"


def test_synced_but_unverified_is_not_complete(setup, monkeypatch):
    wizard, conn, store, _ = setup
    rule(conn)

    def unavailable(*args):
        raise ValueError("Secret provider details")

    monkeypatch.setattr(wizard, "_verify", unavailable)
    result = wizard.finish(wizard.preview(["*"]))
    assert result["state"] == "verification_failed"
    assert len(current(store)) == 1


def test_full_capacity_finishes_setup_and_preserves_pending_preferences(setup):
    from tests.test_cloud_archive import local_entry

    wizard, conn, store, _ = setup
    rule(conn)
    local_entry(conn, "history-capacity")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',authored_by_user=1,body=? "
        "WHERE id='history-capacity'",
        ("From: owner\nConversation: Codex — demo\n\nUse SQLite offline.",),
    )
    conn.commit()
    store.max_storage_bytes = 1
    result = wizard.finish(wizard.preview(["*"], ["demo"]))
    assert result["state"] == "complete_limited"
    assert result["storage_full"] is True
    assert result["history"]["uploaded"] == 1  # Other allowances still work.
    assert wizard.choices()["setup_complete"] is True
    assert current(store) == []
    assert len(SQLitePrincipleStore(conn).current()) == 1
    store.max_storage_bytes = 50 * 1024**2
    assert wizard.finish(wizard.preview(["*"]))["state"] == "complete"


@pytest.mark.parametrize("kind", ["answer", "workflow", "empty"])
def test_fewer_rules_do_not_require_fabricating_a_rule(setup, kind):
    wizard, conn, _, _ = setup
    if kind == "answer":
        SQLiteKnowledgeStore(conn).remember(
            question="Which tone?",
            answer="Warm",
            basis=AnswerBasis.EXPLICIT,
            scope="alpha",
            **COMMON,
        )
    elif kind == "workflow":
        SQLiteWorkflowStore(conn).record(
            name="Ship", steps=[{"step": "Review"}, {"step": "Ship"}], scope="alpha", **COMMON
        )
    result = wizard.finish(wizard.preview(["alpha"]))
    assert result["state"] == ("complete_empty" if kind == "empty" else "complete")


@pytest.mark.parametrize(
    "projects,all_projects",
    [([""], False), (["alpha"], "yes"), ("alpha", False), (["alpha"], True)],
)
def test_ambiguous_scope_choices_are_rejected(projects, all_projects):
    with pytest.raises(ValueError):
        GuidedSetup.scopes(projects, all_projects)


def test_history_approval_is_separate_bound_and_project_based(setup):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import read_archive_journal

    wizard, conn, store, requests = setup
    local_entry(conn, "history-one")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',authored_by_user=1,body=? "
        "WHERE id='history-one'",
        ("From: owner\nConversation: Codex — demo\n\nWe chose SQLite for offline work.",),
    )
    conn.commit()
    choices = wizard.choices()
    assert {p["name"] for p in choices["history_projects"]} == {"*", "demo"}
    preview = wizard.preview(["*"], [])
    assert preview["history_uploads"] == 0
    assert not read_archive_journal(wizard.archive_path)["enabled"]
    preview = wizard.preview(["*"], ["demo"])
    assert preview["history_uploads"] == 1
    conn.execute("UPDATE entries SET body=body || ' changed' WHERE id='history-one'")
    conn.commit()
    with pytest.raises(PreviewChanged):
        wizard.finish(preview)
    result = wizard.finish(wizard.preview(["*"], ["demo"]))
    assert result["history"]["uploaded"] == 1
    assert read_archive_journal(wizard.archive_path)["projects"].keys() == {"demo"}
    assert ("POST", "/api/archive/permit") in requests


def test_history_project_deselection_removes_cloud_copy(setup):
    from tests.test_cloud_archive import local_entry

    wizard, conn, store, requests = setup
    local_entry(conn, "general-history")
    wizard.finish(wizard.preview(["*"], ["*"]))
    assert wizard.choices()["history_selected"] == ["*"]
    preview = wizard.preview(["*"], [])
    assert preview["history_deletions"] == 1
    wizard.finish(preview)
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM archive_documents WHERE payload!='null'").fetchone()[0]
            == 0
        )
    assert ("POST", "/api/archive/revoke") in requests


@pytest.mark.parametrize("synced", [False, True])
def test_history_deselection_clears_older_individual_approvals(setup, synced):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import ArchiveSync, approve, read_archive_journal

    wizard, conn, store, requests = setup
    local_entry(conn, "downloaded-history")
    approve(conn, wizard.archive_path, ["downloaded-history"], "*")
    if synced:
        ArchiveSync(conn, wizard.archive_path).sync(guided.CloudClient(), "owner")
    requests.clear()
    preview = wizard.preview(["*"], [])
    assert preview["history_uploads"] == 0
    assert preview["history_deletions"] == int(synced)
    wizard.finish(preview)
    assert read_archive_journal(wizard.archive_path)["selected"] == {}
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM archive_documents WHERE payload!='null'").fetchone()[0]
            == 0
        )


def test_downloaded_repos_are_not_proposed_or_accepted_without_own_work(setup, tmp_path):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import allow_project

    wizard, conn, _, requests = setup
    for name, authored in (("stable-diffusion", False), ("my-tool", True)):
        local_entry(conn, name)
        conn.execute(
            "UPDATE entries SET source='git',external_id=?,authored_by_user=? WHERE id=?",
            (str(tmp_path / name) + "@commit", authored, name),
        )
    conn.commit()
    choices = wizard.choices()
    assert {p["name"] for p in choices["history_projects"]} == {"*", "my-tool"}
    with pytest.raises(ValueError, match="Review available history projects"):
        wizard.preview(["*"], ["stable-diffusion"])
    assert requests == []
    # Previously approved imports remain visible so the owner can revoke them.
    allow_project(wizard.archive_path, "stable-diffusion", tmp_path / "stable-diffusion")
    assert "stable-diffusion" in {p["name"] for p in wizard.choices()["history_projects"]}


def test_project_conversation_is_evidence_of_work_in_an_imported_repo(setup, tmp_path):
    from tests.test_cloud_archive import local_entry

    wizard, conn, _, _ = setup
    local_entry(conn, "conversation")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',authored_by_user=1,body=? "
        "WHERE id='conversation'",
        ("From: owner\nConversation: Codex — my-fork\n\nHelp me change this project.",),
    )
    conn.commit()
    assert "my-fork" in {p["name"] for p in wizard.choices()["history_projects"]}


def test_live_watcher_sends_approved_history_without_preference_sync(setup):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import allow_project, read_archive_journal
    from varyz.cloud.live_sync import LiveCloudSync

    wizard, conn, _, requests = setup
    local_entry(conn, "history-live")
    conn.execute(
        "UPDATE entries SET source='ai_conversations',authored_by_user=1,body=? "
        "WHERE id='history-live'",
        ("From: owner\nConversation: Codex — demo\n\nUse SQLite offline.",),
    )
    conn.commit()
    watcher = LiveCloudSync(
        wizard.config,
        wizard.config.parent / "data/varyz.db",
        client=lambda _: guided.CloudClient(),
    )
    assert watcher._sync(conn, only_if_planned=True) is None
    assert requests == []
    allow_project(wizard.archive_path, "demo")
    assert watcher._sync(conn, only_if_planned=True) is True
    assert "history-live" in read_archive_journal(wizard.archive_path)["records"]
    assert ("POST", "/api/sync") not in requests
    requests.clear()
    assert watcher._sync(conn, only_if_planned=True) is None
    assert requests == []


def test_folder_activity_is_reviewable_and_assistant_only_is_not_ownership(setup, tmp_path):
    from tests.test_cloud_archive import local_entry

    wizard, conn, _, requests = setup
    for name, authored in (("site", 1), ("kronos", 1), ("assistant-only", 0)):
        local_entry(conn, name)
        conn.execute(
            "UPDATE entries SET source='ai_conversations',authored_by_user=?,body=? WHERE id=?",
            (authored, f"From: user\nConversation: Codex — {name}\n\nSome activity.", name),
        )
    local_entry(conn, "my-project")
    conn.execute(
        "UPDATE entries SET source='git',authored_by_user=1,external_id=? WHERE id='my-project'",
        (str(tmp_path / "my-project") + "@commit",),
    )
    conn.commit()
    choices = {p["name"]: p for p in wizard.choices()["sync_projects"]}
    assert choices["site"]["needs_review"] is True
    assert choices["kronos"]["needs_review"] is True
    assert choices["my-project"]["needs_review"] is False
    assert "assistant-only" not in choices
    assert requests == []


def test_project_exclusion_removes_cloud_copies_and_keeps_local_evidence(setup):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import read_archive_journal
    from varyz.cloud.project_cleanup import remove_projects
    from varyz.cloud.project_policy import exclusions

    wizard, conn, store, _ = setup
    junk_rule = rule(conn, "A Kronos rule", "kronos")
    good_rule = rule(conn, "Keep my project", "alpha")
    rule(conn, "General stays local and synced")
    for name in ("kronos", "alpha"):
        local_entry(conn, name)
        conn.execute(
            "UPDATE entries SET source='ai_conversations',authored_by_user=1,body=? WHERE id=?",
            (f"From: user\nConversation: Codex — {name}\n\nProject activity.", name),
        )
    conn.commit()
    wizard.finish(wizard.preview_projects(["kronos", "alpha"]))
    original = read_journal(wizard.path)
    assert remove_projects(wizard.config, ["kronos"])["applied"] is False
    assert read_journal(wizard.path) == original and exclusions(wizard.config) == set()
    result = remove_projects(wizard.config, ["kronos"], apply=True)
    assert result["local_evidence_deleted"] is False
    assert "kronos" not in wizard.choices()["sync_selected"]
    assert "kronos" not in {p["name"] for p in wizard.choices()["sync_projects"]}
    wizard.finish(wizard.preview_projects(["alpha"]))
    assert all(record["payload"]["scope"] != "kronos" for record in current(store))
    assert {r.id for r in SQLitePrincipleStore(conn).current()} >= {junk_rule, good_rule}
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE id='kronos'").fetchone()[0] == 1
    assert "kronos" not in read_archive_journal(wizard.archive_path)["projects"]
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM archive_documents WHERE payload!='null'").fetchone()[0]
            == 1
        )


def test_exclusions_override_legacy_all_project_and_individual_approvals(setup):
    from tests.test_cloud_archive import local_entry
    from varyz.cloud.archive_sync import ArchiveSync, approve
    from varyz.cloud.local_sync import LocalSync
    from varyz.cloud.project_policy import exclude

    wizard, conn, _, _ = setup
    rule(conn, "Not mine", "kronos")
    rule(conn, "My project", "alpha")
    journal = read_journal(wizard.path)
    journal.update(enabled=True, projects=[])
    save_journal(wizard.path, journal)
    local_entry(conn, "junk-history")
    approve(conn, wizard.archive_path, ["junk-history"], "kronos")
    exclude(wizard.config, ["kronos"])
    assert {c["payload"]["scope"] for c in LocalSync(conn, wizard.path).plan()} == {"alpha"}
    assert ArchiveSync(conn, wizard.archive_path).preview()["uploads"] == 0


def test_remote_excluded_rule_deletion_does_not_retract_local_rule(setup):
    from varyz.cloud.local_sync import LocalSync
    from varyz.cloud.project_policy import exclude

    wizard, conn, _, _ = setup
    identity = rule(conn, "Still useful locally", "kronos")
    journal = read_journal(wizard.path)
    journal["records"]["rule:remote"] = dict(local_id=identity, version=1, hash="old")
    save_journal(wizard.path, journal)
    exclude(wizard.config, ["kronos"])
    worker = LocalSync(conn, wizard.path)
    worker._apply(dict(kind="rule", id="remote", version=2, payload=None))
    assert identity in {r.id for r in SQLitePrincipleStore(conn).current()}
