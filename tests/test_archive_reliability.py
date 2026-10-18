"""General safety and budget regressions; private archive examples stay in data/."""

import json
from contextlib import closing

import pytest

from varyz.config import Config
from varyz.db.connection import open_db
from varyz.resolution.briefing_budget import disclosed_size, fit
from varyz.resolution.continuation import brief
from varyz.resolution.knowledge import _terms, decision_score
from varyz.session_progress import Checkpoint, record


@pytest.mark.parametrize(
    "question,answer,query,expected",
    [
        (
            "Use Python, SQLite and Typer for the reporting stack?",
            "Use this approach",
            "Should reporting use Python?",
            True,
        ),
        (
            "Use Python, SQLite and Typer for the reporting stack?",
            "Use this approach",
            "Should reporting use Rust?",
            False,
        ),
        (
            "Use Python if the host is Linux?",
            "Use this approach",
            "Should the host use Python?",
            False,
        ),
        (
            "Approve uploading the Python report to GitHub?",
            "Use this approach",
            "Should the report use Python?",
            False,
        ),
        (
            "Build the Python installer with Inno Setup?",
            "Use this approach",
            "Did the owner approve this installer build?",
            False,
        ),
        (
            "Use Python for Acorn reporting?",
            "Use this approach",
            "Should Birch reporting use Python?",
            False,
        ),
        (
            "Keep the established #ab1234 and Example Sans brand?",
            "Keep the established brand",
            "Should the brand retain its existing colors and fonts?",
            True,
        ),
    ],
)
def test_literal_endorsed_subchoices_do_not_widen_scope(question, answer, query, expected):
    assert (decision_score(query, question, answer, "acorn") is not None) is expected


def test_whole_record_budget_reserves_continuation_and_reports_omissions():
    state = {"status": "recorded", "state": {"pending": ["DNS verification"]}}
    result = {
        "continuation": state,
        "project_rules": [{"statement": "x" * 300}] * 20,
        "project_decisions": [{"answer": "y" * 300}] * 20,
        "definition_of_done": {"website": [{"item": "z" * 300}] * 20},
    }
    fit(result, 1000)
    assert result["continuation"] == state
    assert disclosed_size(result) <= 1000
    assert result["context_omitted"]["project_decisions"] > 0
    assert "Omitted rules still apply" in result["context_followup"]
    assert all(len(r["statement"]) == 300 for r in result["project_rules"])


def test_large_realistic_checkpoint_metadata_keeps_blocker(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        checkpoint = Checkpoint(
            project="acorn",
            conversation_id="old",
            checkpoint_id="last",
            task="Website",
            status="blocked",
            done=["d" * 140] * 5,
            checks=["c" * 140] * 5,
            pending=["Verify DNS"],
            artifacts=["a" * 140] * 5,
        )
        record(conn, checkpoint, agent_id="agent:codex:eval")
        result = brief(conn, "Continue Codex", "acorn", cfg=cfg, max_chars=1400)
        assert result["state"]["pending"] == ["Verify DNS"]
        assert len(json.dumps(result, ensure_ascii=False)) <= 1400
        assert result["omitted"] and result["next_tool"] == "session_history"


def test_budget_preserves_relevant_decision_and_standing_rule_alongside_trials():
    result = {
        "how_owner_works": [{"statement": "Ask before publishing", "trial": False}],
        "project_rules": [{"statement": "White stone images " + "x" * 150, "trial": True}] * 20,
        "project_decisions": [
            {"question": "Which image style?", "answer": "Stone"},
            {"question": "Which installer format?", "answer": "Native package"},
        ],
    }
    fit(result, 500, "Fix the native installer")
    assert result["how_owner_works"][0]["statement"] == "Ask before publishing"
    assert result["project_decisions"][0]["answer"] == "Native package"
    assert disclosed_size(result) <= 500


def test_os_name_does_not_match_a_terminal_window_subject():
    assert "window" not in _terms("Repair Windows directories")
    assert "window" in _terms("Repair terminal windows")
    assert decision_score(
        "What repair should the terminal window receive?",
        "Repair Windows directory publication?", "Fix everything", "acorn",
    ) is None


def test_generic_resume_prefers_its_chat_and_previous_session_respects_client(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        for conversation, agent, pending in (
            ("older", "agent:codex:local", "Previous Codex work"),
            ("current", "agent:codex:local", "Current task blocker"),
            ("other", "agent:claude-code:local", "Unrelated Claude work"),
        ):
            record(conn, Checkpoint(project="acorn", conversation_id=conversation,
                checkpoint_id="last", task="Build", status="working", pending=[pending]),
                agent_id=agent)
        args = dict(conversation_id="current", agent_id="agent:codex:local", cfg=cfg)
        assert brief(conn, "Resume", "acorn", **args)["state"]["conversation_id"] == "current"
        assert brief(conn, "Continue the previous session", "acorn", **args)[
            "state"]["conversation_id"] == "older"
        assert brief(conn, "Resume Claude Code", "acorn", **args)[
            "state"]["conversation_id"] == "other"
        record(conn, Checkpoint(project="acorn", conversation_id="current",
            checkpoint_id="private", task="Private work", status="working",
            sensitivity="sensitive"), agent_id="agent:codex:local")
        assert brief(conn, "Resume", "acorn", **args)["status"] == "withheld"
