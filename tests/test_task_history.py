# ruff: noqa: F811
"""Task briefings retrieve evidence before scope selection, for every account."""

import json

import pytest

from tests.test_cloud_archive import hosted, payload  # noqa: F401
from tests.test_hosted_plugin_capabilities import rpc
from varyz.cloud.archive import PLUGIN_SCOPE, Archive, Change, Document
from varyz.config import Config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.mcp_server import VaryzServer
from varyz.resolution.briefing import render_markdown, start_task
from varyz.resolution.task_history import local_history, task_query
from varyz.trust import register_agent


def briefing(client, headers, task="Add the teams section to varyz", path="/mcp/plugin"):
    args = {"project": "varyz"}
    if task is not None:
        args["task"] = task
    response = rpc(client, headers, path, "tools/call", "start_task", args)
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("account", ["alice", "bob"])
def test_fresh_small_task_receives_project_history_without_personal_rule(hosted, account):
    client, store, _ = hosted
    archive = Archive(store, "http://localhost", 2_000_000)
    for owner in ("alice", "bob"):
        for identity, project in (("teams", "varyz"), ("wrong-project", "other")):
            doc = Document.model_validate(payload(
                identity, f"Teams for {owner}: shared project decisions and team billing.", project,
            ))
            archive.push(owner, [Change(id=identity, base_version=0, payload=doc)])
    headers = {"Authorization": "Bearer " + store.credential(
        account, "access", 3600, "test", scope=PLUGIN_SCOPE,
    )}
    check = briefing(client, headers)["history_check"]
    assert check["status"] == "checked"
    assert check["query"] == "teams"
    assert [item["id"] for item in check["evidence"]] == ["teams"]
    assert f"Teams for {account}" in check["evidence"][0]["text"]
    assert check["evidence"][0]["metadata"]["occurred_at"].startswith("2026-10-01")
    assert check["evidence"][0]["url"].endswith("/archive/teams")
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM questions").fetchone()[0] == 0


def test_old_schema_and_preferences_only_grant_do_not_claim_history_checked(hosted):
    client, store, auth = hosted
    plugin = {"Authorization": "Bearer " + store.credential(
        "alice", "access", 3600, "test", scope=PLUGIN_SCOPE,
    )}
    assert briefing(client, plugin, task=None)["history_check"]["status"] == "task_required"
    assert briefing(client, auth(client_id="preferences-only"), path="/mcp")[
        "history_check"
    ]["status"] == "access_required"
    assert briefing(client, auth(), path="/mcp")["history_check"]["status"] == "checked"
    tools = rpc(client, plugin, "/mcp/plugin", "tools/list").json()["result"]["tools"]
    schema = next(tool for tool in tools if tool["name"] == "start_task")["inputSchema"]
    assert "task" in schema["properties"]
    assert "task" not in schema["required"]  # Existing callers remain compatible.


def test_paused_and_revoked_cloud_history_is_not_disclosed(hosted):
    client, store, _ = hosted
    archive = Archive(store, "http://localhost", 2_000_000)
    doc = Document.model_validate(payload("teams", "Teams: shared billing."))
    archive.push("alice", [Change(id=doc.id, base_version=0, payload=doc)])
    headers = {"Authorization": "Bearer " + store.credential(
        "alice", "access", 3600, "test", scope=PLUGIN_SCOPE,
    )}
    archive.control("alice", True)
    check = briefing(client, headers)["history_check"]
    assert check["status"] == "paused" and check["evidence"] == []
    archive.control("alice", False)
    archive.push("alice", [Change(id=doc.id, base_version=1, payload=None)])
    check = briefing(client, headers)["history_check"]
    assert check["status"] == "checked" and check["evidence"] == []
    assert "no matches does not mean" in check["coverage"]


def test_local_history_respects_scope_sensitivity_exclusions_and_grant(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with open_db(cfg.db_path) as conn:
        caller = register_agent(conn, agent_id="test", display_name="Test agent").caller
        for name, project, sensitivity in (
            ("teams", "varyz", Sensitivity.PERSONAL),
            ("other", "other", Sensitivity.PERSONAL),
        ):
            dao.upsert_entry(conn, NormalizedEntry(
                external_id=name, kind=Kind.MESSAGE,
                body=f"From: me\nConversation: ChatGPT — {project}\n\nTeams: shared billing.",
            ), source="ai_conversations", sensitivity=sensitivity)
        check = local_history(conn, cfg, caller, "Add the teams section to varyz", "varyz",
                              tmp_path, 5000)
        assert [hit["reference"] for hit in check["evidence"]] == ["teams"]
        dao.upsert_entry(conn, NormalizedEntry(
            external_id="private", kind=Kind.MESSAGE,
            body="From: me\nConversation: ChatGPT — varyz\n\nTeams: private matter.",
        ), source="ai_conversations", sensitivity=Sensitivity.SENSITIVE)
        check = local_history(conn, cfg, caller, "Add teams", "varyz", tmp_path, 5000)
        assert check["status"] == "withheld" and check["evidence"] == []
        cfg.graph.excluded_sources = ["ai_conversations"]
        assert local_history(conn, cfg, caller, "Add teams", "varyz", tmp_path, 5000)[
            "evidence"
        ] == []
        conn.execute("UPDATE agent_capabilities SET resource_types_json='[\"owner_knowledge\"]'")
        assert local_history(conn, cfg, caller, "Add teams", "varyz", tmp_path, 5000)[
            "status"
        ] == "access_required"


def test_rules_only_hook_preflight_survives_partial_rendering(tmp_path):
    with open_db(tmp_path / "db.sqlite") as conn:
        result = start_task(conn, "Add teams", project="varyz", cwd=tmp_path)
    assert result["history_check"]["status"] == "required"
    text = render_markdown(result, include_protocol=False)
    assert "even for a small change" in text
    assert "history has not been retrieved" in text
    assert task_query('Add Teams OR "billing" to varyz', "varyz") == "teams billing"


def test_authenticated_local_start_task_includes_history_before_any_ask(tmp_path, monkeypatch):
    cfg = Config(base_dir=tmp_path)
    with open_db(cfg.db_path) as conn:
        credential = register_agent(conn, agent_id="codex-test", display_name="Codex test")
        dao.upsert_entry(conn, NormalizedEntry(
            external_id="teams-conversation", kind=Kind.MESSAGE,
            body="From: me\nConversation: ChatGPT — varyz\n\nTeams: shared project preferences.",
        ), source="ai_conversations", sensitivity=Sensitivity.PERSONAL)
    server = VaryzServer(agent_token=credential.token)
    monkeypatch.setattr(type(server), "cfg", property(lambda self: cfg))
    monkeypatch.setattr(server, "_task_sizer", lambda: None)
    try:
        result = server._call_start_task({
            "task": "Add the teams section to varyz", "project": "varyz",
        })
        assert result["history_check"]["status"] == "checked"
        assert result["history_check"]["evidence"][0]["reference"] == "teams-conversation"
        assert server.conn.execute("SELECT count(*) FROM resolution_escalations").fetchone()[0] == 0
    finally:
        server.close()
