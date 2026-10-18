"""General scope spelling must agree across capture, storage and Cloud."""

import json

import pytest

from varyz.card.source import load_owner_rules
from varyz.cloud.local_sync import LocalSync, save_journal, selected_payloads
from varyz.cloud.payloads import canonical_payload
from varyz.cloud.payloads import normalize_scope as cloud_scope
from varyz.cloud.store import Conflict, Store
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller, ResolutionRequest
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.scope import parse_scope_choice
from varyz.resolution.shadow_policy import scope_key
from varyz.resolution.workflows import SQLiteWorkflowStore
from varyz.scopes import normalize_scope, stored_scope

GENERAL_LABELS = (
    "all_projects",
    "All Projects",
    "all-projects",
    "every_project",
    "each project",
    "any-project",
    "all_of_my_projects",
    "all  my  projects",
    "all repositories",
    "every-codebase",
    "general",
    "GLOBAL",
    "universal",
    "ＡＬＬ＿ＰＲＯＪＥＣＴＳ",
)
PROJECT_NAMES = (
    "all_projects_tool",
    "alloy",
    "my-projects",
    "project_all",
    "every-project-backup",
    "general-store",
    "universal-app",
    "all projects dashboard",
    "always-ready",
    "always review this project",
)
OWNER = Caller(
    agent_id="owner:local",
    display_name="Owner",
    purpose="owner_knowledge",
    device_id="test",
    transport="local",
)
COMMON = {"caller": OWNER, "device_id": "test", "source": "owner"}


@pytest.mark.parametrize("label", GENERAL_LABELS)
def test_general_labels_have_one_meaning_on_every_surface(label):
    assert normalize_scope(label) == stored_scope(label) == cloud_scope(label) == "*"
    assert parse_scope_choice(label, "shop") == scope_key(label) == "*"
    for kind, payload in (
        ("rule", {"statement": "Use clear words"}),
        ("answer", {"question": "Which tone?", "answer": "Plain words"}),
        ("workflow", {"name": "Review", "steps": [{"step": "draft"}, {"step": "review"}]}),
    ):
        assert canonical_payload(kind, {**payload, "scope": label})["scope"] == "*"


@pytest.mark.parametrize("name", PROJECT_NAMES)
def test_similar_project_names_are_never_promoted_to_general(name):
    assert normalize_scope(name) == cloud_scope(name) == scope_key(name) == name
    assert parse_scope_choice(name, "shop") == name


@pytest.mark.parametrize("label", ["this_project", "current-project", "project_only"])
def test_relative_scope_labels_require_a_real_project(label):
    assert parse_scope_choice(label, "shop") == "shop"
    with pytest.raises(ValueError, match="known project"):
        parse_scope_choice(label, None)


@pytest.mark.parametrize(
    "scope", ["org:alpha", "published:alpha", "ORG:*", "published:all_projects"]
)
def test_foreign_taste_cannot_become_the_owners_general_preferences(scope):
    assert stored_scope(scope).startswith(("org:", "published:"))
    with pytest.raises(ValueError):
        cloud_scope(scope)


@pytest.mark.parametrize("label", ["general", "all_of_my_projects", "each-project"])
def test_new_and_legacy_rules_answers_and_workflows_share_global_scope(tmp_path, label):
    db = tmp_path / "local.sqlite"
    conn = open_db(db)
    try:
        rules = SQLitePrincipleStore(conn)
        answers = SQLiteKnowledgeStore(conn)
        workflows = SQLiteWorkflowStore(conn)
        rule = rules.add(statement="Use plain words", scope=label, **COMMON)
        answer = answers.remember(
            question="Which review tone?",
            answer="Plain words",
            basis=AnswerBasis.EXPLICIT,
            scope=label,
            **COMMON,
        )
        workflow = workflows.record(name="Review", steps=["draft", "review"], scope=label, **COMMON)
        for table, identity in (
            ("owner_principles", rule),
            ("owner_knowledge", answer),
            ("owner_workflows", workflow),
        ):
            assert (
                conn.execute(f"SELECT scope FROM {table} WHERE id=?", (identity,)).fetchone()[0]
                == "*"
            )
            # Simulate a row written by an older release. Production only folds reads.
            conn.execute(f"DROP TRIGGER {table}_no_update")
            conn.execute(f"UPDATE {table} SET scope=? WHERE id=?", (label, identity))
        conn.commit()
        assert (
            rules.get(rule).scope
            == answers.get(answer).scope
            == workflows.get(workflow).scope
            == "*"
        )
        assert rules.add(statement="Use plain words", scope="*", **COMMON) == rule
        assert (
            workflows.record(name="Review", steps=["draft", "review"], scope="*", **COMMON)
            == workflow
        )
        result = answers.try_resolve(
            ResolutionRequest(
                question="Which review tone?",
                project="new-project",
                caller=OWNER,
                purpose=OWNER.purpose,
            )
        )
        assert result is not None and result.answer == "Plain words"
        selected = selected_payloads(conn, ["new-project"])
        assert set(selected) == {f"rule:{rule}", f"answer:{answer}", f"workflow:{workflow}"}
        assert all(payload["scope"] == "*" for payload in selected.values())
        assert all(row.general for row in load_owner_rules(db).rules)
        for table in ("owner_principles", "owner_knowledge", "owner_workflows"):
            assert conn.execute(f"SELECT scope FROM {table}").fetchone()[0] == label
    finally:
        conn.close()


def test_hosted_legacy_reads_are_canonical_and_stay_account_isolated(tmp_path):
    store = Store(tmp_path / "cloud.sqlite")
    original = {"statement": "Use clear words", "scope": "any_project"}
    store.push("alice", [{"kind": "rule", "id": "general", "base_version": 0, "payload": original}])
    store.push(
        "alice",
        [
            {
                "kind": "rule",
                "id": "other",
                "base_version": 0,
                "payload": {"statement": "Use blue", "scope": "other"},
            }
        ],
    )
    assert [r["id"] for r in store.knowledge("alice", "shop")] == ["general"]
    assert store.pull("alice", 0, 10)["records"][0]["payload"]["scope"] == "*"
    assert store.knowledge("bob", "shop") == []
    # Replaying a canonical retry against an old spelling is not a content conflict.
    retried = store.push(
        "alice",
        [
            {
                "kind": "rule",
                "id": "general",
                "base_version": 0,
                "payload": {**original, "scope": "*"},
            }
        ],
    )
    assert retried[0]["version"] == 1
    with pytest.raises(Conflict):
        store.push(
            "alice",
            [
                {
                    "kind": "rule",
                    "id": "general",
                    "base_version": 0,
                    "payload": {**original, "scope": "other"},
                }
            ],
        )
    with store.connection() as conn:
        payload = conn.execute(
            "SELECT payload FROM records WHERE account='alice' AND id='general'"
        ).fetchone()[0]
        assert json.loads(payload) == original


def test_old_remote_global_labels_are_not_dropped_by_project_sync_filter(tmp_path):
    conn = open_db(tmp_path / "local.sqlite")
    try:
        path = tmp_path / "sync.json"
        save_journal(path, {"enabled": True, "projects": ["shop"], "records": {}, "cursor": 0})
        sync = LocalSync(conn, path)
        sync._apply(
            {
                "kind": "rule",
                "id": "general",
                "version": 1,
                "payload": {"statement": "Use clear words", "scope": "every_project"},
            }
        )
        [rule] = SQLitePrincipleStore(conn).current()
        assert rule.scope == "*"
    finally:
        conn.close()
