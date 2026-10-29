"""Offline contracts for hand-offs: stop-time capture and "where you left off"."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from varyz import handoff, hooks
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.session_miner import approval_scope, privacy_scope
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
    assert [a[1:] for a in started] == [("varyz", "s", "t.jsonl", None)]


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


def test_another_agent_picks_up_and_closes_a_thread_without_cloud(setup, monkeypatch):
    path, cfg, conn = setup
    record(conn, checkpoint("a", pending=["Submit sitemap"]), agent_id="claude")
    server = VaryzServer(path, agent_token="")
    monkeypatch.setattr(server, "_promotion_available", lambda: False)
    try:
        server._agent_token = register_agent(server.conn, agent_id="codex",
                                             display_name="Codex").token
        [thread] = server.call("catch_up", {"project": "varyz", "conversation_id": "codex"})[
            "threads"]
        pickup = {"project": "varyz", "thread_id": thread["thread_id"]}
        claimed = server.call("claim_work", {**pickup, "conversation_id": "codex"})
        assert claimed["claimed"] and claimed["cloud"] == "off"
        taken = server.call("claim_work", {**pickup, "conversation_id": "other"})
        assert taken["conflict"] and not taken["claimed"]
        step = {"project": "varyz", "conversation_id": "codex", "task": "Improve SEO",
                "thread_id": thread["thread_id"]}
        assert server.call("record_progress", {**step, "checkpoint_id": "1",
                                               "pending": ["Check Bing"]})["recorded"]
        [seen] = handoff.open_work(cfg, path, project="varyz", conversation_id="third")[
            "threads"]
        assert seen["pending"] == ["Check Bing"] and seen["claimed"]
        assert "claimed by another session" in handoff.render(
            {"project": "varyz", "threads": [seen]})
        # Codex's own chat does not list the thread it picked up as someone else's.
        assert handoff.open_work(cfg, path, project="varyz", conversation_id="codex")[
            "threads"] == []
        assert server.call("record_progress", {**step, "checkpoint_id": "2",
                                               "status": "complete"})["recorded"]
        assert handoff.open_work(cfg, path, project="varyz", conversation_id="third")[
            "threads"] == []
        assert not handoff.claim(cfg, project="varyz", thread_id=thread["thread_id"],
                                 conversation_id="x", action="status")["claimed"]
        with pytest.raises(ValueError, match="no longer open"):
            handoff.claim(cfg, project="varyz", thread_id=thread["thread_id"],
                          conversation_id="x")
    finally:
        server.close()


def test_claims_lapse_so_work_is_never_stuck(setup, monkeypatch):
    _, cfg, conn = setup
    record(conn, checkpoint("a", pending=["Step"]), agent_id="claude")
    [thread] = handoff._local_threads(cfg, "varyz")
    first = {"project": "varyz", "thread_id": thread["thread_id"]}
    assert handoff.claim(cfg, **first, conversation_id="out-of-usage")["claimed"]
    assert handoff.claim(cfg, **first, conversation_id="next")["conflict"]
    monkeypatch.setattr(handoff, "CLAIM_SECONDS", 0)
    assert handoff.claim(cfg, **first, conversation_id="out-of-usage", action="renew")["claimed"]
    assert handoff.claim(cfg, **first, conversation_id="next")["claimed"]


def test_threads_the_task_names_come_before_newer_ones(setup):
    path, cfg, conn = setup
    record(conn, checkpoint("a", pending=["Submit sitemap"]), agent_id="claude",
           _occurred_at="2026-10-01T00:00:00Z")
    record(conn, checkpoint("b", "mac", task="Build the macOS installer",
                            pending=["Ship installer"]), agent_id="codex",
           _occurred_at="2026-10-02T00:00:00Z")
    work = handoff.open_work(cfg, path, project="varyz", task="finish the sitemap")
    assert [(t["conversation_id"], t["match"]) for t in work["threads"]] == [
        ("seo", "topic"), ("mac", "recent")]
    work = handoff.open_work(cfg, path, project="varyz", task="continue")
    assert [t["conversation_id"] for t in work["threads"]] == ["mac", "seo"]


def test_cloud_outage_falls_back_to_its_last_answer_and_says_so(setup, monkeypatch):
    path, cfg, _ = setup
    monkeypatch.setattr(handoff, "_cloud_configured", lambda *a: True)
    monkeypatch.setattr(handoff, "refresh_in_background", lambda *a: None)
    work = handoff.open_work(cfg, path, project="varyz")
    assert work["cloud"] == "unavailable" and work["source"] == "local"
    handoff._write_cache(cfg, "varyz", [
        {"conversation_id": "elsewhere", "task": "Other computer", "status": "working",
         "occurred_at": "2026-10-01T00:00:00Z", "pending": ["Step"]}])
    work = handoff.open_work(cfg, path, project="varyz")
    assert work["cloud"] == "cached" and work["threads"][0]["task"] == "Other computer"
    assert "as Varyz Cloud last reported it" in handoff.render(work)
    work["cloud"], work["threads"] = "unavailable", [{"task": "X", "status": "working"}]
    assert "did not answer" in handoff.render(work)


def test_a_thread_stopped_by_usage_says_so(setup):
    path, cfg, conn = setup
    cfg.connectors["ai_conversations"] = {"approved": True}
    record(conn, checkpoint("a", pending=["Stopped before finishing", MINED_QUALIFIER]),
           agent_id="history:claude_code", _occurred_at="2026-10-01T00:00:00Z",
           _evidence={"source_agent": "claude_code", "stopped": "usage_limit",
                      "approval_scope": approval_scope(cfg),
                      "privacy_scope": privacy_scope(cfg)})
    work = handoff.open_work(cfg, path, project="varyz", conversation_id="codex")
    assert work["threads"][0]["stopped"] == "usage_limit"
    assert "stopped mid-task when its usage ran out" in handoff.render(work)


def test_sweep_starts_at_most_every_ten_minutes_and_briefing_waits_briefly(setup,
                                                                           monkeypatch):
    path, cfg, _ = setup
    spawned = []
    monkeypatch.setattr(handoff, "_spawn", lambda *a: spawned.append(a))
    assert handoff.sweep_in_background(cfg, path, "varyz")
    assert not handoff.sweep_in_background(cfg, path, "varyz")
    assert spawned[0][1][:3] == ["sweep", "--project", "varyz"]
    monkeypatch.setattr(handoff, "SWEEP_WAIT_SECONDS", 0.3)
    handoff.wait_for_sweep(cfg, "varyz")  # still marked running: gives up after 0.3s
    monkeypatch.setattr(handoff, "can_capture", lambda cfg: False)
    assert handoff.sweep(cfg, path, project="varyz") == {"skipped": "not_approved_or_paused"}
    assert handoff._sweep_marker(cfg, "varyz").read_text(encoding="utf-8") == "done"


def test_low_usage_labels_a_thread_whose_report_was_already_mined(setup):
    # The capture at 5% finds the chat unchanged since its stop-time capture, so
    # no new report is written: the label has to come from the signal.
    path, cfg, conn = setup
    approved(cfg, conn, "a", "2026-10-01T12:00:00Z", ["Submit sitemap"])
    work = handoff.open_work(cfg, path, project="varyz", conversation_id="other")
    assert not work["threads"][0].get("stopped")
    assert handoff.signal(cfg.data_dir, project="Varyz", conversation_id="seo",
                          kind="usage_low", left=4.04, agent="claude_code",
                          at=_seconds("2026-10-01T12:00:30Z"),
                          resets_at=_seconds("2026-10-01T15:00:00Z"))
    [thread] = handoff.open_work(cfg, path, project="varyz", conversation_id="other")["threads"]
    assert thread["stopped"] == "usage_low" and thread["usage_left"] == 4.0
    assert "its usage was nearly out, so it may have stopped mid-task (4% left)" in (
        handoff.render({"project": "varyz", "threads": [thread]}))


def test_a_usage_signal_goes_stale_once_the_agent_works_on(setup):
    path, cfg, conn = setup
    handoff.signal(cfg.data_dir, project="varyz", conversation_id="seo", kind="usage_low",
                   left=3, at=_seconds("2026-10-01T12:00:30Z"),
                   resets_at=_seconds("2026-10-01T15:00:00Z"))
    approved(cfg, conn, "after-reset", "2026-10-01T16:00:00Z", ["Deploy"])
    [thread] = handoff.open_work(cfg, path, project="varyz")["threads"]
    assert not thread.get("stopped")
    handoff.signal(cfg.data_dir, project="varyz", conversation_id="seo", kind="usage_limit",
                   at=_seconds("2026-10-01T15:30:00Z"))  # an old stop, re-mined
    [thread] = handoff.open_work(cfg, path, project="varyz")["threads"]
    assert not thread.get("stopped")


def test_an_old_signal_never_replaces_a_newer_one(setup):
    _, cfg, _ = setup
    assert handoff.signal(cfg.data_dir, project="varyz", conversation_id="seo",
                          kind="usage_low", left=4, at=_seconds("2026-10-02T00:00:00Z"))
    assert not handoff.signal(cfg.data_dir, project="varyz", conversation_id="seo",
                              kind="usage_limit", at=_seconds("2026-10-01T00:00:00Z"))
    assert handoff._signals(cfg.data_dir)["varyz\nseo"]["kind"] == "usage_low"
    assert not handoff.signal(cfg.data_dir, project="", conversation_id="seo",
                              kind="usage_low")


def test_signals_older_than_a_month_are_dropped(setup):
    _, cfg, _ = setup
    handoff.signal(cfg.data_dir, project="varyz", conversation_id="old", kind="usage_low",
                   at=time.time() - 40 * 24 * 3600)
    handoff.signal(cfg.data_dir, project="varyz", conversation_id="new", kind="usage_low")
    assert list(handoff._signals(cfg.data_dir)) == ["varyz\nnew"]


def test_corrupt_runtime_files_do_not_break_hand_offs(setup):
    path, cfg, conn = setup
    approved(cfg, conn, "a", "2026-10-01T12:00:00Z", ["Submit sitemap"])
    folder = cfg.data_dir / "runtime" / "handoff"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (("signals.json", "{not json"), ("claims.json", "[1, 2]")):
        (folder / name).write_text(text, encoding="utf-8")
    assert handoff.open_work(cfg, path, project="varyz")["threads"]
    (folder / "signals.json").write_text('{"varyz\nseo": {"kind": "other", "at": 1},'
                                         ' "x": "y"}', encoding="utf-8")
    assert not handoff.open_work(cfg, path, project="varyz")["threads"][0].get("stopped")
    assert handoff.signal(cfg.data_dir, project="varyz", conversation_id="seo",
                          kind="usage_limit")
    assert handoff.claim(cfg, project="varyz", thread_id="t", conversation_id="c")["claimed"]


def test_capture_waits_for_a_sweep_that_is_mining(setup, monkeypatch):
    path, cfg, _ = setup
    answers = [{"skipped": "busy"}, {"skipped": "busy"}, {"reports": 1, "errors": 0}]
    monkeypatch.setattr(handoff, "_mine", lambda *a, **k: answers.pop(0))
    assert handoff.capture(cfg, path, project="varyz", conversation_id="s",
                           transcript=path) == {"reports": 1, "errors": 0}
    monkeypatch.setattr(handoff, "_mine", lambda *a, **k: {"skipped": "busy"})
    monkeypatch.setattr(handoff, "CAPTURE_WAIT_SECONDS", 0.6)
    started = time.monotonic()
    assert handoff.capture(cfg, path, project="varyz", conversation_id="s",
                           transcript=path) == {"skipped": "busy"}
    assert time.monotonic() - started < 3


def test_capture_really_waits_for_the_mining_lock(setup, monkeypatch):
    import threading

    from varyz.db.connection import FileLock

    path, cfg, _ = setup
    monkeypatch.setattr(handoff, "can_capture", lambda cfg: True)
    seen = []
    monkeypatch.setattr("varyz.session_miner.mine",
                        lambda *a, **k: seen.append(k) or {"reports": 0, "errors": 0})
    lock = FileLock(cfg.data_dir / "session-mining.lock")
    assert lock.acquire()
    threading.Timer(0.7, lock.release).start()
    assert handoff.capture(cfg, None, project="varyz", conversation_id="s",
                           transcript=path) == {"reports": 0, "errors": 0}
    assert seen and seen[0]["conversation_id"] == "s"


def approved(cfg, conn, key, occurred_at, pending, conversation_id="seo"):
    cfg.connectors["ai_conversations"] = {"approved": True}
    record(conn, checkpoint(key, conversation_id, pending=[*pending, MINED_QUALIFIER]),
           agent_id="history:claude_code", _occurred_at=occurred_at,
           _evidence={"source_agent": "claude_code", "approval_scope": approval_scope(cfg),
                      "privacy_scope": privacy_scope(cfg)})


def _seconds(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
