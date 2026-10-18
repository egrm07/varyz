"""Progress, provider errors and restarts with offline readers and approved synthetic chats."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from tests.test_profile_reader import (
    THIN,
    _agent_server,
    _item,
    _shop,
    daemon_client,  # noqa: F401
)
from varyz import onboarding, setup_extract
from varyz.config import load_config
from varyz.resolution import headless_reader as reader
from varyz.resolution import usage


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n[workflows]\n'
                    'reader_codex_model = "test-model"\n'
                    'large_reading_min_left_5h = 90\n'
                    'large_reading_min_left_weekly = 90\n', encoding="utf-8")
    return load_config(path), path


def used(percent=100):
    return usage.Usage("codex", usage.Window(percent, 1000), usage.Window(percent, 2000),
                       100, "test")


@pytest.mark.parametrize("allowance", [None, used(99), used(100)])
def test_low_and_unknown_allowance_do_not_block_any_reading(config, allowance):
    cfg, _ = config
    for kind in ("full", "live"):
        choice = reader.choose(kind, usage.Gate(90, 90), {"codex": allowance}, 100)
        assert choice.runner == "codex" and choice.resume_after is None
    assert usage.gates(cfg)["full"].five_hour == 0  # Old saved settings are ignored.


@pytest.mark.parametrize("allowance", [None, used(100)])
def test_running_reader_continues_past_the_old_unknown_usage_timeout(config, allowance):
    cfg, _ = config
    clock = [100.0]
    process = SimpleNamespace(pid=999, stdout=io.StringIO(""),
                              poll=lambda: 0 if clock[0] >= 300 else None)
    result = reader.run_one(cfg, reader.Runner("codex", "fake", "test-token"),
        "full", ["fake"], gate=usage.Gate(90, 90), read_usage=lambda _: allowance,
        launch=lambda *a: process, clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result.exit_code == 0 and result.stopped is None


def test_supervisor_keeps_starting_batches_until_all_work_is_complete(config, monkeypatch):
    cfg, path = config
    left = [10]
    runs = []
    monkeypatch.setattr(reader, "RUNS_PER_START", 2)
    monkeypatch.setattr(reader, "_work_left", lambda *a: (left[0], 0, 10-left[0], 8))

    def run(*a, **kw):
        runs.append(True)
        left[0] -= 1
        return reader.RunResult(0)

    monkeypatch.setattr(reader, "run_one", run)
    runners = {"codex": reader.Runner("codex", "fake", "test-token")}
    def supervise(*args):
        return reader.supervise(cfg, path, runners=runners,
                                read_usage=lambda _: used(100), clock=lambda: 100)

    state = reader.supervise(cfg, path, kind="full", runners=runners,
                             read_usage=lambda _: used(100), clock=lambda: 100)
    for _ in range(4):
        assert state["state"] == "queued" and state["pending"] == ["full"]
        resumed = []
        assert reader.maybe_resume(cfg, path, state["next_check"],
                                   start=lambda *a, resumed=resumed: resumed.append(supervise()))
        state = resumed[0]
    assert len(runs) == 10 and left[0] == 0
    assert state["state"] == "idle" and state["pending"] == []


@pytest.mark.parametrize("reported_usage", [None, used(100)])
def test_an_actual_provider_limit_is_reported_and_saved_work_waits_for_reset(
    config, monkeypatch, reported_usage
):
    cfg, path = config
    monkeypatch.setattr(reader, "_work_left", lambda *a: (200, 1, 40, 8))
    launches = []

    def launch(*args):
        launches.append(args)
        return SimpleNamespace(pid=999, poll=lambda: 1,
            stdout=io.StringIO(json.dumps({"type": "error", "message":
                                          "Usage limit reached. Try again after reset."}) + "\n"))

    state = reader.supervise(cfg, path, kind="full",
        runners={"codex": reader.Runner("codex", "fake", "test-token")},
        read_usage=lambda _: reported_usage, launch=launch, clock=lambda: 100)
    assert len(launches) == 1
    assert state["state"] == "rate_limited" and state["pending"] == ["full"]
    assert "Usage limit reached" in state["reason"]
    assert state["next_check"] == (2000 if reported_usage else 100 + reader.RECHECK_SECONDS)
    view = setup_extract.progress(cfg, {"runner": "codex", "at": 100, "turns": 240,
                                       "seconds": 100}, 110, work=(200, 1, 40, 8))
    assert view["phase"] == "waiting" and "Usage limit reached" in view["reason"]
    assert not reader.maybe_resume(cfg, path, 101, start=lambda *a: pytest.fail("retried too soon"))
    calls = []
    assert reader.maybe_resume(cfg, path, state["next_check"], start=lambda *a: calls.append(a))
    assert len(calls) == 1
    view = setup_extract.progress(cfg, {"runner": "codex", "at": 100, "turns": 240,
                                       "seconds": 100}, 110, work=(200, 1, 40, 8))
    assert view["phase"] != "done"


def test_status_does_not_treat_a_partial_reader_return_as_completion(
    tmp_path, daemon_client, monkeypatch  # noqa: F811
):
    from varyz.resolution.profile_reader import TASK_CHARS

    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    monkeypatch.setitem(TASK_CHARS, "agent", 100)
    try:
        initial = server.call("profile_reading_status", {})
        assert not initial["complete"] and initial["messages_remaining"] == 3
        read = server.call("profile_reading_task", {})
        noted = server.call("submit_profile_reading", {"task_id": read["task_id"], "items": [
            _item("rule", THIN, (1, "use thin fonts"))]})
        status = server.call("profile_reading_status", {})
        assert not noted["learning"]["complete"] and not status["complete"]
        assert status["messages_remaining"] > 0
        assert "resume" in status["do"]
        assert server.call("profile_reading_task", {})["kind"] == "read"
    finally:
        server.close()


def test_idle_worker_reason_does_not_hide_unread_agent_work(tmp_path, daemon_client):  # noqa: F811
    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    reader.update_status(server.cfg, lambda status: status.update(
        state="idle", pending=[], reason="nothing waiting to be read"))
    try:
        status = server.call("profile_reading_status", {})
        assert not status["complete"] and status["messages_remaining"] == 3
        assert "3 messages" in status["reason"] and "idle" in status["reason"]
        assert status["reason"] != status["worker_reason"]
        assert server.call("profile_reading_task", {})["kind"] == "read"
    finally:
        server.close()


def test_actual_provider_refusal_guides_the_user_instead_of_offering_premature_review(
    tmp_path, daemon_client  # noqa: F811
):
    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    reader.update_status(server.cfg, lambda state: state.update(
        state="rate_limited", pending=["full"], runner="claude",
        reason="Claude usage limit reached", next_check=2000))
    try:
        response = server.call("start_task", {"task": onboarding.LEARNING_MESSAGE})
        assert response["onboarding"]["state"] == "provider_limited"
        assert response["onboarding"]["retry_at"] == 2000
        assert "provider" in response["onboarding"]["do"]
        assert not response["onboarding"]["complete"]
        assert "background_job" not in response
    finally:
        server.close()


def test_status_requires_an_authenticated_agent(tmp_path, daemon_client):  # noqa: F811
    server = _agent_server(tmp_path, daemon_client, registered=False)
    try:
        result = server.call("profile_reading_status", {})
        assert "error" in result and not result["complete"]
        assert "messages_read" not in result
    finally:
        server.close()


def test_provider_failure_is_announced_on_the_next_question_once(config, monkeypatch):
    import time

    from varyz import hooks, news
    from varyz.db.connection import open_db

    cfg, _ = config
    conn = open_db(cfg.db_path)
    _shop(conn)
    conn.close()
    reader.update_status(cfg, lambda state: state.update(
        state="rate_limited", pending=["full"], reason="Claude usage limit reached",
        next_check=time.time() + 3600))
    monkeypatch.setattr(hooks, "is_task", lambda *a: False)
    monkeypatch.setattr(news, "maybe_check_on_use", lambda *a: None)
    monkeypatch.setattr(news, "agent_notice", lambda *a, **kw: None)
    response = hooks.user_prompt(cfg, {"prompt": "hello", "session_id": "welcome"})
    text = response["hookSpecificOutput"]["additionalContext"]
    assert "Claude usage limit reached" in text and "work is saved" in text
    assert onboarding.learning_notice(cfg) is None


def test_an_empty_supervisor_queue_is_not_a_completion_signal_when_chats_remain(config):
    cfg, _ = config
    reader.update_status(cfg, lambda state: state.update(state="idle", pending=[]))
    view = setup_extract.progress(cfg, {"runner": "codex", "at": 100, "turns": 200,
                                       "seconds": 100}, 110, work=(120, 2, 80, 8))
    assert view["phase"] == "stopped" and view["fraction"] != 1


def test_claude_result_errors_preserve_the_provider_limit_reason():
    output = reader._Output([json.dumps({"type": "result", "is_error": True,
                                       "errors": ["You have hit your usage limit"]})])
    output.join(1)
    assert reader.provider_limited(output.error)
    assert "usage limit" in output.error
