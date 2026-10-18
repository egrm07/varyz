# ruff: noqa: F811
"""Task briefings retrieve evidence before scope selection, for every account."""


from varyz.config import Config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.mcp_server import VaryzServer
from varyz.resolution.briefing import render_markdown, start_task
from varyz.resolution.task_history import local_history, task_query
from varyz.trust import register_agent


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
