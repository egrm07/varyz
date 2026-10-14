"""The owner's workflow: varyz briefs the agent first, answers it while it works,
asks the owner what it does not know, and checks the work before "done".

The guarantees under test:

* a general preference reaches every project; a one-off product choice never
  leaves the project it was made for;
* the definition of done for a kind of work reaches the agent whole, with no
  ranking dropping the favicon because the task never said "favicon";
* hooks brief the agent even when it forgets to call varyz, gate "done" once,
  and fail open — varyz never breaks a session.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from varyz import hooks, install
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.mcp_server import TOOLS, VaryzServer
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.advice import advise
from varyz.resolution.briefing import finish_check, render_markdown, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.domains import detect_domains
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.scope import parse_scope_choice, scope_options, suggest_scope
from varyz.resolution.workflows import SQLiteWorkflowStore, owner_kinds

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
        "[resolution]\nautostart = false\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return _config(tmp_path)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db",
        host="127.0.0.1",
        port=0,
        device_id=DEVICE,
        notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


@pytest.fixture
def server(config_path: Path, daemon_client: ResolutionClient) -> Iterator[VaryzServer]:
    server = VaryzServer(config_path, agent_token="", resolution_client=daemon_client)
    try:
        yield server
    finally:
        server.close()


def _rule(db: sqlite3.Connection, statement: str, **kwargs) -> str:
    return SQLitePrincipleStore(db).add(
        statement=statement, caller=OWNER, device_id=DEVICE, source="manual", **kwargs
    )


def _know(db: sqlite3.Connection, question: str, answer: str, scope: str = "*") -> str:
    return SQLiteKnowledgeStore(db).remember(
        question=question,
        answer=answer,
        caller=OWNER,
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id=DEVICE,
        scope=scope,
    )


# -- preference or one-off choice --------------------------------------------


@pytest.mark.parametrize(
    ("question", "answer", "general"),
    [
        ("Should websites get a favicon?", "Always, in all my projects.", True),
        ("Do you want a dry-run mode?", "I always want a dry-run first.", True),
        ("What accent colour for the landing page?", "#6d5efc", False),
        ("What should the app be called?", "Call it Lumen.", False),
        ("Which font here?", "Inter for this one.", False),
        ("Which analytics?", "Plausible.", False),  # unclear: fails narrow
    ],
)
def test_scope_default_separates_a_way_of_working_from_a_product_choice(
    question: str, answer: str, general: bool
) -> None:
    assert suggest_scope(question, answer, "lumen").general is general


@pytest.mark.parametrize(
    ("question", "ask"),
    [
        ("Which accent colour for the landing page?", False),
        ("Should lumen ship with a changelog?", False),
        ("Should I drop the jinja2 dependency as part of this final alpha pass?", False),
        ("Should the UI server be removed in this change?", False),
        ("Should every website ship with a favicon?", True),
        ("Which analytics?", True),
    ],
)
def test_the_reach_is_only_asked_when_the_question_could_be_a_habit(
    question: str, ask: bool
) -> None:
    options = scope_options(question, None, "lumen")
    assert options["ask"] is ask
    if not ask:
        assert options["default"] == "project"
        assert "Do not ask" in str(options["how"])


def test_the_owners_scope_pick_overrides_the_guess() -> None:
    assert parse_scope_choice("general", "lumen") == "*"
    assert parse_scope_choice("Always, in all my projects", "lumen") == "*"
    assert parse_scope_choice("project", "lumen") == "lumen"
    assert parse_scope_choice("Just for lumen: #6d5efc", "lumen") == "lumen"
    assert parse_scope_choice("san7", "lumen") == "san7"
    with pytest.raises(ValueError):
        parse_scope_choice("project", None)
    assert parse_scope_choice(None, "lumen") is None


# -- kinds of work and their definition of done --------------------------------


def test_the_task_names_the_kind_of_work_and_the_folder_only_fills_silence(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert "website" in detect_domains("Build a landing page for the waitlist", tmp_path)
    assert detect_domains("tidy up these things", tmp_path) == ("python-tool",)
    assert "python-tool" not in detect_domains("Write the README intro", tmp_path)


def test_a_product_named_as_the_subject_is_not_more_work() -> None:
    # The tallyho job got the Python-tool checklist for a static site.
    assert detect_domains(
        "Build a one-page landing site for tallyho, a small CLI that counts words."
    ) == ("website",)
    assert detect_domains("Design a website for our iOS app") == ("website",)
    assert detect_domains("Write the README for the wordcount CLI") == ("docs",)
    # Work named on both sides of "and" stays; a subject alone is still the work.
    assert set(detect_domains("Build a CLI that counts words and a landing page for it")) == {
        "python-tool", "website",
    }
    assert detect_domains("Write tests for the CLI") == ("python-tool",)


EVERYDAY = {
    "writing", "job-application", "travel", "shopping", "cooking", "social-post",
    "events", "study",
}


@pytest.mark.parametrize(
    "task",
    [
        "fix the failing pytest",
        "add a favicon to the landing page",
        "write the README",
        "schedule the nightly job",
        # Trade-offs chosen so developer wording never reads as everyday work:
        # "reply to" needs a determiner or a name ("reply to my landlord's email"),
        "the bot should reply to messages in telegram",
        "rewrite the error messages in the CLI",
        "send an email notification when the backup fails",
        # "resume" alone is a download or a run picking up again,
        "resume the download after a crash",
        "add a resume button to the player",
        # "trip" after "round" and "flight" after "in" are latency, not travel,
        "measure the round trip latency",
        "keep two requests in flight",
        # "buy" needs an object and never a subscription, "gift" a gift for someone,
        "let users buy a subscription in SAN7",
        "add gift subscriptions to SAN7",
        "restore purchases with RevenueCat",
        # "post" is HTTP or a blog unless a platform is named,
        "handle POST requests in FastAPI",
        "write a blog post about the release",
        "add an Instagram connector to memoir",
        # "third party" is a library, "event" and "calendar" are code nouns,
        "add a third party library for parsing",
        "store webhook events in the events table",
        "plan the events table migration",
        "render the calendar view",
        # and "learn", "course", "study" and "bake" are developer words too.
        "tune the learning rate",
        "the miner should learn from corrections",
        "of course, go ahead",
        "do an end-to-end study of the codebase",
        "bake the config into the docker image",
        "add a newsletter signup to the site",
        "publish a draft release",
        "add a trip planner feature for the travel app",
    ],
)
def test_developer_work_names_no_everyday_kind_of_work(task: str) -> None:
    assert not set(detect_domains(task)) & EVERYDAY


@pytest.mark.parametrize(
    ("task", "kind"),
    [
        ("write a cover letter for the Stripe job", "job-application"),
        ("update my CV for a product manager role", "job-application"),
        ("plan a 4-day trip to Lisbon", "travel"),
        ("book a flight and hotel for Berlin", "travel"),
        ("reply to my landlord's email", "writing"),
        ("email my boss that I'm sick tomorrow", "writing"),
        ("draft a LinkedIn post about the launch", "social-post"),
        ("write a tweet about our new feature", "social-post"),
        ("meal plan for the week, I'm vegetarian", "cooking"),
        ("what should I cook for dinner tonight", "cooking"),
        ("which laptop should I buy", "shopping"),
        ("help me pick a birthday gift for my mum", "shopping"),
        ("plan my sister's wedding", "events"),
        ("send out the birthday party invitations", "events"),
        ("help me study for my statistics exam", "study"),
        ("make flashcards for Spanish verbs", "study"),
    ],
)
def test_everyday_work_names_its_kind(task: str, kind: str) -> None:
    found = detect_domains(task)
    assert kind in found
    # "about the launch" is the post's subject, not a release.
    assert "release" not in found


def test_the_owners_own_kinds_of_work_count_when_the_task_names_them(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    own = ("gardening", "wedding-speech", "party", "website")
    assert detect_domains("Plan the gardening for spring", extra=own) == ("gardening",)
    assert "wedding-speech" in detect_domains("Polish my wedding speeches", extra=own)
    assert "party" in detect_domains("throw two parties", extra=own)
    assert detect_domains("tidy the garden", extra=own) == ()
    assert detect_domains("a gardener's tips", extra=own) == ()
    # A built-in name keeps its own triggers; a subject is still not the work.
    assert detect_domains("Build a landing page about gardening", extra=own) == ("website",)
    # Naming the owner's kind of work is wording, so the folder stays silent.
    assert detect_domains("gardening notes", tmp_path, extra=own) == ("gardening",)
    assert detect_domains("tidy up these things", tmp_path, extra=own) == ("python-tool",)


# -- start_task and finish_check ---------------------------------------------


def _website_owner(db: sqlite3.Connection) -> None:
    _rule(db, "Never create git commits unless I ask.")
    _rule(db, "Add a favicon in every size.", domain="website")
    _rule(db, "Write a meta description and Open Graph tags for every page.", domain="website")
    _rule(db, "Use the lowercase wordmark with the period.", scope="persona", domain="website")
    _know(db, "What accent colour for the landing page?", "#6d5efc", scope="persona")
    _know(db, "Where are websites hosted?", "Cloudflare Pages, every site.")


def test_start_task_hands_over_the_whole_definition_of_done(db: sqlite3.Connection) -> None:
    _website_owner(db)
    briefing = start_task(db, "Build the landing page for the waitlist", project="persona")

    assert [item["domain"] for item in briefing["kinds_of_work"]] == ["website"]
    items = [item["item"] for item in briefing["definition_of_done"]["website"]]
    assert items[0] == "Use the lowercase wordmark with the period."  # project's own first
    assert "Add a favicon in every size." in items
    assert any("meta description" in item for item in items)
    assert [rule["statement"] for rule in briefing["how_owner_works"]] == [
        "Never create git commits unless I ask."
    ]
    assert [item["answer"] for item in briefing["project_decisions"]] == ["#6d5efc"]
    assert [item["answer"] for item in briefing["general_preferences"]] == [
        "Cloudflare Pages, every site."
    ]
    hosted = [item for item in briefing["already_decided"] if "hosted" in item["question"]]
    assert hosted and hosted[0]["answer"] == "Cloudflare Pages, every site."
    assert briefing["ask_varyz_about"]
    text = render_markdown(briefing)
    assert "- [ ] Add a favicon in every size." in text
    assert "finish_check" in text


def test_a_one_off_choice_never_reaches_another_project(db: sqlite3.Connection) -> None:
    _website_owner(db)
    briefing = start_task(db, "Build the landing page for the waitlist", project="san7")
    items = [item["item"] for item in briefing["definition_of_done"]["website"]]
    assert "Use the lowercase wordmark with the period." not in items
    assert "Add a favicon in every size." in items
    assert briefing["project_decisions"] == []
    assert "#6d5efc" not in render_markdown(briefing)


def test_finish_check_lists_every_item_to_verify(db: sqlite3.Connection) -> None:
    _website_owner(db)
    check = finish_check(db, "Build the landing page", project="persona")
    assert {item["domain"] for item in check["definition_of_done"]} == {"website"}
    assert len(check["definition_of_done"]) == 3
    assert "done" in check["how"]
    empty = finish_check(db, "Reply to the email", project="persona")
    assert empty["definition_of_done"] == []


def test_a_rule_filed_under_the_owners_own_kind_of_work_reaches_that_work(
    db: sqlite3.Connection,
) -> None:
    _rule(db, "Only plants that are safe for cats.", domain="gardening")
    _rule(db, "Never create git commits unless I ask.")

    briefing = start_task(db, "Plan the gardening for the balcony", project="home")
    assert {"domain": "gardening", "title": "Gardening"} in briefing["kinds_of_work"]
    assert [item["item"] for item in briefing["definition_of_done"]["gardening"]] == [
        "Only plants that are safe for cats."
    ]
    assert "Only plants that are safe for cats." in render_markdown(briefing)
    check = finish_check(db, "Plan the gardening for the balcony", project="home")
    assert [item["item"] for item in check["definition_of_done"]] == [
        "Only plants that are safe for cats."
    ]
    advice = advise(db, "Pick seeds for my gardening", project="home")
    assert "Only plants that are safe for cats." in [r["statement"] for r in advice["owner_rules"]]

    other = start_task(db, "Plan a 4-day trip to Lisbon", project="home")
    assert "gardening" not in other["definition_of_done"]
    assert "gardening" not in [item["domain"] for item in other["kinds_of_work"]]
    assert finish_check(db, "Plan a 4-day trip to Lisbon", project="home")[
        "definition_of_done"
    ] == []
    assert "Only plants that are safe for cats." not in [
        r["statement"] for r in advise(db, "Plan a 4-day trip", project="home")["owner_rules"]
    ]


def test_a_workflow_filed_under_the_owners_own_kind_of_work_reaches_that_work(
    db: sqlite3.Connection,
) -> None:
    SQLiteWorkflowStore(db).record(
        name="Balcony routine", steps=["soil", "seedlings", "watering"], caller=OWNER,
        device_id=DEVICE, source="manual", domain="gardening",
    )
    _rule(db, "Book an aisle seat.", domain="travel")
    # No rule is filed under gardening; the workflow alone makes it a kind of work.
    assert owner_kinds(db) == ("gardening", "travel")

    briefing = start_task(db, "Plan the gardening for spring", project="home")
    assert "gardening" in [item["domain"] for item in briefing["kinds_of_work"]]
    assert [item["name"] for item in briefing["owner_workflows"]] == ["Balcony routine"]
    other = start_task(db, "Plan a 4-day trip to Lisbon", project="home")
    assert other["owner_workflows"] == []
    assert [item["item"] for item in other["definition_of_done"]["travel"]] == [
        "Book an aisle seat."
    ]


@pytest.mark.parametrize(
    ("task", "not_kind", "kind"),
    [
        ("schedule a dinner with Sam", "automation", "events"),
        ("schedule a dinner with Sam", "cooking", "events"),
        ("plan a dinner party for eight", "cooking", "events"),
        ("schedule a team meeting next week", "automation", "events"),
        ("visit historic sites in Rome on my trip", "website", "travel"),
        ("help me learn Python", "python-tool", "study"),
    ],
)
def test_everyday_wording_is_not_developer_work(task: str, not_kind: str, kind: str) -> None:
    found = detect_domains(task)
    assert not_kind not in found and kind in found


def test_developer_wording_keeps_its_kind() -> None:
    assert "automation" in detect_domains("schedule the nightly backup job")
    assert "website" in detect_domains("deploy the site")
    assert "python-tool" in detect_domains("write a Python script to rename files")


def test_cli_checklist_lists_the_owners_own_kinds_of_work(
    config_path: Path, db: sqlite3.Connection
) -> None:
    from varyz.cli import app

    _rule(db, "Only plants that are safe for cats.", domain="gardening")
    _rule(db, "Book an aisle seat.", domain="travel")
    runner = CliRunner()
    everything = runner.invoke(app, ["rules", "checklist", "--config", str(config_path)])
    assert everything.exit_code == 0, everything.output
    assert "Travel" in everything.output and "Book an aisle seat." in everything.output
    assert "Gardening" in everything.output and "gardening, yours, 1" in everything.output
    assert "Only plants that are safe for cats." in everything.output

    one = runner.invoke(app, ["rules", "checklist", "Gardening", "--config", str(config_path)])
    assert one.exit_code == 0, one.output
    assert "Only plants that are safe for cats." in one.output
    assert "Book an aisle seat." not in one.output

    fresh = runner.invoke(app, ["rules", "checklist", "knitting", "--config", str(config_path)])
    assert fresh.exit_code == 0, fresh.output
    assert "--domain knitting" in " ".join(fresh.output.split())

    wrong = runner.invoke(app, ["rules", "checklist", "a/b!", "--config", str(config_path)])
    assert wrong.exit_code == 2
    assert "Not a kind of work" in wrong.output


# -- the MCP surface ---------------------------------------------------------


def test_the_agent_asks_the_owner_answers_once_and_the_scope_holds(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    question = "Which accent colour should the landing page use?"
    first = server.call("ask", {"question": question, "project": "lumen"})
    ask = first["ask_owner"]
    assert first["basis"] == "escalated"
    assert ask["scope"]["project"] == "lumen"
    assert ask["scope"]["ask"] is False  # plainly this product's: its reach is not asked

    recorded = server.call("answer_owner_question", {
        "escalation_id": ask["escalation_id"], "answer": "#6d5efc", "scope": "project",
    })
    assert recorded["recorded"] is True
    assert recorded["stored_as"] == "choice for lumen only"
    assert recorded["scope_chosen_by"] == "owner"

    again = server.call("ask", {"question": question, "project": "lumen"})
    assert again["disposition"] == "answered"
    assert again["answer"] == "#6d5efc"
    elsewhere = server.call("ask", {"question": question, "project": "san7"})
    assert elsewhere.get("answer") != "#6d5efc"
    assert elsewhere["basis"] == "escalated"  # asked afresh for the other product


def test_a_general_answer_serves_every_project(server: VaryzServer) -> None:
    question = "Should every website ship with a favicon?"
    ask = server.call("ask", {"question": question, "project": "lumen"})["ask_owner"]
    server.call("answer_owner_question", {
        "escalation_id": ask["escalation_id"], "answer": "Yes, always.", "scope": "general",
    })
    other = server.call("ask", {"question": question, "project": "san7"})
    assert other["answer"] == "Yes, always."


def test_the_owner_can_dismiss_questions_without_teaching_anything(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    question = "Which headline should the hero use?"
    ask = server.call("ask", {"question": question, "project": "scratch"})["ask_owner"]
    server.call("ask", {"question": "Which font for the logo?", "project": "lumen"})
    assert len(daemon_client.pending()) == 2

    assert daemon_client.dismiss(ask["escalation_id"]) is True
    assert daemon_client.dismiss(ask["escalation_id"]) is False  # no longer pending
    assert [item["project"] for item in daemon_client.pending()] == ["lumen"]
    assert daemon_client.knowledge() == ()  # nothing learned from a dismissal
    assert server.call("ask", {"question": question, "project": "scratch"})["basis"] == "escalated"


def test_start_task_and_finish_check_through_the_server(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    daemon_client.add_principle(
        statement="Add a favicon in every size.", caller=OWNER, device_id=DEVICE,
        domain="website",
    )
    briefing = server.call("start_task", {"task": "Build a landing page", "project": "lumen"})
    assert briefing["definition_of_done"]["website"][0]["item"] == "Add a favicon in every size."
    check = server.call("finish_check", {"task": "Build a landing page", "project": "lumen"})
    assert [item["item"] for item in check["definition_of_done"]] == [
        "Add a favicon in every size."
    ]
    with pytest.raises(ValueError):
        server.call("start_task", {"task": "  "})
    assert [tool["name"] for tool in TOOLS[:3]] == ["start_task", "ask", "finish_check"]


# -- hooks ---------------------------------------------------------------------


def _cfg(config_path: Path, **agent: bool):
    cfg = load_config(config_path)
    for key, value in agent.items():
        setattr(cfg.agent, key, value)
    return cfg


def _transcript(path: Path, *entries: dict) -> str:
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    return str(path)


PROMPT = {"type": "user", "message": {"role": "user", "content": "Build the landing page"}}
EDIT = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


CHECK = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "finish", "name": "mcp__varyz__finish_check",
                             "input": {}}]},
}
CHECK_RESULT = {"type": "user", "message": {"content": [{
    "type": "tool_result", "tool_use_id": "finish",
    "content": json.dumps({"verification": {"ready_to_report": True}}),
}]}}


def test_edits_are_counted_only_since_the_last_real_prompt(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    assert hooks.edited_since_last_prompt(_transcript(path, PROMPT, EDIT, RESULT)) is True
    assert hooks.edited_since_last_prompt(_transcript(path, PROMPT, EDIT, PROMPT)) is False
    assert hooks.edited_since_last_prompt(str(tmp_path / "missing.jsonl")) is None


def test_a_finish_check_counts_only_after_the_last_edit(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    assert hooks.turn_activity(_transcript(path, PROMPT, EDIT, RESULT, CHECK)) == (True, False)
    assert hooks.turn_activity(
        _transcript(path, PROMPT, EDIT, RESULT, CHECK, CHECK_RESULT)
    ) == (True, True)
    assert hooks.turn_activity(_transcript(path, PROMPT, CHECK, EDIT, RESULT)) == (True, False)
    assert hooks.turn_activity(_transcript(path, CHECK, PROMPT, EDIT, RESULT)) == (True, False)
    assert hooks.turn_activity(str(tmp_path / "missing.jsonl")) is None


@pytest.mark.parametrize(
    ("prompt", "task"),
    [
        ("do i need to specify in the prompt to use varyz or will it be auto used?", False),
        ("should we make varyz intelligent with an llm that decides?", False),
        ("why is the website build so slow on windows", False),
        ("what does https://x.dev/a?b=c return for the pricing page?", False),
        ("yea, skip plain questions. also should we make it smarter with an llm?", True),
        ("Can you build a landing page for lumen?", True),
        ("Could you add a favicon to the site?", True),
        ("Please make the hero bigger?", True),
        ("Build a landing page for the waitlist", True),
        ("Do the migration for the scope column now.", True),
        ("ok. Build the pricing page for san7.", True),
    ],
)
def test_a_plain_question_is_answered_not_briefed(prompt: str, task: bool) -> None:
    assert hooks.is_task(prompt) is task


def test_questions_beside_a_request_do_not_add_kinds_of_work() -> None:
    mixed = "yea, skip plain questions in the hook. also should we use an llm for it?"
    assert hooks.task_focus(mixed) == "yea, skip plain questions in the hook."
    assert "ai-integration" not in detect_domains(hooks.task_focus(mixed))
    assert hooks.task_focus("Can you add a pricing page?") == "Can you add a pricing page?"
    assert hooks.task_focus("Build the site. Which font?") == "Build the site."


def test_the_first_task_is_briefed_and_later_prompts_only_add_new_kinds_of_work(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _website_owner(db)
    _rule(db, "Every CLI gets a --dry-run flag.", domain="python-tool")
    cfg = _cfg(config_path)
    event = {"session_id": "s1", "cwd": str(tmp_path / "persona")}

    assert hooks.user_prompt(cfg, {**event, "prompt": "thanks"}) is None
    question = "do I need to mention varyz in the prompt for the website?"
    assert hooks.user_prompt(cfg, {**event, "prompt": question}) is None
    for notice in (
        "<task-notification>\n<task-id>b1</task-id> Background command finished with exit code 1",
        "[SYSTEM NOTIFICATION - NOT USER INPUT] This is an automated background-task event",
    ):  # written by the client, not the owner: never taken for the task
        assert hooks.user_prompt(cfg, {**event, "prompt": notice}) is None
    first =hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})
    context = first["hookSpecificOutput"]["additionalContext"]
    assert first["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Add a favicon in every size." in context
    assert "#6d5efc" in context
    assert "start_task was run for you" in context

    repeated = hooks.user_prompt(cfg, {**event, "prompt": "make the hero section bigger"})
    assert repeated is not None
    reminder = repeated["hookSpecificOutput"]["additionalContext"]
    assert "History before clarification" in reminder
    assert "Status: required" in reminder
    assert "Definition of done" not in reminder
    later = hooks.user_prompt(cfg, {**event, "prompt": "now add a python cli to deploy it"})
    added = later["hookSpecificOutput"]["additionalContext"]
    assert "Every CLI gets a --dry-run flag." in added
    assert "Add a favicon" not in added


def test_live_rules_reach_existing_sessions_on_short_non_task_prompts(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    cfg = _cfg(config_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    # Neither reading nor task classification is needed for a live rule update.
    monkeypatch.setattr(hooks, "_live_job_text", lambda cfg, prompt: "")
    events = [{"session_id": name, "cwd": str(tmp_path / "persona")}
              for name in ("live-a", "live-b")]
    for event in events:
        hooks.user_prompt(cfg, {**event, "prompt": "Build a landing page for persona"})
    rule_id = _rule(db, "dont use . in titles", scope="all")
    for event in events:
        result = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "Varyz owner rule updates" in context
        assert "dont use . in titles" in context
        assert rule_id in context
        assert "do not ask" in context
        # A receipt in one chat must not suppress delivery in another.
        again = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
        assert "Varyz owner rule updates" not in again["hookSpecificOutput"]["additionalContext"]


def test_existing_chat_gets_current_rules_when_upgrading_without_receipts(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    cfg = _cfg(config_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    monkeypatch.setattr(hooks, "_live_job_text", lambda cfg, prompt: "")
    event = {"session_id": "live-upgrade", "cwd": str(tmp_path / "persona")}
    _rule(db, "dont use . in titles", scope="all")
    state_file = hooks._state_path(cfg, event["session_id"])
    hooks._save_state(state_file, {"briefed": True, "domains": ["website"],
                                   "task": "Build a landing page", "project": "persona"})
    result = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
    assert "dont use . in titles" in result["hookSpecificOutput"]["additionalContext"]
    assert "preference_snapshot" in hooks._load_state(state_file)


def test_live_rule_replacements_and_withdrawals_clear_stale_versions(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    cfg = _cfg(config_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    monkeypatch.setattr(hooks, "_live_job_text", lambda cfg, prompt: "")
    event = {"session_id": "live-replace", "cwd": str(tmp_path / "persona")}
    old = _rule(db, "Use periods in titles", scope="all")
    hooks.user_prompt(cfg, {**event, "prompt": "Build a landing page for persona"})
    new = _rule(db, "dont use . in titles", scope="all", supersedes_id=old)
    result = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "dont use . in titles" in context
    assert old in context and "No longer active" in context
    SQLitePrincipleStore(db).retract(new, caller=OWNER, device_id=DEVICE)
    result = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert new in context and "No longer active" in context
    assert "dont use . in titles" not in context


def test_live_rule_delivery_preserves_project_and_domain_scope(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    cfg = _cfg(config_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    monkeypatch.setattr(hooks, "_live_job_text", lambda cfg, prompt: "")
    event = {"session_id": "live-scope", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build a landing page for persona"})
    _rule(db, "Only SAN7 titles use blue text", scope="san7")
    _rule(db, "CLI output uses sentence titles", domain="python-tool")
    own = _rule(db, "Persona titles use cream text", scope="persona")
    context = hooks.user_prompt(cfg, {**event, "prompt": "continue"})
    context = context["hookSpecificOutput"]["additionalContext"]
    assert own in context
    assert "Only SAN7 titles" not in context
    assert "CLI output uses" not in context


def test_live_new_rule_is_not_lost_to_relevance_ranking(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    cfg = _cfg(config_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    event = {"session_id": "live-ranking", "cwd": str(tmp_path / "persona")}
    for i in range(25):
        _rule(db, f"Landing page hero section requirement {i}", scope="all")
    hooks.user_prompt(cfg, {**event, "prompt": "Build a landing page for persona"})
    new = _rule(db, "dont use . in titles", scope="all")
    result = hooks.user_prompt(cfg, {**event, "prompt": "make the hero section bigger"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert new in context and "dont use . in titles" in context


def test_the_first_task_briefing_carries_the_readers_ask_and_survives_its_failure(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, monkeypatch
) -> None:
    from varyz.resolution import profile_reader

    _website_owner(db)
    _rule(db, "Every CLI gets a --dry-run flag.", domain="python-tool")
    cfg = _cfg(config_path)
    job = {"turns_waiting": 4, "do": "Start the varyz-rule-miner agent in the background.",
           "subagent_prompt": "Call profile_reading_task until it says done."}
    # The daily reading's ask; the live one is off here (`daily=False`).
    monkeypatch.setattr(profile_reader, "agent_reading_offer",
                        lambda cfg, conn, now, *, prompt=None, daily=True: job if daily else None)
    event = {"session_id": "s-reader", "cwd": str(tmp_path / "persona")}

    first = hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "## Background job" in context
    assert job["do"] in context and job["subagent_prompt"] in context
    # Only the first task of a session is asked.
    later = hooks.user_prompt(cfg, {**event, "prompt": "now add a python cli to deploy it"})
    assert "Background job" not in later["hookSpecificOutput"]["additionalContext"]

    def broken(cfg, conn, now, **_):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(profile_reader, "agent_reading_offer", broken)
    other = {**event, "session_id": "s-broken", "prompt": "Build the landing page for persona"}
    context = hooks.user_prompt(cfg, other)["hookSpecificOutput"]["additionalContext"]
    assert "Add a favicon in every size." in context and "Background job" not in context


def test_the_done_gate_fires_once_per_task_and_only_after_edits(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _website_owner(db)
    cfg = _cfg(config_path)
    event = {"session_id": "s2", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})

    talk = _transcript(tmp_path / "talk.jsonl", PROMPT)
    assert hooks.stop(cfg, {**event, "transcript_path": talk}) is None
    edited = _transcript(tmp_path / "edit.jsonl", PROMPT, EDIT, RESULT)
    assert hooks.stop(cfg, {**event, "transcript_path": edited, "stop_hook_active": True}) is None

    blocked = hooks.stop(cfg, {**event, "transcript_path": edited})
    assert blocked["decision"] == "block"
    assert "Add a favicon in every size." in blocked["reason"]
    # The report is already on screen: the agent must not write it a second time.
    assert "do not repeat" in blocked["reason"]
    assert "how you verified" not in blocked["reason"]
    assert hooks.stop(cfg, {**event, "transcript_path": edited}) is None

    off = _cfg(config_path, done_gate=False)
    hooks.user_prompt(off, {"session_id": "s3", "cwd": event["cwd"],
                            "prompt": "Build another landing page for persona"})
    assert hooks.stop(off, {"session_id": "s3", "transcript_path": edited}) is None


def test_the_done_gate_lets_an_agent_that_checked_itself_report_once(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _website_owner(db)
    cfg = _cfg(config_path)
    event = {"session_id": "s4", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})

    checked = _transcript(tmp_path / "checked.jsonl", PROMPT, EDIT, RESULT, CHECK, CHECK_RESULT)
    assert hooks.stop(cfg, {**event, "transcript_path": checked}) is None
    # A subsequent edit with no successful check requires standards again.
    edited = _transcript(tmp_path / "edit.jsonl", PROMPT, EDIT, RESULT)
    assert hooks.stop(cfg, {**event, "transcript_path": edited})["decision"] == "block"

    event = {"session_id": "s5", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})
    stale = _transcript(tmp_path / "stale.jsonl", PROMPT, CHECK, EDIT, RESULT)
    assert hooks.stop(cfg, {**event, "transcript_path": stale})["decision"] == "block"


def test_varyz_texts_ask_for_exceptions_not_a_walkthrough() -> None:
    from varyz.mcp_server import INSTRUCTIONS, TOOLS
    from varyz.resolution.briefing import PROTOCOL

    finish = next(tool for tool in TOOLS if tool["name"] == "finish_check")
    for text in (INSTRUCTIONS, finish["description"], " ".join(PROTOCOL),
                 hooks.SESSION_START_CONTEXT):
        assert "how you verified" not in text
    for text in (INSTRUCTIONS, " ".join(PROTOCOL), hooks.SESSION_START_CONTEXT):
        assert "not to the user" in text


def test_hooks_fail_open(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{not json")))
    assert hooks.run("user-prompt", tmp_path / "absent.toml") == 0
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"prompt": 1}')))
    assert hooks.run("stop", tmp_path / "absent.toml") == 0
    assert capsys.readouterr().out == ""


def test_pause_is_a_kill_switch_for_every_hook(
    monkeypatch, config_path: Path, db: sqlite3.Connection, capsys
) -> None:
    from varyz.cli import app

    _website_owner(db)
    event = json.dumps({"session_id": "p1", "prompt": "Build the landing page for lumen"})
    runner = CliRunner()

    def prompt() -> str:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(event.encode())))
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
        hooks.run("user-prompt", config_path)
        return sys.stdout.buffer.getvalue().decode("utf-8")

    assert runner.invoke(app, ["hooks", "pause", "--config", str(config_path)]).exit_code == 0
    assert prompt() == ""
    assert runner.invoke(app, ["hooks", "resume", "--config", str(config_path)]).exit_code == 0
    assert "favicon" in prompt()
    monkeypatch.undo()
    claude = config_path.parent / ".claude.json"  # Claude Code is installed ...
    claude.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(install, "client_paths", lambda: {"claude-code": claude})
    doctor = runner.invoke(app, ["hooks", "doctor", "--config", str(config_path)])
    assert doctor.exit_code == 1 and "NO-GO" in doctor.output  # ... and has no launchers yet


def test_the_hook_runs_as_a_process_and_speaks_the_clients_json(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _website_owner(db)
    payload = json.dumps({
        "session_id": "p1", "cwd": str(tmp_path / "persona"),
        "prompt": "Build the landing page for persona",
    })
    done = subprocess.run(
        [sys.executable, "-m", "varyz.hooks", "user-prompt", "--config", str(config_path)],
        input=payload.encode("utf-8"), capture_output=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout.decode("utf-8"))
    assert "favicon" in output["hookSpecificOutput"]["additionalContext"]

    start = subprocess.run(
        [sys.executable, "-m", "varyz.hooks", "session-start", "--config", str(config_path)],
        input=b"{}", capture_output=True, timeout=60, check=False,
    )
    assert json.loads(start.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# -- installing the hooks -------------------------------------------------------


ORCA = {"hooks": [{"type": "command", "command": "C:/orca/claude-hook.cmd", "timeout": 10}]}


def test_merging_hooks_keeps_the_owners_other_hooks_in_place() -> None:
    settings = {"hooks": {"Stop": [ORCA], "PreToolUse": [ORCA]}, "theme": "dark"}
    commands = {"SessionStart": "a/varyz-hook-session-start.cmd",
                "Stop": "a/varyz-hook-stop.cmd"}
    assert install.merge_hooks(settings, commands) is True
    assert settings["hooks"]["Stop"][0] == ORCA  # trust in Codex is positional
    assert settings["hooks"]["Stop"][1]["hooks"][0]["command"] == "a/varyz-hook-stop.cmd"
    assert install.merge_hooks(settings, commands) is False
    moved = {**commands, "Stop": "b/varyz-hook-stop.cmd"}
    assert install.merge_hooks(settings, moved) is True
    assert len(settings["hooks"]["Stop"]) == 2
    assert install.remove_hooks(settings, "a/") is True  # only this archive's
    assert settings["hooks"]["Stop"][1]["hooks"][0]["command"] == "b/varyz-hook-stop.cmd"
    assert install.remove_hooks(settings) is True
    assert settings["hooks"]["Stop"] == [ORCA]
    assert settings["hooks"]["PreToolUse"] == [ORCA]
    assert "SessionStart" not in settings["hooks"]


def test_install_writes_launchers_and_both_clients_settings(
    tmp_path: Path, config_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.write_text(json.dumps({"hooks": {"Stop": [ORCA]}}), encoding="utf-8")
    codex_config = home / ".codex" / "config.toml"
    # Both clients are installed: the doctor checks only the ones that are.
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    codex_config.write_text("", encoding="utf-8")

    changed, detail = install.install_agent_hooks(
        "claude-code", home / ".claude.json", config_path=config_path, dry_run=False
    )
    assert changed and "hooks" in detail
    settings = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert settings["hooks"]["Stop"][0] == ORCA
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert command.startswith("if [ -f '") and "varyz-hook-user-prompt" in command
    assert claude_settings.with_suffix(".json.varyz-backup").exists()
    again = install.install_agent_hooks(
        "claude-code", home / ".claude.json", config_path=config_path, dry_run=False
    )
    assert again == (False, "briefing hooks already installed")

    install.install_agent_hooks("codex", codex_config, config_path=config_path, dry_run=False)
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    launcher = Path(codex["hooks"]["Stop"][0]["hooks"][0]["command"])
    assert launcher.exists()
    if launcher.suffix == ".exe":
        assert Path(str(launcher) + ".launch").read_text().splitlines()[1] == "stop"
    else:
        assert "varyz.hooks stop" in launcher.read_text(encoding="utf-8")

    report = dict(
        (name, ok) for name, ok, _ in hooks.doctor(
            load_config(config_path),
            client_configs={"claude-code": home / ".claude.json", "codex": codex_config},
        )
    )
    assert report["launchers"] and report["claude-code hooks"] and report["codex hooks"]

    removed = install.uninstall_agent_hooks(
        config_path, paths={"claude-code": home / ".claude.json", "codex": codex_config}
    )
    assert {item.client for item in removed} == {"claude-code", "codex"}
    settings = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert settings["hooks"] == {"Stop": [ORCA]}


def test_no_launchers_is_not_a_no_go_when_no_client_runs_hooks(
    tmp_path: Path, config_path: Path
) -> None:
    """Cursor only, or no agent yet: nothing would run a launcher."""
    cfg = load_config(config_path)
    absent = {"claude-code": tmp_path / "no.json", "codex": tmp_path / "no.toml"}
    report = {name: (ok, detail) for name, ok, detail in hooks.doctor(cfg, client_configs=absent)}
    assert report["launchers"] == (True, "not needed: neither Claude Code nor Codex is installed")
    present = tmp_path / ".claude.json"
    present.write_text("{}", encoding="utf-8")
    checks = hooks.doctor(cfg, client_configs={"claude-code": present})
    report = {name: ok for name, ok, _ in checks}
    assert report["launchers"] is False and report["claude-code hooks"] is False


def test_cli_previews_the_briefing_and_seeds_standards_once(
    config_path: Path, db: sqlite3.Connection
) -> None:
    from varyz.cli import app

    _rule(db, "Add a favicon in every size.", domain="website")
    runner = CliRunner()
    preview = runner.invoke(app, [
        "task", "Build the landing page", "--project", "persona", "--config", str(config_path),
    ])
    assert preview.exit_code == 0, preview.output
    assert "Add a favicon in every size." in preview.output
    seed = runner.invoke(app, ["rules", "seed", "--config", str(config_path)])
    assert seed.exit_code != 0  # varyz ships no rules of its own
    checklist = runner.invoke(app, [
        "rules", "checklist", "website", "--config", str(config_path),
    ])
    assert "Add a favicon in every size." in checklist.output


def test_a_hook_failure_from_replaced_code_does_not_hold_the_doctor(config_path, monkeypatch):
    """The error log is append-only; only failures newer than the code count."""
    cfg = load_config(config_path)
    log = hooks._errors_path(cfg)
    log.parent.mkdir(parents=True)
    # A line from before lines carried a time, from a build long since replaced.
    log.write_text(
        "user-prompt: NameError: name 'field' is not defined\n"
        "1970-01-02T00:00:00+00:00 stop: OSError: gone\n",
        encoding="utf-8",
    )
    ok, detail = hooks._error_status(cfg)
    assert ok and "2 older" in detail
    log.write_text("user-prompt: NameError: name 'field' is not defined\n", encoding="utf-8")
    ok, detail = hooks._error_status(cfg)
    assert ok and "1 older" in detail

    monkeypatch.setattr(hooks, "_load_cfg", lambda _config: cfg)
    hooks._log_failure(config_path, "stop", RuntimeError("database\nlocked"))
    ok, detail = hooks._error_status(cfg)
    assert not ok and detail.startswith("1 since") and "RuntimeError: database locked" in detail
    last = log.read_text(encoding="utf-8").splitlines()[-1]
    assert last.split(" ", 1)[1] == "stop: RuntimeError: database locked"
