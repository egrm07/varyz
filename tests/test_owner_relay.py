"""The agent relay: varyz asks the owner through the agent they are talking to.

PIVOT_PLAN section 4.5, channel 1. An escalated resolve tells the agent what to
ask; the agent asks with its own question UI (AskUserQuestion in Claude Code, chat
elsewhere) and records the reply, which then answers the question at tier one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from varyz.db.connection import open_db
from varyz.mcp_server import INSTRUCTIONS, TOOLS, VaryzServer
from varyz.resolution.client import ResolutionClient, ResolutionClientError
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.trust import register_agent

QUESTION = "Should agents push commits to the remote without asking?"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db",
        host="127.0.0.1",
        port=0,
        device_id="device:test",
        notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


@pytest.fixture
def server(tmp_path: Path, daemon_client: ResolutionClient) -> Iterator[VaryzServer]:
    server = VaryzServer(_config(tmp_path), agent_token="", resolution_client=daemon_client)
    try:
        yield server
    finally:
        server.close()


def test_relay_tools_are_listed_and_the_agent_is_told_how_to_ask() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}

    assert tools["owner_questions"]["annotations"]["readOnlyHint"] is True
    answer = tools["answer_owner_question"]
    assert answer["annotations"]["readOnlyHint"] is False
    assert answer["inputSchema"]["required"] == ["escalation_id", "answer"]
    assert "never" in answer["description"].lower()
    assert "AskUserQuestion" in INSTRUCTIONS
    assert "answer_owner_question" in INSTRUCTIONS


def test_skill_teaches_the_relay_for_claude_and_codex() -> None:
    skill = (
        Path(__file__).parents[1] / "src" / "varyz" / "skills" / "varyz-context" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "AskUserQuestion" in skill
    assert "answer_owner_question" in skill
    assert "Codex" in skill
    assert "Skip for now" in skill


def test_escalation_is_relayed_recorded_and_then_answered_at_tier_one(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    first = server.call("resolve", {"question": QUESTION})
    assert first["disposition"] == "clarification"
    assert first["basis"] == "escalated"
    ask = first["ask_owner"]
    assert ask["escalation_id"] == first["escalation_id"]
    assert ask["question"] == QUESTION
    assert "AskUserQuestion" in ask["how"]

    waiting = server.call("owner_questions", {})
    assert waiting["pending"] == 1
    assert waiting["questions"][0]["escalation_id"] == ask["escalation_id"]
    assert waiting["questions"][0]["question"] == QUESTION

    recorded = server.call(
        "answer_owner_question",
        {"escalation_id": ask["escalation_id"], "answer": "  No, only push when I ask.  "},
    )
    assert recorded["recorded"] is True
    assert recorded["answer"] == "No, only push when I ask."
    assert recorded["basis"] == "user_confirmed"
    assert recorded["tier"] == 1

    again = server.call("resolve", {"question": QUESTION})
    assert again["disposition"] == "answered"
    assert again["answer"] == "No, only push when I ask."
    assert again["tier"] == 1
    assert "ask_owner" not in again
    assert server.call("owner_questions", {}) == {"pending": 0, "questions": [], "how": None}

    [entry] = daemon_client.knowledge()
    assert entry["basis"] == "user_confirmed"
    assert entry["source"] == "agent_relay:agent:unregistered"
    assert entry["escalation_id"] == ask["escalation_id"]

    # A second relay of the same question neither overwrites nor duplicates it.
    repeat = server.call(
        "answer_owner_question",
        {"escalation_id": ask["escalation_id"], "answer": "Yes, always push."},
    )
    assert repeat == {
        "recorded": False,
        "escalation_id": ask["escalation_id"],
        "already_answered": "No, only push when I ask.",
    }
    assert len(daemon_client.knowledge()) == 1


def test_the_open_choices_of_a_task_are_asked_together_and_each_answer_is_kept(
    server: VaryzServer,
) -> None:
    choices = [
        "Which accent colour should the shop use?",
        "Which font should the shop's headings use?",
        QUESTION,
    ]
    # One of them the owner answered before.
    earlier = server.call("ask", {"question": QUESTION, "project": "shop"})
    server.call("answer_owner_question", {
        "escalation_id": earlier["escalation_id"], "answer": "No, only push when I ask.",
        "scope": "general",
    })

    batch = server.call("ask", {
        "questions": [{"question": choice} for choice in choices], "project": "shop",
    })
    assert [result.get("answer") for result in batch["results"]] == [
        None, None, "No, only push when I ask."]
    asked = batch["ask_owner_batch"]
    assert [item["question"] for item in asked["questions"]] == choices[:2]
    # How to ask is said once, for all of them together.
    assert all("how" not in item for item in asked["questions"])
    assert "together" in asked["how"] and "answer_owner_question" in asked["how"]
    assert server.call("owner_questions", {"project": "shop"})["pending"] == 2

    for item, answer in zip(asked["questions"], ["Teal", "A thin serif"], strict=True):
        server.call("answer_owner_question", {
            "escalation_id": item["escalation_id"], "answer": answer, "scope": "project",
        })
    again = server.call("ask", {
        "questions": [{"question": choice} for choice in choices], "project": "shop",
    })
    assert "ask_owner_batch" not in again
    assert [result["answer"] for result in again["results"]] == [
        "Teal", "A thin serif", "No, only push when I ask."]


@pytest.mark.parametrize("arguments", [
    {},
    {"question": QUESTION, "questions": [{"question": QUESTION}]},
    {"questions": []},
    {"questions": [{"question": QUESTION, "scope": "general"}]},
    {"questions": [{"question": QUESTION}] * 21},
])
def test_ask_takes_one_question_or_a_list_of_them(
    server: VaryzServer, arguments: dict
) -> None:
    with pytest.raises(ValueError):
        server._call_ask(arguments)


def test_agents_batch_relevant_choices_before_building_not_one_at_a_time(
) -> None:
    from varyz.hooks import SESSION_START_CONTEXT
    from varyz.resolution.briefing import PROTOCOL

    skill = (
        Path(__file__).parents[1] / "src" / "varyz" / "skills" / "varyz-context" / "SKILL.md"
    ).read_text(encoding="utf-8")
    away = (
        Path(__file__).parents[1] / "src" / "varyz" / "skills" / "away" / "SKILL.md"
    ).read_text(encoding="utf-8")
    ask = next(tool for tool in TOOLS if tool["name"] == "ask")
    start = next(tool for tool in TOOLS if tool["name"] == "start_task")
    texts = {
        "hook": SESSION_START_CONTEXT, "instructions": INSTRUCTIONS,
        "protocol": " ".join(PROTOCOL), "ask": ask["description"],
        "start_task": start["description"], "skill": skill, "away": away,
    }
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert "more often than" not in flat, name
        assert "at every choice" not in flat, name
        assert "questions" in flat, name
    for name in ("hook", "instructions", "protocol", "ask", "skill"):
        assert "mid-work" in " ".join(texts[name].split()), name
    assert "questions" in ask["inputSchema"]["properties"]
    assert "required" not in ask["inputSchema"]


def test_question_guidance_preserves_meaning_and_keeps_internal_detail_private() -> None:
    from varyz.hooks import SESSION_START_CONTEXT
    from varyz.mcp_server import ASK_OWNER_BATCH_HOW, ASK_OWNER_HOW
    from varyz.resolution.briefing import PROTOCOL

    texts = (SESSION_START_CONTEXT, INSTRUCTIONS, " ".join(PROTOCOL),
             ASK_OWNER_HOW, ASK_OWNER_BATCH_HOW)
    for text in texts:
        assert "one short plain-language sentence" in text
        assert "15 words" in text
        assert "keep this rationale internal" in text
        assert "Unknown answer alone" in text or "unknown answer alone" in text
        assert "permission questions must still name" in text
        assert "escalation_id" in text
        assert "question verbatim" not in text


def test_agents_save_explicit_reusable_rules_without_confirmation() -> None:
    from varyz.hooks import SESSION_START_CONTEXT
    from varyz.resolution.advice import HOW_TO_USE
    from varyz.resolution.briefing import PROTOCOL
    from varyz.resolution.memory_policy import MEMORY_POLICY

    skill = (
        Path(__file__).parents[1] / "src" / "varyz" / "skills" / "varyz-context" / "SKILL.md"
    ).read_text(encoding="utf-8")
    plugins = Path(__file__).parents[1] / "plugins"
    copies = [
        (plugins / client / "skills" / "varyz-context" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for client in ("codex", "claude-code")
    ]
    for text in (SESSION_START_CONTEXT, INSTRUCTIONS, " ".join(PROTOCOL), HOW_TO_USE,
                 skill, *copies):
        flat = " ".join(text.split())
        assert MEMORY_POLICY in flat
        assert "Whenever the user corrects something you did" not in flat
        assert "General first, then groups with the most preferences" in flat
        assert "Project scope alone does not make a choice reusable" in flat
        assert "If future applicability is unclear, skip the memory offer" in flat
        assert "Honor explicit requests to remember a rule" in flat
        assert "replaces set to its id" in flat
        assert "call remember_owner_rule now, before continuing work" in flat
        assert "Use scope='all'" in flat
        assert "do not ask whether to make it a rule" in flat
        assert "remember_owner_rule" in flat


def test_answered_questions_are_not_asked_and_nothing_is_recorded_on_skip(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    server.call("resolve", {"question": QUESTION})
    # Skipping means the agent makes no call: the question simply stays pending.
    assert server.call("owner_questions", {})["pending"] == 1
    assert daemon_client.knowledge() == ()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"escalation_id": "../knowledge/x/retract", "answer": "yes"}, "escalation_id"),
        ({"escalation_id": "01NOSUCHQUESTION", "answer": "yes"}, "no question"),
        ({"escalation_id": "01NOSUCHQUESTION", "answer": "   "}, "answer is required"),
    ],
)
def test_relay_refuses_ids_varyz_did_not_issue_and_blank_answers(
    server: VaryzServer,
    daemon_client: ResolutionClient,
    arguments: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        server.call("answer_owner_question", arguments)
    assert daemon_client.knowledge() == ()


def test_registered_agent_may_relay_under_its_resolve_grant(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    config = _config(tmp_path)
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:claude-code:local", display_name="Claude Code", device_id="local"
    ).token
    conn.close()
    server = VaryzServer(config, agent_token=token, resolution_client=daemon_client)
    try:
        ask = server.call("resolve", {"question": QUESTION})["ask_owner"]
        recorded = server.call(
            "answer_owner_question",
            {"escalation_id": ask["escalation_id"], "answer": "Only when asked."},
        )
    finally:
        server.close()

    assert recorded["recorded"] is True
    [entry] = daemon_client.knowledge()
    assert entry["source"] == "agent_relay:agent:claude-code:local"


def test_relay_reports_a_daemon_outage_instead_of_an_empty_queue(tmp_path: Path) -> None:
    class OfflineClient:
        def pending(self):
            raise ResolutionClientError("resolution daemon unavailable: refused")

    server = VaryzServer(_config(tmp_path), agent_token="", resolution_client=OfflineClient())
    try:
        listed = server.call("owner_questions", {})
        answered = server.call(
            "answer_owner_question", {"escalation_id": "01ABC", "answer": "yes"}
        )
    finally:
        server.close()

    assert listed["error"] == answered["error"] == "resolution_daemon_unavailable"
    assert "questions" not in listed
