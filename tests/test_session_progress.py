"""Offline contracts for reported progress, chronology, privacy and MCP identity."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from varyz.db.connection import open_db
from varyz.mcp_server import TOOLS, VaryzServer, handle
from varyz.session_progress import Checkpoint, HistoryQuery, forget, history, pause_path, record
from varyz.trust import register_agent, revoke_agent


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "progress.db")
    yield conn
    conn.close()


def checkpoint(key="step-1", **fields):
    return Checkpoint.model_validate({"project": "varyz", "conversation_id": "claude-session",
                                     "checkpoint_id": key, "task": "Prepare SEO", **fields})


def test_append_retry_and_conflicting_retry_preserve_history(db):
    first = record(db, checkpoint(done=["Added sitemap"]), agent_id="claude")
    again = record(db, checkpoint(done=["Added sitemap"]), agent_id="claude")
    assert again["duplicate"] and again["seq"] == first["seq"]
    with pytest.raises(ValueError, match="different content"):
        record(db, checkpoint(done=["Deployed sitemap"]), agent_id="claude")
    record(db, checkpoint("step-2", done=["Added sitemap"], pending=["Deploy sitemap"]),
           agent_id="claude")
    reports = history(db, HistoryQuery(project="VARYZ", conversation_id="claude-session",
                                      latest=False))["reports"]
    assert [r["done"] for r in reports] == [["Added sitemap"], ["Added sitemap"]]
    assert reports[0]["pending"] == ["Deploy sitemap"]
    assert reports[0]["provenance"] == "agent_report"
    assert reports[0]["independently_verified"] is False
    assert reports[0]["reference"].startswith("varyz-progress:")
    assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_topic_finds_session_but_returns_current_state(db):
    record(db, checkpoint(done=["Added IndexNow"]), agent_id="claude")
    record(db, checkpoint("step-2", task="Finish discovery", status="blocked",
                          pending=["Bing account verification required"]), agent_id="claude")
    record(db, checkpoint("step-3", conversation_id="other", task="Fix branding"),
           agent_id="claude")
    found = history(db, HistoryQuery(project="varyz", topic="SEO", agent="claude"))
    assert found["found"] == 1
    assert found["reports"][0]["status"] == "blocked"
    assert found["reports"][0]["pending"] == ["Bing account verification required"]


def test_latest_is_record_order_not_relevance_and_retry_does_not_bump(db):
    one = checkpoint(conversation_id="old")
    record(db, one, agent_id="claude")
    record(db, checkpoint(conversation_id="new"), agent_id="claude")
    record(db, one, agent_id="claude")
    assert history(db, HistoryQuery(project="varyz", topic="SEO"))["reports"][0][
        "conversation_id"] == "new"


def test_exact_project_and_agent_filters(db):
    record(db, checkpoint(), agent_id="claude")
    record(db, checkpoint(project="san7"), agent_id="claude")
    record(db, checkpoint(), agent_id="codex")
    found = history(db, HistoryQuery(project="varyz", agent="claude"))
    assert found["reports"][0]["agent"] == "claude"
    assert found["reports"][0]["project"] == "varyz"
    assert history(db, HistoryQuery(project="varyz", topic="nonexistent"))["found"] == 0


@pytest.mark.parametrize("alias", ["claude", "claude-code", "claude_code", "CLAUDE_CODE"])
def test_client_alias_finds_current_live_report_and_preserves_origin_and_privacy(db, alias):
    record(db, checkpoint("old", done=["Added obsolete sitemap"]),
           agent_id="agent:claude-code:remote-device")
    record(db, checkpoint("current", task="Finish discovery", status="blocked",
                          done=["Added current sitemap"], pending=["Owner verification required"]),
           agent_id="agent:claude-code:remote-device")
    record(db, checkpoint(conversation_id="mined"), agent_id="history:claude_code",
           _evidence={"source_agent": "claude_code"})
    record(db, checkpoint(conversation_id="decoy", status="complete"),
           agent_id="agent:codex:local")
    found = history(db, HistoryQuery(project="varyz", agent=alias,
                                    origin="agent_report", topic="SEO"))
    assert found["reports"][0]["conversation_id"] == "claude-session"
    assert found["reports"][0]["status"] == "blocked"
    assert found["reports"][0]["pending"] == ["Owner verification required"]
    assert history(db, HistoryQuery(project="varyz", agent=alias, origin="mined_report"))[
        "reports"][0]["conversation_id"] == "mined"
    timeline = history(db, HistoryQuery(project="varyz", agent=alias, origin="agent_report",
                                      conversation_id="claude-session", latest=False, limit=1))
    assert timeline["truncated"] and timeline["next_before_seq"]
    older = history(db, HistoryQuery(project="varyz", agent=alias, origin="agent_report",
                    conversation_id="claude-session", latest=False, limit=1,
                    before_seq=timeline["next_before_seq"]))
    assert older["reports"][0]["done"] == ["Added obsolete sitemap"]
    record(db, checkpoint("private", sensitivity="sensitive"),
           agent_id="agent:claude-code:remote-device")
    hidden = history(db, HistoryQuery(project="varyz", agent=alias,
                                     origin="agent_report", topic="SEO"))
    assert hidden["reports"] == [] and hidden["withheld_sensitive"] == 1


@pytest.mark.parametrize("client", ["codex", "openclaw", "hermes"])
def test_other_client_aliases_are_exact_families(db, client):
    for agent in [f"agent:{client}:device", f"history:{client}",
                  f"agent:{client}-unrelated:device", f"arbitrary:{client}"]:
        record(db, checkpoint(conversation_id=agent), agent_id=agent)
    found = history(db, HistoryQuery(project="varyz", agent=client, latest=False))
    assert {r["agent"] for r in found["reports"]} == {
        f"agent:{client}:device", f"history:{client}"}


def test_sensitive_latest_is_withheld_without_fallback_or_topic_leak(db):
    record(db, checkpoint(), agent_id="claude")
    record(db, checkpoint("sensitive", sensitivity="sensitive", done=["Private diagnosis"]),
           agent_id="claude")
    found = history(db, HistoryQuery(project="varyz", topic="SEO"))
    assert found["reports"] == [] and found["withheld_sensitive"] == 1
    assert "diagnosis" not in json.dumps(found)
    assert history(db, HistoryQuery(project="varyz", topic="diagnosis"))["found"] == 0


def test_credentials_are_redacted_before_storage(db):
    secret = "sk-ant-" + "a" * 30
    result = record(db, checkpoint(done=[f"Removed {secret}"]), agent_id="claude")
    assert result["credentials_removed"]
    assert secret not in db.execute("SELECT payload_json FROM session_progress").fetchone()[0]
    assert secret not in db.execute("SELECT facts FROM session_progress_fts").fetchone()[0]
    with pytest.raises(ValueError, match="identifiers"):
        record(db, checkpoint(checkpoint_id=secret), agent_id="claude")


@pytest.mark.parametrize("fields", [
    {"task": " "}, {"task": "x" * 241}, {"done": ["x"] * 7},
    {"checks": ["x" * 181]}, {"status": "complete", "pending": ["Deploy"]},
    {"status": "blocked"}, {"agent_id": "spoof"}, {"status": "verified"},
    {"done": ["x" * 180] * 6, "checks": ["x" * 180] * 6,
     "pending": ["x" * 180] * 6},
])
def test_rejects_ambiguous_or_verbose_snapshots(fields):
    with pytest.raises(ValidationError):
        checkpoint(**fields)


def test_budget_never_drops_pending_or_marks_partial_as_complete(db):
    record(db, checkpoint(status="blocked", pending=["Required deployment failed"]),
           agent_id="claude")
    found = history(db, HistoryQuery(project="varyz"), max_chars=40)
    assert found["truncated"] and not found["reports"]


def test_pagination_and_erasure_remove_search_text(db):
    for n in range(4):
        record(db, checkpoint(f"step-{n}", done=[f"Finished stage {n}"]), agent_id="claude")
    query = HistoryQuery(project="varyz", conversation_id="claude-session", latest=False, limit=2)
    first = history(db, query)
    second = history(db, query.model_copy(update={"before_seq": first["next_before_seq"]}))
    assert first["truncated"] and not second["truncated"]
    assert [r["seq"] for r in first["reports"] + second["reports"]] == [4, 3, 2, 1]
    assert forget(db, "VARYZ", "claude-session") == 4
    assert history(db, HistoryQuery(project="varyz", topic="SEO"))["found"] == 0
    assert db.execute("SELECT COUNT(*) FROM session_progress_fts").fetchone()[0] == 0


def test_fts_operators_are_literal_words(db):
    record(db, checkpoint(), agent_id="claude")
    assert history(db, HistoryQuery(project="varyz", topic='SEO OR "*"'))["found"] == 0
    with pytest.raises(ValueError, match="searchable"):
        history(db, HistoryQuery(project="varyz", topic="***"))


def test_sensitive_history_paginates_even_when_entire_page_is_withheld(db):
    record(db, checkpoint(), agent_id="claude")
    for n in range(3):
        record(db, checkpoint(f"private-{n}", sensitivity="sensitive"), agent_id="claude")
    query = HistoryQuery(project="varyz", conversation_id="claude-session", latest=False, limit=2)
    first = history(db, query)
    assert first["withheld_sensitive"] == 2 and first["reports"] == []
    assert first["truncated"] and first["next_before_seq"] == 3
    second = history(db, query.model_copy(update={"before_seq": first["next_before_seq"]}))
    assert second["withheld_sensitive"] == 1 and second["found"] == 1


def test_partial_transaction_cannot_leave_unsearchable_checkpoint(db):
    db.execute("DROP TABLE session_progress_fts")
    with pytest.raises(Exception, match="no such table"):
        record(db, checkpoint(), agent_id="claude")
    assert db.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 0


def test_query_rejects_cursor_without_session():
    with pytest.raises(ValidationError, match="before_seq"):
        HistoryQuery(project="varyz", before_seq=3)


def test_cli_owner_controls(tmp_path):
    from typer.testing import CliRunner

    from varyz.session_progress_cli import app

    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"]
    result = runner.invoke(app, ["latest", "varyz", "--topic", "SEO", "--config", str(path)])
    assert result.exit_code == 0 and json.loads(result.output)["found"] == 0
    for command in ("pause", "resume"):
        assert runner.invoke(app, [command, "--config", str(path)]).exit_code == 0


def test_instruction_surfaces_teach_concise_checkpoints():
    from importlib import resources

    from varyz.hooks import SESSION_START_CONTEXT
    from varyz.mcp_server import INSTRUCTIONS

    skill = (resources.files("varyz") / "skills/varyz-context/SKILL.md").read_text("utf-8")
    for instructions in (SESSION_START_CONTEXT, INSTRUCTIONS, skill):
        assert "record_progress" in instructions
        assert "session_history" in instructions
        assert "checkpoint_id" in instructions


def test_doctor_detects_missing_search_row(tmp_path):
    from typer.testing import CliRunner

    from varyz.config import load_config
    from varyz.session_progress_cli import app

    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    conn = open_db(load_config(path).db_path)
    record(conn, checkpoint(), agent_id="claude")
    conn.execute("DELETE FROM session_progress_fts")
    conn.commit()
    conn.close()
    result = CliRunner().invoke(app, ["doctor", "--config", str(path)])
    assert result.exit_code == 1
    assert json.loads(result.output)["index_mismatches"] == 1


def test_mcp_protocol_identity_pause_revocation_and_discovery(tmp_path, monkeypatch):
    # New tools never need the inference models, background sync or a daemon.
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    server = VaryzServer(config, agent_token="")
    monkeypatch.setattr(server, "_promotion_available", lambda: False)
    try:
        assert server.call("record_progress", checkpoint().model_dump())["recorded"] is False
        credential = register_agent(server.conn, agent_id="claude", display_name="Claude")
        server._agent_token = credential.token
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "record_progress", "arguments": checkpoint().model_dump()}}
        response = handle(server, request)
        assert response["result"]["structuredContent"]["recorded"] is True
        assert server.call("session_history", {"project": "varyz"})["reports"][0][
            "agent"] == "claude"
        pause_path(server.cfg.data_dir).touch()
        result = server.call("record_progress", checkpoint("paused").model_dump())
        assert result["status"] == "paused"
        assert server.call("session_history", {"project": "varyz"})["found"] == 1
        revoke_agent(server.conn, "claude")
        assert "error" in server.call("session_history", {"project": "varyz"})
        assert server.conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 1
        assert not server._embedder_loaded
        secret = "sk-ant-" + "b" * 30
        invalid = checkpoint().model_dump() | {"task": secret * 20, secret: "unexpected"}
        response = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "record_progress", "arguments": invalid}})
        # A revoked token is denied before payload validation.
        assert secret not in json.dumps(response)
        tools = {t["name"]: t for t in TOOLS}
        assert tools["record_progress"]["annotations"]["idempotentHint"]
        assert tools["session_history"]["annotations"]["readOnlyHint"]
    finally:
        server.close()


def test_invalid_mcp_payload_does_not_echo_secrets(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    server = VaryzServer(config, agent_token="")
    monkeypatch.setattr(server, "_promotion_available", lambda: False)
    try:
        credential = register_agent(server.conn, agent_id="claude", display_name="Claude")
        server._agent_token = credential.token
        secret = "sk-ant-" + "b" * 30
        invalid = checkpoint().model_dump() | {"task": secret * 20, secret: "unexpected"}
        response = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "record_progress", "arguments": invalid}})
        assert response["result"]["isError"]
        assert secret not in json.dumps(response)
    finally:
        server.close()
