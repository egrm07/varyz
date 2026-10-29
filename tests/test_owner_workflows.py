"""Workflows: the order the owner guides an agent through a kind of work.

A website is colour, then logo, then the page; a product is vision, design,
monetization. Varyz hands that order to the agent with the task, marks the
steps this project already settled, and, when the task starts at a later step
while an earlier one is open, tells the agent to say so once without blocking:
the owner sometimes starts with the wrong thing, and learning must not copy it.
"""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from varyz.db.connection import open_db
from varyz.mcp_server import _UNTRUSTED_NOTE, INSTRUCTIONS, TOOLS, VaryzServer, _utf8
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.briefing import finish_check, render_markdown, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.workflow_miner import learn_workflows, project_of, prose, record_learned
from varyz.resolution.workflows import SQLiteWorkflowStore, Step, parse_steps
from varyz.trust import register_agent

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"
WEBSITE = ["colour", "logo: three options side by side, then I pick", "page content"]
START = datetime(2026, 9, 1, tzinfo=UTC)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SQLiteWorkflowStore:
    return SQLiteWorkflowStore(conn)


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


def _record(store: SQLiteWorkflowStore, name: str, steps, **kwargs) -> str:
    return store.record(
        name=name, steps=steps, caller=OWNER, device_id=DEVICE, source="manual", **kwargs
    )


def _learn(store: SQLiteWorkflowStore, name: str, steps, **kwargs) -> str | None:
    return store.learn(
        name=name, steps=steps, caller=OWNER, device_id=DEVICE,
        source="learned:ai_conversations", **kwargs,
    )


class _Turns:
    """The owner's turns to coding agents, as the ai_conversations connector stores them."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.count = 0

    def add(
        self,
        thread: str,
        text: str,
        *,
        project: str = "persona",
        authored: int = 1,
        sensitivity: str = "personal",
    ) -> None:
        self.count += 1
        when = (START + timedelta(minutes=self.count)).isoformat()
        speaker = "me" if authored else "Claude Code assistant"
        body = f"From: {speaker}\nConversation: Claude Code — {project}\n\n{text}"
        self.conn.execute(
            """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
                   occurred_start, ingested_at, thread_id, sensitivity, authored_by_user)
               VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"turn-{self.count}", f"turn-{self.count}", f"hash-{self.count}", body,
             when, when, thread, sensitivity, authored),
        )

    def session(self, thread: str, *texts: str, project: str = "persona") -> None:
        for text in texts:
            self.add(thread, text, project=project)
        self.conn.commit()


# -- the ledger ---------------------------------------------------------------


def test_steps_parse_from_words_and_how_splits_at_the_first_colon() -> None:
    assert parse_steps(WEBSITE) == (
        Step("colour"),
        Step("logo", "three options side by side, then I pick"),
        Step("page content"),
    )
    assert parse_steps([{"step": "vision", "how": "one paragraph"}, "design"])[0].how == (
        "one paragraph"
    )
    with pytest.raises(ValueError, match="at least two steps"):
        parse_steps(["colour"])
    with pytest.raises(ValueError, match="once"):
        parse_steps(["colour", "Colour"])


def test_recording_again_replaces_the_order_and_the_same_order_keeps_one(
    store: SQLiteWorkflowStore,
) -> None:
    first = _record(store, "website", WEBSITE, domain="website")
    assert _record(store, "Website", WEBSITE, domain="website") == first

    changed = _record(store, "website", ["logo", "colour", "page content"], domain="website")

    [current] = store.current()
    assert current.id == changed and current.supersedes_id == first
    assert [step.step for step in current.steps] == ["logo", "colour", "page content"]
    assert current.basis == "stated"


def test_the_ledger_cannot_be_rewritten(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    workflow_id = _record(store, "website", WEBSITE)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE owner_workflows SET name = 'x' WHERE id = ?", (workflow_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM owner_workflows WHERE id = ?", (workflow_id,))


def test_the_owners_words_beat_a_learned_order(store: SQLiteWorkflowStore) -> None:
    learned = _learn(store, "website", ["page content", "colour"])
    assert learned is not None
    assert _learn(store, "website", ["page content", "colour"]) is None  # unchanged

    stated = _record(store, "website", ["colour", "page content"])

    assert _learn(store, "website", ["logo", "page content"]) is None
    [current] = store.current()
    assert current.id == stated and current.supersedes_id == learned


def test_a_withdrawn_learned_order_is_never_learned_back(store: SQLiteWorkflowStore) -> None:
    learned = _learn(store, "website", ["page content", "colour"])
    assert learned is not None
    store.retract(learned, caller=OWNER, device_id=DEVICE, reason="that was a slip")

    assert store.current() == ()
    assert _learn(store, "website", ["page content", "colour"]) is None
    assert _learn(store, "website", ["colour", "page content"]) is not None
    with pytest.raises(ValueError, match="already replaced or withdrawn"):
        store.retract(learned, caller=OWNER, device_id=DEVICE)


def test_the_closest_scope_applies_and_other_projects_stay_out(
    store: SQLiteWorkflowStore,
) -> None:
    _record(store, "website", WEBSITE, domain="website")
    mine = _record(store, "website", ["logo", "colour"], domain="website", scope="persona")
    _record(store, "product", ["vision", "design", "monetization"])

    [chosen] = store.applicable("build the landing page", project="persona", domains=["website"])
    assert chosen.id == mine
    [general] = store.applicable("build the landing page", project="san7", domains=["website"])
    assert general.scope == "*"
    [product] = store.applicable("work out monetization", project="san7")
    assert product.name == "product"
    assert store.applicable("fix the flaky test", project="san7", domains=["python-tool"]) == ()


# -- the briefing -------------------------------------------------------------


def test_a_task_that_starts_at_a_later_step_is_flagged_not_blocked(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    _record(store, "website", WEBSITE, domain="website", rationale="The page follows the brand.")

    briefing = start_task(
        conn, "Build the hero section of the landing page", project="persona"
    )

    [guide] = briefing["owner_workflows"]
    assert guide["task_step"] == "page content"
    assert guide["open_before_task_step"] == ["colour", "logo"]
    assert guide["origin"] == "the owner's own order"
    assert "may be a slip" in guide["guidance"] and "Do not block" in guide["guidance"]
    assert guide["steps"][1]["how"] == "three options side by side, then I pick"
    text = render_markdown(briefing)
    assert "How the owner guides this work: website (their usual order)" in text
    assert "3. page content ← this task" in text
    assert "Why: The page follows the brand." in text


def test_steps_this_project_decided_are_settled(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    _record(store, "website", WEBSITE, domain="website")
    SQLiteKnowledgeStore(conn).remember(
        question="Which brand colours does persona use?", answer="Navy and white",
        caller=OWNER, basis=AnswerBasis.EXPLICIT, source="manual", device_id=DEVICE,
        scope="persona",
    )

    [guide] = start_task(
        conn, "Build the hero section of the landing page", project="persona"
    )["owner_workflows"]
    assert guide["steps"][0]["settled"].startswith("decided: Which brand colours")
    assert guide["open_before_task_step"] == ["logo"]

    [elsewhere] = start_task(
        conn, "Build the hero section of the landing page", project="san7"
    )["owner_workflows"]
    assert elsewhere["open_before_task_step"] == ["colour", "logo"]


def test_starting_at_the_first_step_names_the_next_one(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    _record(store, "website", WEBSITE, domain="website")

    [guide] = start_task(
        conn, "Pick the colours for the landing page", project="persona"
    )["owner_workflows"]

    assert guide["task_step"] == "colour"
    assert guide["open_before_task_step"] == []
    assert guide["guidance"].startswith("Next in the owner's order: 'colour'")


def test_finish_check_names_the_steps_still_open(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    _record(store, "website", WEBSITE, domain="website")

    check = finish_check(conn, "Build the hero section of the landing page", project="persona")

    [item] = check["workflow_steps_open"]
    assert item["workflow"] == "website" and item["task_step"] == "page content"
    assert item["open_before_it"] == ["colour", "logo"]
    assert "record_workflow_nudge" in item["record"]
    assert "workflow step" in check["how"]
    # A small change to the page is not new work on it: nothing to name.
    small = finish_check(conn, "Fix the hero section padding on the landing page",
                         project="persona")
    assert small["workflow_steps_open"] == []


def test_a_step_with_its_own_workflow_is_linked(
    conn: sqlite3.Connection, store: SQLiteWorkflowStore
) -> None:
    _record(store, "product", ["vision", "design", "monetization", "website"])
    _record(store, "website", WEBSITE, domain="website")

    guides = start_task(conn, "Work out monetization", project="persona")["owner_workflows"]

    [product] = guides
    assert product["steps"][3] == {"step": "website", "has_own_workflow": True}
    assert product["open_before_task_step"] == ["vision", "design"]


# -- learning -----------------------------------------------------------------


def test_redoing_the_page_after_picking_colours_teaches_colour_first(
    conn: sqlite3.Connection,
) -> None:
    """The owner built the site first, then picked colours, then had it redone."""
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}",
            "Build a landing page for persona with a hero section and a features list.",
            "Use a deep navy colour palette.",
            "Now redo the hero with the new colours.",
        )

    [website] = learn_workflows(conn)[0]

    assert [step.step for step in website.steps] == ["colour", "page content"]
    assert "redone in 3 of 3" in website.steps[0].how
    assert website.domain == "website"
    assert website.provenance["units"] == 3


def test_without_a_correction_the_order_the_owner_took_is_learned(
    conn: sqlite3.Connection,
) -> None:
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}",
            "Build a landing page with a hero section.",
            "Use a deep navy colour palette.",
        )

    [website], stats = learn_workflows(conn)

    assert [step.step for step in website.steps] == ["page content", "colour"]
    assert website.steps[0].how == ""
    assert stats.votes["first_mention"] == 3 and stats.votes["correction"] == 0


def test_regret_counts_for_the_step_that_should_have_come_first(
    conn: sqlite3.Connection,
) -> None:
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}",
            "Make the website for persona.",
            "Add a hero section.",
            "Hmm, we should have picked the colours first. Go with navy.",
        )

    [website] = learn_workflows(conn)[0]

    assert [step.step for step in website.steps] == ["colour", "page content"]


def test_an_explicit_order_beats_the_order_things_came_up(conn: sqlite3.Connection) -> None:
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}",
            "Design a logo for the website.",
            "Wait, pick the colours before the logo.",
        )

    [website], stats = learn_workflows(conn)

    assert [step.step for step in website.steps] == ["colour", "logo"]
    assert stats.votes["explicit"] == 3


def test_too_few_sessions_teach_nothing(conn: sqlite3.Connection) -> None:
    turns = _Turns(conn)
    for n in range(2):
        turns.session(f"site-{n}", "Build the landing page hero.", "Now the colour palette.")

    learned, stats = learn_workflows(conn)

    assert learned == []
    assert stats.units["website"] == 2


def test_the_product_order_is_learned_from_what_the_owner_said(
    conn: sqlite3.Connection,
) -> None:
    turns = _Turns(conn)
    for project in ("alpha", "beta", "gamma"):
        turns.session(
            f"{project}-1",
            f"Write the vision for {project} before any design work.",
            "Now design the onboarding screens.",
            project=project,
        )
        turns.session(
            f"{project}-2",
            "Pricing comes after the design is settled.",
            project=project,
        )

    [product] = learn_workflows(conn)[0]

    assert product.name == "product" and product.domain == ""
    assert [step.step for step in product.steps] == ["vision", "design", "monetization"]
    assert product.provenance["unit"] == "projects"


def test_what_a_project_discussed_first_is_not_its_order(conn: sqlite3.Connection) -> None:
    """A project runs for months: 'launch the daemon' in week one is not a launch step."""
    turns = _Turns(conn)
    for project in ("alpha", "beta", "gamma", "delta"):
        turns.session(
            f"{project}-1",
            "Launch the dev server and check the logs.",
            "Now design the database schema.",
            "Write the vision doc.",
            project=project,
        )

    learned, stats = learn_workflows(conn)

    assert learned == []
    assert stats.units["product"] == 4
    assert stats.votes["first_mention"] == 0


def test_learning_reads_only_the_owners_non_sensitive_prose(conn: sqlite3.Connection) -> None:
    turns = _Turns(conn)
    for n in range(3):
        turns.add(f"site-{n}", "Build the landing page hero.")
        turns.add(f"site-{n}", "Pick the colour palette.", sensitivity="sensitive")
        turns.add(f"site-{n}", "I chose a colour palette for the website.", authored=0)
    conn.commit()

    learned, stats = learn_workflows(conn)

    assert learned == []
    assert stats.turns_read == 3
    assert stats.sensitive_skipped == 3
    kept = prose("Keep it navy.\n```css\n.hero { color: #112233; }\n```\n  x = f()")
    assert kept.strip() == "Keep it navy."


def test_learned_orders_are_written_once(conn: sqlite3.Connection) -> None:
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}", "Build a landing page with a hero section.", "Use a navy palette."
        )
    store = SQLiteWorkflowStore(conn)
    record = partial(store.learn, caller=OWNER, device_id=DEVICE)

    assert record_learned(record, learn_workflows(conn)[0]) == (1, 0)
    assert record_learned(record, learn_workflows(conn)[0]) == (0, 1)
    [current] = store.current()
    assert current.basis == "learned" and current.origin() == "learned from 3 website sessions"


# -- surfaces -----------------------------------------------------------------


def test_workflows_travel_through_the_daemon(daemon_client: ResolutionClient) -> None:
    stated = daemon_client.add_workflow(
        name="website", steps=WEBSITE, caller=OWNER, device_id=DEVICE, domain="website",
        rationale="The page follows the brand.",
    )
    assert daemon_client.add_workflow(
        name="website", steps=["page content", "colour"], caller=OWNER, device_id=DEVICE,
        basis="learned",
    ) is None

    [item] = daemon_client.workflows()
    assert item["id"] == stated and item["steps"][1]["how"].startswith("three options")

    daemon_client.retract_workflow(str(stated), caller=OWNER, device_id=DEVICE)
    assert daemon_client.workflows() == ()


def test_the_tool_asks_first_and_the_instructions_name_it() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    tool = tools["remember_owner_workflow"]
    assert tool["inputSchema"]["required"] == ["name", "steps", "scope"]
    assert tool["annotations"]["readOnlyHint"] is False
    assert "AskUserQuestion" in tool["description"]
    assert tool["description"].endswith(_UNTRUSTED_NOTE)
    assert "remember_owner_workflow" in INSTRUCTIONS


def test_an_agent_relays_a_workflow_and_the_next_task_gets_it(
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
        recorded = server.call("remember_owner_workflow", {
            "name": "website",
            "steps": ["colour", {"step": "logo", "how": "three options"}, "page content"],
            "scope": "all",
            "domain": "website",
            "why": "The page follows the brand.",
        })
        briefing = server.call("start_task", {
            "task": "Build the hero section of the landing page", "project": "persona",
        })
    finally:
        server.close()

    assert recorded["recorded"] is True and recorded["scope"] == "all projects"
    assert recorded["steps"] == ["colour", "logo", "page content"]
    [item] = daemon_client.workflows()
    assert item["source"] == "agent_relay:agent:claude-code:local"
    [guide] = briefing["owner_workflows"]
    assert guide["open_before_task_step"] == ["colour", "logo"]


def test_cli_adds_lists_learns_and_retracts(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli

    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    monkeypatch.setattr(cli.console, "width", 240)
    runner = CliRunner()

    added = runner.invoke(cli.app, [
        "workflows", "add", "website", "colour", "logo: three options side by side",
        "page content", "--domain", "website", "--config", str(config),
    ])
    assert added.exit_code == 0, added.output
    listed = runner.invoke(cli.app, ["workflows", "list", "--steps", "--config", str(config)])
    assert listed.exit_code == 0, listed.output
    assert "colour → logo → page content" in listed.output
    assert "three options side by side" in listed.output

    conn = open_db(tmp_path / "data" / "varyz.db")
    turns = _Turns(conn)
    for n in range(3):
        turns.session(
            f"site-{n}", "The vision first, then design the screens.",
            project=f"p{n}",
        )
    conn.close()
    dry = runner.invoke(cli.app, ["workflows", "learn", "--config", str(config)])
    assert dry.exit_code == 0, dry.output
    assert "product: vision → design" in dry.output
    assert "screens" not in dry.output  # counts and step names, never the owner's words
    assert "Dry run" in dry.output
    applied = runner.invoke(cli.app, ["workflows", "learn", "--apply", "--config", str(config)])
    assert applied.exit_code == 0, applied.output
    assert {item["name"] for item in daemon_client.workflows()} == {"website", "product"}

    [website] = [item for item in daemon_client.workflows() if item["name"] == "website"]
    retracted = runner.invoke(
        cli.app, ["workflows", "retract", str(website["id"]), "--config", str(config)]
    )
    assert retracted.exit_code == 0, retracted.output
    assert [item["name"] for item in daemon_client.workflows()] == ["product"]


def test_autosync_learns_workflows_after_new_turns(tmp_path: Path, monkeypatch) -> None:
    from tests.conftest import FakeEmbedder
    from varyz import autosync
    from varyz.config import load_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n\n'
        "[connectors.ai_conversations]\napproved = true\n\n"
        "[autosync]\nlearn_workflows = true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    open_db(cfg.db_path).close()
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg, **_: FakeEmbedder())

    def fake_sync(conn, vault, connector, config):
        turns = _Turns(conn)
        for n in range(3):
            turns.session(
                f"site-{n}",
                "Build a landing page with a hero section.",
                "Use a navy colour palette.",
                "Now redo the hero with the new colours.",
            )
        return SimpleNamespace(new=9, updated=0, unchanged=0, excluded=0, deleted=0, errors=0)

    monkeypatch.setattr(autosync, "sync_connector", fake_sync)
    result = autosync.run_once(config_path, trigger="test")

    assert "Workflows learned or updated: 1" in result.detail
    conn = open_db(cfg.db_path)
    try:
        [learned] = SQLiteWorkflowStore(conn).current()
    finally:
        conn.close()
    assert [step.step for step in learned.steps] == ["colour", "page content"]
    assert learned.caller.agent_id == "varyz:autosync"


def test_mcp_reads_its_requests_as_utf8() -> None:
    """On Windows stdin defaulted to cp1252 and an em dash was stored as 'â€”'."""
    stream = io.TextIOWrapper(io.BytesIO("colour — then logo\n".encode()), encoding="cp1252")
    assert _utf8(stream).readline() == "colour — then logo\n"


def test_folder_copies_of_a_product_are_one_project() -> None:
    assert project_of("SAN7.14") == project_of("SAN7.13") == "SAN7"
    assert project_of("persona") == "persona"
