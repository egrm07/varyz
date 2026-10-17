"""First-use questions must reach learning, and news counts conversations rather than turns."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.test_profile_reader import (
    THIN,
    _agent_server,
    _item,
    _merged,
    _shop,
    daemon_client,  # noqa: F401
)
from varyz import hooks, news, onboarding
from varyz.config import load_config
from varyz.db.connection import open_db

INTRO = "hey i just installed varyz, what now"
T0 = datetime(2026, 10, 16, 12, tzinfo=UTC).timestamp()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n[resolution]\nautostart = false\n',
                    encoding="utf-8")
    cfg = load_config(path)
    conn = open_db(cfg.db_path)
    conn.close()
    return cfg


@pytest.mark.parametrize("text", [INTRO, "I connected Varyz. What should I do?",
    "How do I start using Varyz?", onboarding.LEARNING_MESSAGE])
def test_first_use_questions_are_recognized(text, config):
    assert onboarding.is_request(text, config)


@pytest.mark.parametrize("text", ["What does Varyz do?", "Fix the Varyz setup style",
    "I installed Python, what now?", "Why are Varyz news shown so early?"])
def test_unrelated_questions_are_not_onboarding(text):
    assert not onboarding.is_request(text)


def test_intro_bypasses_the_task_classifier_and_news(config, monkeypatch):
    monkeypatch.setattr(hooks, "is_task", lambda *a: pytest.fail("onboarding was classified away"))
    monkeypatch.setattr(news, "agent_notice", lambda *a, **k: pytest.fail("onboarding showed news"))
    result = hooks.user_prompt(config, {"prompt": INTRO, "session_id": "welcome"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Call start_task now" in context and "Start approved pending" in context


def test_the_ordinary_first_task_hook_delivers_the_learning_job(config, monkeypatch):
    conn = open_db(config.db_path)
    _shop(conn)
    conn.close()
    monkeypatch.setattr(hooks, "is_task", lambda *a: True)
    monkeypatch.setattr("varyz.resolution.task_size.sizer_from_config", lambda cfg: None)
    if hasattr(hooks, "nudge_live_reading"):
        monkeypatch.setattr(hooks, "nudge_live_reading", lambda *a: None)
    result = hooks.user_prompt(config, {"prompt": "build a settings page", "session_id": "work"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "profile_reading_task" in context and "run_in_background" in context


def test_mcp_intro_reoffers_an_ignored_learning_job_and_confirms_actual_start(
    tmp_path, daemon_client  # noqa: F811
):
    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    try:
        first = server.call("start_task", {"task": "add a settings page"})
        assert first["background_job"]
        # The earlier offer was ignored; that is not proof that any reading happened.
        response = server.call("start_task", {"task": "Varyz?", "conversation_id": "welcome"})
        assert response["background_job"]
        assert response["onboarding"]["state"] == "start_learning"
        assert "before replying" in response["onboarding"]["do"]
        read = server.call("profile_reading_task", {})
        assert read["kind"] == "read" and read["turns_left_to_read"] == 3
        next_response = server.call("start_task", {"task": INTRO, "conversation_id": "welcome"})
        assert "background_job" not in next_response
        # Finish the real read/merge protocol rather than just announcing a start.
        noted = server.call("submit_profile_reading", {"task_id": read["task_id"], "items": [
            _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts"))]})
        assert noted["noted"] == 1
        # Finish both project and cross-project merges when present.
        for _ in range(3):
            merge = server.call("profile_reading_task", {})
            if merge.get("done"):
                break
            assert merge["kind"] == "merge"
            server.call("submit_profile_reading", {"task_id": merge["task_id"],
                        "items": [_merged("rule", THIN, 1)]})
        else:
            pytest.fail("learning never finished its merge")
        done = server.call("start_task", {"task": "Varyz?"})
        assert done["onboarding"]["state"] == "ready_to_review"
        status = server.call("profile_reading_status", {})
        assert status["complete"]
        assert status["messages_remaining"] == status["merges_remaining"] == 0
        followup = status["on_complete"]
        assert followup["review_question"] == "Would you like to review your preferences now?"
        assert followup["suggested_task"]["prompt"] == "Where did we leave off on shop?"
        assert followup["suggested_task"]["source_entry_id"] == "aim"
        assert followup["suggested_task"]["source_date"].startswith("2026-09-03")
        assert done["onboarding"]["suggested_task"] == followup["suggested_task"]
        assert done["onboarding"]["first_run_rules"] == list(onboarding.FIRST_RUN_RULES)
        later = server.call("start_task", {"task": "What does Varyz do?"})
        assert "onboarding" not in later
    finally:
        server.close()


def test_paused_onboarding_never_requests_a_reader(tmp_path, daemon_client):  # noqa: F811
    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    pause = hooks.pause_path(server.cfg)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text("paused", encoding="utf-8")
    try:
        response = server.call("start_task", {"task": INTRO})
        assert response["onboarding"]["state"] == "paused"
        assert "background_job" not in response
    finally:
        server.close()


def test_local_onboarding_keeps_chats_on_the_device(tmp_path, daemon_client):  # noqa: F811
    server = _agent_server(tmp_path, daemon_client)
    server.cfg.workflows.inference_mode = "local"
    _shop(server.conn)
    try:
        response = server.call("start_task", {"task": INTRO})
        assert response["onboarding"]["state"] == "local_learning"
        assert "background_job" not in response
    finally:
        server.close()


def test_intro_rescues_a_setup_job_waiting_on_unavailable_usage(tmp_path, daemon_client):  # noqa: F811
    from varyz.resolution import headless_reader

    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    headless_reader.update_status(server.cfg, lambda state: state.update(
        state="waiting_for_usage", pending=["full"], setup_runner="claude",
        reason="claude: usage unknown", next_check=T0 + 999999))
    try:
        response = server.call("start_task", {"task": INTRO})
        assert response["background_job"]
        assert response["onboarding"]["state"] == "start_learning"
        assert headless_reader.load_status(server.cfg)["setup_agent_fallback"]
        assert server.call("profile_reading_task", {})["kind"] == "read"
    finally:
        server.close()


def test_intro_resumes_old_jobs_blocked_only_by_percentage_thresholds(tmp_path, daemon_client):  # noqa: F811
    from varyz.resolution import headless_reader
    from varyz.resolution.usage import Usage, Window

    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    used = Usage("claude", Window(99), Window(0), T0, "test").as_dict()
    headless_reader.update_status(server.cfg, lambda state: state.update(
        state="waiting_for_usage", pending=["full"], setup_runner="claude",
        reason="claude: 1% five-hour allowance left", usage={"claude": used}))
    try:
        response = server.call("start_task", {"task": INTRO})
        assert response["onboarding"]["state"] == "start_learning"
        assert response["background_job"]
        assert headless_reader.load_status(server.cfg).get("setup_agent_fallback")
    finally:
        server.close()


def cache_news(cfg):
    payload = {"version": 1, "items": [{"id": "2026-10-16-update", "date": "2026-10-16",
        "title": "Varyz news", "summary": "A new feature", "url": "https://varyzai.com/news/"}]}
    assert news.check(cfg, get=lambda _: json.dumps(payload).encode()).status == "ok"


def test_first_three_conversations_never_show_or_consume_news(config):
    cache_news(config)
    later = datetime.fromtimestamp(T0 + 2 * 86400, UTC)
    for number in range(1, 4):
        for _ in range(5):
            onboarding.note_conversation(config, f"chat-{number}", now=T0)
        assert news.agent_notice(config, now=later, conversation_id=f"chat-{number}") is None
        assert len(news.unseen(config, now=later)) == 1
    onboarding.note_conversation(config, "chat-4", now=T0 + 2 * 86400)
    assert news.agent_notice(config, now=later, conversation_id="chat-4")
    assert not news.unseen(config, now=later)


def test_four_conversations_on_install_day_still_stay_quiet(config):
    cache_news(config)
    for number in range(4):
        onboarding.note_conversation(config, f"chat-{number}", now=T0)
    assert news.agent_notice(config, now=datetime.fromtimestamp(T0 + 60, UTC),
                             conversation_id="chat-3") is None


def test_onboarding_stays_quiet_even_after_the_initial_quiet_period(config):
    cache_news(config)
    for number in range(4):
        onboarding.note_conversation(config, f"chat-{number}", now=T0)
    onboarding.note_conversation(config, "setup-help", onboarding=True, now=T0 + 2 * 86400)
    assert news.agent_notice(config, now=datetime.fromtimestamp(T0 + 2 * 86400, UTC),
                             conversation_id="setup-help") is None


def test_missing_conversation_ids_cannot_count_tool_calls_as_conversations(config):
    for _ in range(10):
        onboarding.note_conversation(config, None, now=T0)
    assert not onboarding.news_allowed(config, now=T0 + 2 * 86400)


@pytest.mark.parametrize("mode", ["conversations", "local"])
def test_suggestion_uses_only_read_cloud_safe_project_history(config, mode):
    from tests.test_profile_reader import _entry
    from varyz.resolution.profile_reader import ProfileState, profile_path

    config.workflows.inference_mode = mode
    conn = open_db(config.db_path)
    try:
        _entry(conn, "safe", "We need to finish the project settings page next.", project="harbor")
        _entry(conn, "private", "We need to finish this private project next.", day=2,
               project="private-project", sensitivity="sensitive")
        _entry(conn, "unknown", "We need to finish this unclassified project next.", day=3,
               project="unknown-project", sensitivity="unclassified")
        _entry(conn, "unread", "We need to finish the new project settings page next.", day=4,
               project="new-project")
        profile = ProfileState(profile_path(config, "local" if mode == "local" else "cloud"))
        profile.read = {key: key for key in ["safe", "private", "unknown"]}
        profile.save()
        suggestion = onboarding.completion_followup(config, conn)["suggested_task"]
        assert suggestion["project"] == "harbor"
        # Reclassification and deletion invalidate a previously read example.
        conn.execute("UPDATE entries SET sensitivity = 'sensitive' WHERE id = 'safe'")
        conn.commit()
        fallback = onboarding.completion_followup(config, conn)["suggested_task"]
        assert "project" not in fallback
        assert fallback["prompt"] == "What have you learned about how I like to work?"
    finally:
        conn.close()


def test_incomplete_learning_has_no_review_or_example_task(config):
    conn = open_db(config.db_path)
    try:
        _shop(conn)
        status = onboarding.learning_status(config, conn)
        assert not status["complete"] and "on_complete" not in status
        welcome = onboarding.for_agent(config, conn, None)
        assert "suggested_task" not in welcome
        assert welcome["first_run_rules"]
    finally:
        conn.close()


def test_user_sees_only_a_short_message_and_instructions_stay_in_agent_context():
    from html import unescape

    from varyz.setup_web import render_page

    page = render_page("preview").decode()
    assert onboarding.LEARNING_MESSAGE == "Help me with Varyz"
    assert onboarding.LEARNING_MESSAGE in unescape(page)
    assert onboarding.FIRST_RUN_FINISH not in unescape(page)
    assert "profile_reading_status verifies completion" not in page
    assert "{{LEARNING_MESSAGE}}" not in page
    assert all(rule in hooks.SESSION_START_CONTEXT for rule in onboarding.FIRST_RUN_RULES)


@pytest.mark.parametrize("text", ["Varyz?", "varyz", "Help me with Varyz", "hello varyz",
                                 "What does Varyz do?", "Can Varyz help me?", "varyzai?"])
def test_any_varyz_mention_starts_first_use_before_task_classification(config, monkeypatch, text):
    monkeypatch.setattr(hooks, "is_task", lambda *a: pytest.fail("first use was classified away"))
    monkeypatch.setattr(news, "agent_notice", lambda *a, **k: pytest.fail("first use showed news"))
    assert onboarding.is_request(text, config)
    response = hooks.user_prompt(config, {"prompt": text, "session_id": "first-use"})
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Call start_task now" in context
    assert "Would you like to review your preferences now?" in context
    assert "one specific question grounded in the imported history" in context


def test_a_vague_first_message_reaches_the_real_mcp_learning_job(tmp_path, daemon_client):  # noqa: F811
    server = _agent_server(tmp_path, daemon_client)
    _shop(server.conn)
    try:
        welcome = server.call("start_task", {"task": "Varyz?"})
        assert welcome["onboarding"]["state"] == "start_learning"
        assert welcome["onboarding"]["first_run_rules"] == list(onboarding.FIRST_RUN_RULES)
        assert welcome["background_job"]
        assert server.call("profile_reading_task", {})["kind"] == "read"
    finally:
        server.close()


def test_a_completed_first_run_does_not_repeat_for_ordinary_mentions(config):
    from varyz.resolution.profile_reader import ProfileState, profile_path

    profile = ProfileState(profile_path(config, "cloud"))
    profile.save()
    conn = open_db(config.db_path)
    try:
        # Headless status checks cannot consume the conversational first-run handoff.
        assert onboarding.learning_status(config, conn)["complete"]
        assert onboarding.is_request("Varyz?", config)
        welcome = onboarding.for_agent(config, conn, None)
        assert welcome["state"] == "ready_to_review"
        assert not onboarding.is_request("What does Varyz do?", config)
        reloaded = load_config(config.data_dir.parent / "config.toml")
        assert not onboarding.is_request("Fix Varyz setup", reloaded)
        # An explicit request for onboarding remains available after the first run.
        assert onboarding.is_request(INTRO, config)
    finally:
        conn.close()


def test_mcp_startup_delivers_hidden_first_run_instructions(tmp_path, daemon_client):  # noqa: F811
    from varyz.mcp_server import handle

    server = _agent_server(tmp_path, daemon_client)
    try:
        response = handle(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05",
                                             "capabilities": {},
                                             "clientInfo": {"name": "fixture", "version": "1"}}})
        instructions = response["result"]["instructions"]
        assert onboarding.GUIDANCE in instructions
        assert all(rule in instructions for rule in onboarding.FIRST_RUN_RULES)
    finally:
        server.close()


def test_only_conversational_status_consumes_the_completion_handoff(
    tmp_path, daemon_client, monkeypatch  # noqa: F811
):
    from varyz.resolution.headless_reader import ENV
    from varyz.resolution.profile_reader import ProfileState, profile_path

    server = _agent_server(tmp_path, daemon_client)
    ProfileState(profile_path(server.cfg, "cloud")).save()
    try:
        monkeypatch.setenv(ENV, "fixture-unattended-reader")
        assert server.call("profile_reading_status", {})["complete"]
        assert onboarding.is_request("Varyz?", server.cfg)
        monkeypatch.delenv(ENV)
        status = server.call("profile_reading_status", {})
        assert status["complete"] and status["on_complete"]["suggested_task"]
        assert not onboarding.is_request("Varyz?", server.cfg)
    finally:
        server.close()
