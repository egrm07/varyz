"""Offline contracts for hand-offs: stop-time capture and "where you left off"."""

from __future__ import annotations

import pytest

from varyz import handoff, hooks
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.session_progress import (
    MINED_QUALIFIER,
    Checkpoint,
    HistoryQuery,
    history,
    left_open,
    record,
)
from varyz.trust import register_agent


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    cfg = load_config(path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    monkeypatch.setattr(handoff, "_cloud_threads", lambda *a: None)
    yield path, cfg, conn
    conn.close()


def checkpoint(key, conversation_id="seo", **fields):
    return Checkpoint.model_validate({"project": "varyz", "conversation_id": conversation_id,
                                     "checkpoint_id": key, "task": "Improve SEO", **fields})


def mined(conn, key, occurred_at, pending, conversation_id="seo", **fields):
    record(conn, checkpoint(key, conversation_id, pending=pending, **fields),
           agent_id="history:claude_code", _occurred_at=occurred_at,
           _evidence={"source_agent": "claude_code", "turn": key})


def test_left_open_ignores_bare_mined_turns():
    assert left_open({"status": "working", "provenance": "agent_report", "pending": []})
    assert not left_open({"status": "complete", "provenance": "agent_report"})
    assert not left_open({"status": "working", "provenance": "mined_report",
                          "pending": [MINED_QUALIFIER]})
    assert left_open({"status": "working", "provenance": "mined_report",
                      "pending": ["Deploy", MINED_QUALIFIER]})


def test_newest_report_is_current_and_live_wins_a_tie(setup):
    _, _, conn = setup
    record(conn, checkpoint("live", status="complete"), agent_id="claude")
    live_at = history(conn, HistoryQuery(project="varyz"))["reports"][0]["occurred_at"]
    mined(conn, "old", "2020-01-01T00:00:00Z", ["Stale"])
    assert history(conn, HistoryQuery(project="varyz"))["reports"][0]["status"] == "complete"
    mined(conn, "tie", live_at, ["Tied"])
    assert history(conn, HistoryQuery(project="varyz"))["reports"][0]["status"] == "complete"
    mined(conn, "new", "2999-01-01T00:00:00Z", ["Submit sitemap"])
    [current] = history(conn, HistoryQuery(project="varyz"))["reports"]
    assert current["pending"] == ["Submit sitemap"]


def test_open_work_is_local_without_cloud_and_skips_own_and_finished(setup):
    path, cfg, conn = setup
    record(conn, checkpoint("a", pending=["Submit sitemap"], done=["Added meta tags"],
                            artifacts=["site/index.html"]), agent_id="agent:codex:laptop")
    record(conn, checkpoint("b", "done-chat", status="complete"), agent_id="claude")
    record(conn, checkpoint("c", "this-chat", pending=["Mine"]), agent_id="claude")
    mined(conn, "d", "2026-10-01T00:00:00Z", [MINED_QUALIFIER], conversation_id="chatter")
    work = handoff.open_work(cfg, path, project="varyz", task="continue",
                             conversation_id="this-chat")
    assert work["source"] == "local"
    assert [t["conversation_id"] for t in work["threads"]] == ["seo"]
    text = handoff.render(work)
    assert "Where you left off in varyz" in text and "do not ask the owner" in text
    assert "open: Submit sitemap" in text and "(" in text and "Codex" in text
    assert "artifacts: site/index.html" in text and MINED_QUALIFIER not in text
    assert handoff.render({"project": "varyz", "threads": []}) == ""


def test_cloud_threads_come_first_and_local_fills_in(setup, monkeypatch):
    path, cfg, conn = setup
    record(conn, checkpoint("a", "local-only", pending=["Local step"]), agent_id="claude")
    record(conn, checkpoint("b", "shared", pending=["Old copy"]), agent_id="claude")
    remote = [{"conversation_id": "shared", "task": "Shared", "status": "working",
               "pending": ["Cloud step"], "thread_id": "varyz-progress:x", "match": "recent"}]
    monkeypatch.setattr(handoff, "_cloud_threads", lambda *a: remote)
    work = handoff.open_work(cfg, path, project="varyz")
    assert work["source"] == "cloud+local"
    assert [t["conversation_id"] for t in work["threads"]] == ["shared", "local-only"]
    assert "claim_work with its thread_id" in handoff.render(work)


def test_briefing_reads_cloud_copy_without_waiting_and_refreshes_old_copy(setup, monkeypatch):
    path, cfg, conn = setup
    refreshed = []
    monkeypatch.setattr(handoff, "_cloud_configured", lambda *a: True)
    monkeypatch.setattr(handoff, "refresh_in_background", lambda *a: refreshed.append(a))
    assert handoff.open_work(cfg, path, project="varyz", cloud="cached")["source"] == "local"
    assert len(refreshed) == 1  # no copy yet
    record(conn, checkpoint("a", "moved-on", status="complete"), agent_id="claude")
    handoff._write_cache(cfg, "varyz", [
        {"conversation_id": "elsewhere", "task": "Other computer", "status": "working",
         "occurred_at": "2026-10-01T00:00:00Z", "pending": ["Step"]},
        {"conversation_id": "moved-on", "task": "Finished here since", "status": "working",
         "occurred_at": "2026-10-01T00:00:00Z", "pending": ["Step"]},
    ])
    work = handoff.open_work(cfg, path, project="VARYZ", cloud="cached")
    assert [t["task"] for t in work["threads"]] == ["Other computer"]
    assert work["source"] == "cloud" and len(refreshed) == 1  # fresh copy: no refresh
    monkeypatch.setattr(handoff, "CACHE_FRESH_SECONDS", -1)
    handoff.open_work(cfg, path, project="varyz", cloud="cached")
    assert len(refreshed) == 2


def test_left_off_shows_once_per_session(setup, monkeypatch):
    _, cfg, _ = setup
    calls = []
    monkeypatch.setattr(handoff, "open_work",
                        lambda *a, **kw: calls.append(kw) or {"project": "varyz",
                                                              "threads": [{"task": "X",
                                                                           "status": "working"}]})
    first = hooks._left_off(cfg, "varyz", "session-1", "continue")
    assert first.startswith("Where you left off") and calls[0]["conversation_id"] == "session-1"
    assert hooks._left_off(cfg, "varyz", "session-1", "continue") == ""
    assert hooks._left_off(cfg, None, "session-2", "continue") == ""


def test_stop_capture_starts_only_when_approved(setup, monkeypatch):
    _, cfg, _ = setup
    started = []
    monkeypatch.setattr(handoff, "capture_in_background", lambda *a: started.append(a))
    monkeypatch.setattr(hooks, "_project", lambda event: ("varyz", None))
    event = {"session_id": "s", "transcript_path": "t.jsonl", "cwd": "."}
    monkeypatch.setattr(handoff, "can_capture", lambda cfg: False)
    hooks._capture_turn(cfg, event)
    assert started == []
    monkeypatch.setattr(handoff, "can_capture", lambda cfg: True)
    hooks._capture_turn(cfg, {**event, "transcript_path": None})
    assert started == []
    hooks._capture_turn(cfg, event)
    assert [a[1:] for a in started] == [("varyz", "s", "t.jsonl")]


def test_catch_up_tool_returns_open_work(setup, monkeypatch):
    path, _, conn = setup
    record(conn, checkpoint("a", pending=["Submit sitemap"]), agent_id="claude")
    server = VaryzServer(path, agent_token="")
    monkeypatch.setattr(server, "_promotion_available", lambda: False)
    try:
        server._agent_token = register_agent(server.conn, agent_id="claude",
                                             display_name="Claude").token
        result = server.call("catch_up", {"project": "varyz", "task": "seo"})
        assert result["threads"][0]["pending"] == ["Submit sitemap"]
        assert result["next_tool"] == "claim_work" and "not" in result["coverage"]
        with pytest.raises(ValueError, match="240"):
            server.call("catch_up", {"project": "varyz", "task": "x" * 241})
    finally:
        server.close()
