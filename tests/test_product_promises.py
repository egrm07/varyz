"""Fresh behavior and safety challenges for the five product promises."""

from contextlib import closing

import pytest

from tests.test_away import serving
from varyz import hooks
from varyz.config import Config
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.briefing import render_markdown, start_task
from varyz.resolution.continuation import brief
from varyz.resolution.knowledge import SQLiteKnowledgeStore, decision_score
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.verification import verify
from varyz.session_progress import Checkpoint, record

OWNER = Caller(
    agent_id="owner:eval", display_name="Owner", purpose="owner_knowledge", device_id="device:eval"
)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Which highlight colour belongs on our product?", True),
        ("What accent shade should this project use?", True),
        ("Which accent hue should Acorn use?", True),
        ("What background hue should this project use?", False),
        ("Which warning color should Acorn use?", False),
        ("Why did Acorn choose its accent color?", False),
        ("When did we choose Acorn's accent color?", False),
        ("Which highlight hue should Birch use?", False),
        ("Which accent color should the export panel use?", False),
    ],
)
def test_equivalent_choices_and_distinct_dimensions(query, expected):
    assert (
        decision_score(query, "Which accent colour should Acorn use?", "Amber", "acorn") is not None
    ) is expected


def snapshot(conversation="old", agent="agent:codex:eval", **fields):
    return Checkpoint(
        project="acorn",
        conversation_id=conversation,
        checkpoint_id="last",
        task="Website release",
        status="blocked",
        done=["Built pricing"],
        pending=["Domain verification required"],
        **fields,
    ), agent


def test_continuation_returns_current_state_excludes_new_session_and_names_client(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        for value, agent in (
            snapshot(),
            snapshot("new"),
            snapshot("claude", "agent:claude-code:x"),
        ):
            record(conn, value, agent_id=agent)
        result = brief(
            conn, "Resume the last Codex session", "acorn", conversation_id="new", cfg=cfg
        )
        assert result["state"]["conversation_id"] == "old"
        assert result["state"]["pending"] == ["Domain verification required"]
        assert not result["independently_verified"]
        assert brief(conn, "Build another page", "acorn", cfg=cfg) is None
        assert brief(conn, "Resume", "other", cfg=cfg)["status"] == "not_recorded"
        assert brief(conn, "Resume", "acorn", cfg=cfg, max_chars=100)["status"] == "budget_exceeded"
        assert "Domain verification" in render_markdown(
            start_task(conn, "Continue the website", project="acorn", cfg=cfg)
        )


def test_new_task_on_an_old_problem_gets_earlier_outcomes_without_continue(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        record(conn, Checkpoint(
            project="acorn", conversation_id="first-try", checkpoint_id="last",
            task="Stop the console window flashing on launch", status="working",
            done=["Switched the launcher to pythonw"],
            checks=["Console window still flashes on cold start"],
        ), agent_id="agent:codex:eval")
        record(conn, Checkpoint(
            project="acorn", conversation_id="private", checkpoint_id="last",
            task="Console window launch notes", sensitivity="sensitive",
        ), agent_id="agent:codex:eval")
        result = brief(conn, "The console window still flashes on launch", "acorn",
                       conversation_id="now", cfg=cfg)
        assert result["status"] == "related"
        assert [s["conversation_id"] for s in result["timeline"]] == ["first-try"]
        assert result["timeline"][0]["result"][0] == "Console window still flashes on cold start"
        assert result["timeline"][0]["asked"] == "Stop the console window flashing on launch"
        assert brief(conn, "The console window still flashes", "acorn",
                     conversation_id="first-try", cfg=cfg) is None
        assert brief(conn, "Write the pricing page", "acorn", cfg=cfg) is None
        assert "Earlier sessions on this topic" in render_markdown(
            start_task(conn, "Console window flashes on launch", project="acorn", cfg=cfg)
        )


def test_backstory_is_a_dated_timeline_that_shows_a_problem_came_back(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        for chat, step, asked, check in (
            ("one", "a", "Hide the console window on launch", "Launcher now uses pythonw"),
            ("one", "b", "Check the console window on cold start", "No window on warm start"),
            ("two", "a", "The console window still flashes, I tried to fix it before",
             "Hooks now call a windowless executable"),
        ):
            record(conn, Checkpoint(project="acorn", conversation_id=chat, checkpoint_id=step,
                                    task=asked, status="working", checks=[check]),
                   agent_id="agent:codex:eval")
        result = brief(conn, "Console window flashes again on launch", "acorn",
                       conversation_id="now", cfg=cfg)
        assert [s["asked"][:20] for s in result["timeline"]] == [
            "Hide the console win", "Check the console wi", "The console window s"]
        assert "in 2 session(s)" in result["summary"]
        assert "brought it back 1 time(s)" in result["summary"]


def test_earlier_sessions_share_two_topic_words_not_one_or_chat_words(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        for chat, asked in (
            ("hook", "Make the stop hook note shorter"),
            ("launch", "Plan a big launch for the persona site"),
            ("chat", "I dont see the export button, really"),
        ):
            record(conn, Checkpoint(project="acorn", conversation_id=chat, checkpoint_id="a",
                                    task=asked, status="working", checks=["Done"]),
                   agent_id="agent:codex:eval")
        result = brief(conn, "im not liking the stop hook, its really big and i dont see why",
                       "acorn", conversation_id="now", cfg=cfg)
        assert [s["conversation_id"] for s in result["timeline"]] == ["hook"]


def test_continuation_does_not_replace_withheld_latest_with_stale_state(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        value, agent = snapshot()
        record(conn, value, agent_id=agent)
        value, agent = snapshot("private", sensitivity="sensitive")
        record(conn, value, agent_id=agent)
        result = brief(conn, "Continue", "acorn", cfg=cfg)
        assert result["status"] == "withheld" and "state" not in result
        (cfg.data_dir / "session-progress.pause").touch()
        assert brief(conn, "Continue", "acorn", cfg=cfg)["status"] == "paused"


def favicon_rule():
    return [{"id": "favicon", "item": "Include an SVG favicon.", "domain": "website"}]


def test_completion_inspects_artifact_and_prevents_report_from_overriding_failure(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('<link rel="icon" href="/mark.svg">', encoding="utf-8")
    result = verify(favicon_rule(), cwd=tmp_path)
    assert result["failed"] == ["favicon"] and not result["ready_to_report"]
    (tmp_path / "mark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    passed = verify(favicon_rule(), cwd=tmp_path)
    assert passed["independently_verified"] and passed["ready_to_report"]
    page.write_text("No icon", encoding="utf-8")
    failed = verify(
        favicon_rule(),
        cwd=tmp_path,
        evidence=[
            {
                "rule_id": "favicon",
                "status": "passed",
                "evidence": "Agent says it is present",
            }
        ],
    )
    assert failed["failed"] == ["favicon"]
    assert failed["receipts"][0]["sha256"] != passed["receipts"][0]["sha256"]


@pytest.mark.parametrize(
    "href",
    [
        "../outside.svg",
        "%2e%2e/outside.svg",
        "C:/private.svg",
        "file:///private.svg",
        "https://example.com/icon.svg",
    ],
)
def test_completion_never_reads_outside_root_or_calls_network(tmp_path, href):
    (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">')
    assert not verify(favicon_rule(), cwd=tmp_path)["ready_to_report"]


def test_reported_review_is_labeled_and_unknown_or_duplicate_rule_is_rejected():
    items = [{"id": "tone", "item": "Keep copy concise.", "domain": "standing"}]
    assert verify(items)["unchecked"] == ["tone"]
    evidence = [{"rule_id": "tone", "status": "passed", "evidence": "Reviewed the draft"}]
    result = verify(items, evidence=evidence)
    assert result["status"] == "reported" and not result["independently_verified"]
    with pytest.raises(ValueError):
        verify(items, evidence=evidence * 2)
    with pytest.raises(ValueError):
        verify(items, evidence=[{**evidence[0], "rule_id": "not-a-rule"}])


def test_running_mcp_reuses_decision_resumes_and_checks_result(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir="{(tmp_path / "data").as_posix()}"\n'
        '[workflows]\nparaphrase_verifier="off"\n'
    )
    with serving(path) as (server, _):
        rules = SQLitePrincipleStore(server.conn)
        rule = rules.add(
            statement="Include an SVG favicon.",
            domain="website",
            caller=OWNER,
            source="synthetic",
            device_id="device:eval",
        )
        SQLiteKnowledgeStore(server.conn).remember(
            question="Which accent colour should Acorn use?",
            answer="Amber",
            scope="acorn",
            caller=OWNER,
            source="synthetic",
            device_id="device:eval",
            basis=AnswerBasis.EXPLICIT,
        )
        value, agent = snapshot()
        record(server.conn, value, agent_id=agent)
        args = {"project": "acorn", "conversation_id": "new"}
        result = server.call("start_task", {**args, "task": "Resume the last session"})
        assert result["continuation"]["state"]["pending"] == ["Domain verification required"]
        result = server.call(
            "ask", {**args, "question": "Which highlight hue belongs on the product?"}
        )
        assert result["answer"] == "Amber"
        result = server.call("finish_check", {**args, "task": "Build a website"})
        assert rule in result["verification"]["unchecked"]
        result = server.call(
            "finish_check",
            {
                **args,
                "task": "Build a website",
                "evidence": [
                    {"rule_id": rule, "status": "passed", "evidence": "Browser inspection passed"},
                ],
            },
        )
        assert result["verification"]["ready_to_report"]
        assert not result["verification"]["independently_verified"]


def test_incomplete_finish_result_cannot_suppress_done_gate(tmp_path):
    import json

    transcript = tmp_path / "turn.jsonl"
    events = [
        {"type": "user", "message": {"content": "Build the page"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "edit", "name": "Edit"},
                    {"type": "tool_use", "id": "finish", "name": "mcp__varyz__finish_check"},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "finish",
                        "content": json.dumps({"verification": {"ready_to_report": False}}),
                    },
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events))
    assert hooks.turn_activity(str(transcript)) == (True, False)
    events[-1]["message"]["content"][0]["content"] = json.dumps(
        {"verification": {"ready_to_report": True}}
    )
    transcript.write_text("\n".join(json.dumps(event) for event in events))
    assert hooks.turn_activity(str(transcript)) == (True, True)


def test_completion_names_page_missing_icon_and_skips_noindex_helpers(tmp_path):
    icon = '<link rel="icon" href="mark.svg">'
    (tmp_path / "mark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (tmp_path / "index.html").write_text(icon, encoding="utf-8")
    (tmp_path / "og-source.html").write_text(
        '<meta name="robots" content="noindex">', encoding="utf-8"
    )
    assert verify(favicon_rule(), cwd=tmp_path)["ready_to_report"]
    (tmp_path / "privacy.html").write_text("No icon", encoding="utf-8")
    receipt = verify(favicon_rule(), cwd=tmp_path)["receipts"][0]
    assert receipt["status"] == "failed" and "privacy.html" in receipt["evidence"]


def test_client_note_after_tool_result_keeps_the_turn(tmp_path):
    import json

    transcript = tmp_path / "turn.jsonl"
    events = [
        {"type": "user", "message": {"content": "Build the page"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "e", "name": "Edit"}]},
        },
        {"type": "user", "isMeta": True, "message": {"content": "[Image: screenshot.png]"}},
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events))
    assert hooks.turn_activity(str(transcript)) == (True, False)
