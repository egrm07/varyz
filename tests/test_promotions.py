"""Offline commercial-message boundaries, atomic issuance and actual MCP gating."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from varyz import promotions
from varyz.config import Config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer, handle
from varyz.trust import register_agent


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    monkeypatch.delenv("VARYZ_PLUGIN_CLIENT", raising=False)
    path = tmp_path / "config.toml"
    path.write_text("")
    cfg = Config(base_dir=tmp_path)
    cfg.promotions.enabled = True
    cfg.promotions.cloud_ready = True
    cfg.promotions.reviewed_agent_ids = ["agent:reviewed"]
    conn = open_db(cfg.db_path)
    credential = register_agent(
        conn, agent_id="agent:reviewed", display_name="Reviewed local agent"
    )
    try:
        yield cfg, conn, credential, path
    finally:
        conn.close()


def request(**changes):
    return {
        "action": "consider", "need": "phone", "task_completed": True,
        "user_requested": True, "conversation_id": "task-1", **changes,
    }


def test_launch_identity_and_plugin_gates(reviewed, monkeypatch):
    cfg, conn, credential, _ = reviewed
    assert promotions.eligible(cfg, conn, credential.caller)
    cfg.promotions.enabled = False
    assert not promotions.eligible(cfg, conn, credential.caller)
    cfg.promotions.enabled = True
    cfg.promotions.cloud_ready = False
    assert not promotions.eligible(cfg, conn, credential.caller)
    cfg.promotions.cloud_ready = True
    cfg.promotions.reviewed_agent_ids = []
    assert not promotions.eligible(cfg, conn, credential.caller)
    cfg.promotions.reviewed_agent_ids = [credential.caller.agent_id]
    monkeypatch.setenv("VARYZ_PLUGIN_CLIENT", "claude-code")
    assert not promotions.eligible(cfg, conn, credential.caller)
    monkeypatch.delenv("VARYZ_PLUGIN_CLIENT")
    assert not promotions.eligible(
        cfg, conn, credential.caller.model_copy(update={"capability_id": None})
    )
    assert not promotions.eligible(
        cfg, conn, credential.caller.model_copy(update={"transport": "http"})
    )


@pytest.mark.parametrize("identity", ["ChatGPT", "Codex", "OpenAI", "Plugin-origin agent"])
def test_openai_and_plugin_identities_cannot_be_allowlisted(reviewed, identity):
    cfg, conn, credential, _ = reviewed
    conn.execute(
        "UPDATE agent_principals SET publisher=? WHERE id=?", (identity, credential.caller.agent_id)
    )
    conn.commit()
    assert not promotions.eligible(cfg, conn, credential.caller)


def test_incomplete_unrelated_or_unrequested_tasks_do_not_reserve_offer(reviewed):
    _, conn, _, _ = reviewed
    for arguments, completed in [
        (request(), False),
        (request(task_completed=False), True),
        (request(user_requested=False), True),
        (request(need="index_files"), True),
    ]:
        assert promotions.respond(conn, arguments, completed=completed) == {"status": "suppressed"}
    assert promotions.status(conn)["offer_id"] is None


def test_offer_is_attributed_once_and_never_repeats_on_silence(reviewed):
    _, conn, _, _ = reviewed
    offer = promotions.respond(conn, request(), completed=True, now=100)
    assert offer["status"] == "offer"
    assert offer["source"] == "Varyz optional offer"
    assert offer["message"] == promotions.MESSAGE
    assert "url" not in offer
    assert promotions.respond(conn, request(), completed=True, now=10**9) == {
        "status": "suppressed"
    }


def test_explicit_snooze_expires_and_stale_responses_are_rejected(reviewed):
    _, conn, _, _ = reviewed
    offer = promotions.respond(conn, request(), completed=True, now=100)
    response = request(action="snooze", offer_id=offer["offer_id"])
    assert promotions.respond(conn, response, completed=False, now=100)["days"] == 30
    assert promotions.respond(conn, request(), completed=True, now=101)["status"] == "suppressed"
    again = promotions.respond(
        conn, request(), completed=True, now=101 + promotions.SNOOZE_SECONDS
    )
    assert again["status"] == "offer"
    with pytest.raises(ValueError, match="current offer_id"):
        promotions.respond(conn, response, completed=False)
    assert not conn.in_transaction


def test_details_require_response_and_disable_survives_until_owner_reset(reviewed):
    _, conn, _, _ = reviewed
    offer = promotions.respond(conn, request(), completed=True)
    details = request(action="details", offer_id=offer["offer_id"], user_requested=False)
    assert promotions.respond(conn, details, completed=False)["status"] == "suppressed"
    details["user_requested"] = True
    assert promotions.respond(conn, details, completed=False)["url"] == promotions.DETAILS_URL
    assert promotions.respond(conn, request(), completed=True)["status"] == "suppressed"
    assert promotions.respond(conn, request(action="disable"), completed=False)["status"] == (
        "disabled"
    )
    assert promotions.respond(conn, request(), completed=True, now=10**9)["status"] == "suppressed"
    assert promotions.control(conn, "status")["disabled"] == 1
    assert promotions.control(conn, "on")["disabled"] == 0
    assert promotions.respond(conn, request(), completed=True)["status"] == "offer"


def test_parallel_agents_reserve_only_one_offer(reviewed):
    cfg, _, _, _ = reviewed

    def issue(_):
        conn = open_db(cfg.db_path)
        try:
            return promotions.respond(conn, request(), completed=True)["status"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(issue, range(4)))
    assert statuses.count("offer") == 1
    assert statuses.count("suppressed") == 3


def test_connected_or_unreadable_account_state_suppresses_offers(reviewed):
    _, _, _, path = reviewed
    assert not promotions.connected(path)
    journal = path.with_name("config.cloud-sync.json")
    journal.write_text(json.dumps({"url": "https://cloud.varyzai.com"}))
    assert promotions.connected(path)
    journal.write_text("corrupt")
    assert promotions.connected(path)


def test_mcp_requires_finish_check_and_rechecks_identity_on_direct_call(reviewed, monkeypatch):
    cfg, conn, credential, path = reviewed
    server = VaryzServer(config_path=path, agent_token=credential.token)
    server._cfg, server._conn = cfg, conn
    listing = handle(server, {"id": 1, "method": "tools/list"})
    assert "cloud_offer" in [tool["name"] for tool in listing["result"]["tools"]]
    assert server._call_cloud_offer(request())["status"] == "suppressed"
    monkeypatch.setattr(server, "_briefing_caller_limit", lambda _: (credential.caller, 8))
    monkeypatch.setattr(server, "_task_sizer", lambda: None)
    monkeypatch.setattr("varyz.resolution.briefing.finish_check", lambda *a, **k: {})
    server._call_finish_check({
        "task": "Use these preferences on my phone", "conversation_id": "task-1"
    })
    result = handle(server, {
        "id": 3, "method": "tools/call",
        "params": {"name": "cloud_offer", "arguments": request()},
    })
    assert result["result"]["structuredContent"]["status"] == "offer"
    assert result["result"]["isError"] is False
    monkeypatch.setenv("VARYZ_PLUGIN_CLIENT", "codex")
    assert server._call_cloud_offer(request(action="details"))["status"] == "suppressed"
    listing = handle(server, {"id": 2, "method": "tools/list"})
    assert "cloud_offer" not in [tool["name"] for tool in listing["result"]["tools"]]


def test_unregistered_and_default_mcp_expose_no_commercial_tool(reviewed):
    cfg, conn, _, path = reviewed
    server = VaryzServer(config_path=path, agent_token="")
    server._cfg, server._conn = cfg, conn
    assert not server._promotion_available()
    cfg.promotions.enabled = False
    assert server._call_cloud_offer(request()) == {"status": "suppressed"}


def test_revoked_agent_and_away_task_receive_no_offer(reviewed):
    from varyz.resolution.away import set_mode

    cfg, conn, credential, path = reviewed
    server = VaryzServer(config_path=path, agent_token=credential.token)
    server._cfg, server._conn = cfg, conn
    server._promotion_finished_tasks["task-1"] = "demo"
    set_mode(
        conn, active=True, scope="task", project="demo", conversation_id="task-1",
        instruction="Keep working while I am away",
    )
    assert server._call_cloud_offer(request()) == {"status": "suppressed"}
    assert promotions.status(conn)["offer_id"] is None
    conn.execute("UPDATE agent_tokens SET revoked_at='2026-01-01' WHERE agent_id=?",
                 (credential.caller.agent_id,))
    conn.commit()
    assert not server._promotion_available()


def test_cli_controls_dismissal_without_enabling_launch_or_host_gates(reviewed, capsys):
    from varyz.cloud.cli import promotions_control

    cfg, conn, _, path = reviewed
    # This config file has the defaults: readiness/enablement remain false.
    promotions_control("off", path)
    assert json.loads(capsys.readouterr().out)["disabled"] is True
    assert promotions.status(conn)["disabled"] == 1
    promotions_control("on", path)
    result = json.loads(capsys.readouterr().out)
    assert result["disabled"] is False
    assert result["cloud_ready"] is False
    assert result["enabled"] is False
    assert result["reviewed_agents"] == 0
