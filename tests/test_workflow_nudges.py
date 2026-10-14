"""Workflow nudges: what the owner answers when an agent raises an earlier step.

The owner is right about the task in front of them. An agent raises an open
earlier step only when the task starts new work at a later one (a small fix is
just done), and records the answer. A skip that led to redoing work becomes the
reason the next nudge gives; a step skipped three times at no cost is put to the
owner once - keep it, make it optional, or drop it - and never changed silently.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from varyz.db.connection import open_db
from varyz.mcp_server import _UNTRUSTED_NOTE, INSTRUCTIONS, TOOLS, VaryzServer
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.briefing import finish_check, render_markdown, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.task_size import ChainSizer
from varyz.resolution.workflow_nudges import SQLiteNudgeStore
from varyz.resolution.workflows import (
    REVIEW_AFTER_SKIPS,
    REVIEW_WINDOW,
    SQLiteWorkflowStore,
    is_redo,
    parse_steps,
    task_size,
)
from varyz.trust import register_agent

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"
WEBSITE = ["colour", "logo", "page content"]
NEW_PAGE = "Build the hero section of the landing page"
REDO_PAGE = "Redo the hero section of the landing page"


class _Tick:
    """A clock that moves a second each time it is read.

    It starts a day ago by default: start_task reads the nudges on the real
    clock, and skips count toward a review only within the last week.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC) - timedelta(days=1)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def nudges(conn: sqlite3.Connection) -> SQLiteNudgeStore:
    return SQLiteNudgeStore(conn, clock=_Tick())


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


def _website(conn: sqlite3.Connection, steps=WEBSITE) -> str:
    return SQLiteWorkflowStore(conn).record(
        name="website", steps=steps, caller=OWNER, device_id=DEVICE, source="manual",
        domain="website",
    )


def _skip(nudges: SQLiteNudgeStore, workflow_id: str, step: str = "colour",
          project: str = "persona") -> str:
    [skip_id] = nudges.record_answer(
        workflow_id, steps=[step], answer="skipped", caller=OWNER, device_id=DEVICE,
        task_step="page content", project=project,
    )
    return skip_id


def _guide(conn: sqlite3.Connection, task: str = NEW_PAGE, project: str = "persona"):
    [guide] = start_task(conn, task, project=project)["owner_workflows"]
    return guide


def _settle_colour(conn: sqlite3.Connection) -> None:
    SQLiteKnowledgeStore(conn).remember(
        question="Which brand colours does persona use?", answer="Navy and white",
        caller=OWNER, basis=AnswerBasis.EXPLICIT, source="manual", device_id=DEVICE,
        scope="persona",
    )


# -- sizing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "size"),
    [
        ("Fix the build", "small"),
        ("Tweak the design of the header", "small"),
        ("Make a small tweak to the landing page", "small"),
        ("Fix a typo on the pricing page", "small"),
        ("Update the footer links", "small"),
        ("Build the landing page", "new"),
        ("Make the landing page", "new"),
        ("Redesign the logo", "new"),
        ("Add a pricing section", "new"),
        ("The landing page", "new"),  # nothing says small: treated as new work
    ],
)
def test_a_task_is_sized_by_its_verb(task: str, size: str) -> None:
    assert task_size(task, parse_steps(["colour", "logo", "design", "page content"])) == size


def test_a_step_name_does_not_size_the_task() -> None:
    steps = parse_steps(["vision", "design", "monetization"])
    # 'design' is the step here, not the verb: "tweak" sizes it.
    assert task_size("Tweak the design", steps) == "small"
    assert task_size("Design the onboarding screens", steps) == "new"


def test_a_redo_is_recognised() -> None:
    assert is_redo(REDO_PAGE)
    assert is_redo("Do the landing page again")
    assert is_redo("Rebuild the page from scratch")
    assert not is_redo(NEW_PAGE)


# -- when a step is raised ----------------------------------------------------


def test_new_work_is_nudged_and_a_small_change_is_just_done(
    conn: sqlite3.Connection,
) -> None:
    _website(conn)

    new = _guide(conn)
    assert new["work"] == "new" and new["nudge"] is True
    assert "starts new work on the 'page content' step" in new["guidance"]
    assert "record_workflow_nudge" in new["guidance"]

    small = _guide(conn, "Fix the hero section padding on the landing page")
    assert small["work"] == "small" and small["nudge"] is False
    assert small["open_before_task_step"] == ["colour", "logo"]  # still shown, not raised
    assert "just do it" in small["guidance"]
    assert "may be a slip" not in small["guidance"]


def test_an_optional_step_is_never_raised(conn: sqlite3.Connection) -> None:
    _website(conn, ["colour", {"step": "logo", "optional": True}, "page content"])

    guide = _guide(conn)
    assert guide["open_before_task_step"] == ["colour"]
    assert guide["steps"][1] == {"step": "logo", "optional": True}
    assert "2. logo (optional)" in render_markdown(start_task(conn, NEW_PAGE, project="persona"))


# -- skips, rework, review ----------------------------------------------------


def test_answers_are_recorded_against_the_workflows_own_step_names(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)

    ids = nudges.record_answer(
        workflow_id, steps=["Colour", "logo", "colour"], answer="took",
        caller=OWNER, device_id=DEVICE, task_step="page content", project="Persona",
    )

    assert len(ids) == 2  # the same step named twice is one answer
    rows = conn.execute(
        "SELECT step, answer, project FROM owner_workflow_nudges ORDER BY monotonic_seq"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("colour", "took", "persona"), ("logo", "took", "persona"),
    ]
    with pytest.raises(ValueError, match="not a step"):
        nudges.record_answer(workflow_id, steps=["fonts"], answer="took",
                             caller=OWNER, device_id=DEVICE)
    with pytest.raises(ValueError, match="'took' or 'skipped'"):
        nudges.record_answer(workflow_id, steps=["colour"], answer="rework",
                             caller=OWNER, device_id=DEVICE)
    with pytest.raises(KeyError):
        nudges.record_answer("missing", steps=["colour"], answer="took",
                             caller=OWNER, device_id=DEVICE)


def test_two_free_skips_in_a_week_ask_the_owner_once(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    for _ in range(REVIEW_AFTER_SKIPS - 1):
        _skip(nudges, workflow_id)
    assert "review" not in _guide(conn)

    _skip(nudges, workflow_id)
    guide = _guide(conn)
    assert guide["review"]["step"] == "colour"
    assert guide["review"]["skips"] == REVIEW_AFTER_SKIPS == 2
    assert "times this week" in guide["review"]["question"]
    assert "Keep it in the order, make it optional" in guide["review"]["question"]
    assert "answer_workflow_review" in guide["guidance"]
    assert "Never change the workflow without their answer" in guide["guidance"]
    [due] = finish_check(conn, NEW_PAGE, project="persona")["workflow_reviews_due"]
    assert due["workflow_id"] == workflow_id and due["step"] == "colour"

    # 'keep' changes nothing and is not asked again, however many skips follow.
    result = nudges.review(workflow_id, step="colour", decision="keep", caller=OWNER,
                           device_id=DEVICE, source="manual")
    assert result == {"decision": "keep", "workflow_id": workflow_id,
                      "already_answered": False}
    for _ in range(REVIEW_AFTER_SKIPS):
        _skip(nudges, workflow_id)
    assert "review" not in _guide(conn)
    again = nudges.review(workflow_id, step="colour", decision="drop", caller=OWNER,
                          device_id=DEVICE, source="manual")
    assert again["already_answered"] is True and again["decision"] == "keep"
    [current] = SQLiteWorkflowStore(conn).current()
    assert current.id == workflow_id


def test_a_skip_that_led_to_rework_is_the_reason_next_time(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    skip_id = _skip(nudges, workflow_id)

    # Colour is still open: redoing the page is not yet evidence of anything.
    assert "rework" not in _guide(conn, REDO_PAGE)

    _settle_colour(conn)
    guide = _guide(conn, REDO_PAGE)
    assert guide["rework"] == {"skip_id": skip_id, "step": "colour",
                               "task_step": "page content"}
    assert f"skip_id '{skip_id}'" in guide["guidance"]
    [rework] = finish_check(conn, REDO_PAGE, project="persona")["workflow_reworks"]
    assert rework["skip_id"] == skip_id
    # Another project's skip is not this project's rework.
    assert "rework" not in _guide(conn, REDO_PAGE, project="san7")

    rework_id = nudges.record_rework(skip_id, caller=OWNER, device_id=DEVICE)
    assert nudges.record_rework(skip_id, caller=OWNER, device_id=DEVICE) == rework_id
    assert "rework" not in _guide(conn, REDO_PAGE)  # recorded once

    # In a new project, the nudge now says why colour comes first...
    fresh = _guide(conn, project="san7")
    assert fresh["nudge"] is True
    assert "skipping 'colour' (1×) before led to redoing work" in fresh["guidance"]
    # ...and a step whose skip cost rework is never put up for dropping.
    for _ in range(REVIEW_AFTER_SKIPS):
        _skip(nudges, workflow_id, project="san7")
    assert "review" not in _guide(conn, project="san7")
    with pytest.raises(ValueError, match="not a skip"):
        nudges.record_rework(rework_id, caller=OWNER, device_id=DEVICE)


class _CountingRedo(ChainSizer):
    """Sizes by the verb list and counts every rework question it is asked."""

    def __init__(self) -> None:
        super().__init__([])
        self.asked: list[str] = []

    def is_redo(self, task, steps) -> bool:
        self.asked.append(task)
        return super().is_redo(task, steps)


def test_rework_is_asked_only_when_a_skip_could_count_it(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    # With the local model deciding rework, each question can cost a model load;
    # with no open skip whose step is settled, the answer could change nothing.
    workflow_id = _website(conn)
    sizer = _CountingRedo()
    start_task(conn, REDO_PAGE, project="persona", sizer=sizer)
    _skip(nudges, workflow_id)
    start_task(conn, REDO_PAGE, project="persona", sizer=sizer)  # colour still open
    assert sizer.asked == []
    _settle_colour(conn)
    [guide] = start_task(conn, REDO_PAGE, project="persona", sizer=sizer)["owner_workflows"]
    assert sizer.asked == [REDO_PAGE]
    assert guide["rework"]["step"] == "colour"


def test_optional_and_drop_write_a_new_version_of_the_owners_order(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    for _ in range(REVIEW_AFTER_SKIPS):
        _skip(nudges, workflow_id, step="logo")

    optional = nudges.review(workflow_id, step="logo", decision="optional", caller=OWNER,
                             device_id=DEVICE, source="manual")
    [current] = SQLiteWorkflowStore(conn).current()
    assert optional["workflow_id"] == current.id != workflow_id
    assert [(step.step, step.optional) for step in current.steps] == [
        ("colour", False), ("logo", True), ("page content", False),
    ]
    assert current.basis == "stated"
    assert current.provenance["reviewed"] == {"step": "logo", "decision": "optional",
                                              "from": workflow_id}
    guide = _guide(conn)
    assert guide["open_before_task_step"] == ["colour"] and "review" not in guide

    # The replaced version cannot be reviewed into overwriting its successor.
    with pytest.raises(ValueError, match="already replaced"):
        nudges.review(workflow_id, step="colour", decision="drop", caller=OWNER,
                      device_id=DEVICE, source="manual")

    dropped = nudges.review(current.id, step="colour", decision="drop", caller=OWNER,
                            device_id=DEVICE, source="manual")
    [latest] = SQLiteWorkflowStore(conn).current()
    assert dropped["workflow_id"] == latest.id
    assert [step.step for step in latest.steps] == ["logo", "page content"]
    with pytest.raises(ValueError, match="at least two steps"):
        nudges.review(latest.id, step="page content", decision="drop", caller=OWNER,
                      device_id=DEVICE, source="manual")


def test_counts_follow_the_workflow_across_versions(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    nudges.record_answer(workflow_id, steps=["colour"], answer="took", caller=OWNER,
                         device_id=DEVICE, project="persona")
    for _ in range(REVIEW_AFTER_SKIPS):
        _skip(nudges, workflow_id, step="logo")
    # A new version of the same workflow keeps the history.
    _website(conn, ["colour", "logo", "page content", "hosting"])

    rows = {row["step"]: row for row in nudges.counts()}
    assert set(rows) == {"colour", "logo"}
    assert (rows["colour"]["took"], rows["colour"]["skipped"]) == (1, 0)
    assert rows["logo"]["skipped"] == rows["logo"]["skips_this_week"] == REVIEW_AFTER_SKIPS
    assert rows["logo"]["review_due"] is True


def test_skips_older_than_a_week_expire_and_the_step_stays(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    long_ago = SQLiteNudgeStore(
        conn, clock=_Tick(datetime.now(UTC) - REVIEW_WINDOW - timedelta(days=3))
    )
    for _ in range(REVIEW_AFTER_SKIPS + 3):
        _skip(long_ago, workflow_id)
    assert "review" not in _guide(conn)  # many skips, but none this week

    _skip(nudges, workflow_id)
    assert "review" not in _guide(conn)  # one this week is not a pattern
    [row] = [row for row in nudges.counts() if row["step"] == "colour"]
    assert (row["skipped"], row["skips_this_week"], row["review_due"]) == (
        REVIEW_AFTER_SKIPS + 4, 1, False,
    )

    _skip(nudges, workflow_id)
    assert _guide(conn)["review"]["skips"] == 2


def test_the_ledgers_cannot_be_rewritten(
    conn: sqlite3.Connection, nudges: SQLiteNudgeStore
) -> None:
    workflow_id = _website(conn)
    _skip(nudges, workflow_id)
    nudges.review(workflow_id, step="colour", decision="keep", caller=OWNER,
                  device_id=DEVICE, source="manual")
    for table in ("owner_workflow_nudges", "owner_workflow_reviews"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET device_id = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO owner_workflow_nudges
               (id, workflow_id, step, normalized_step, answer, caller_agent_id,
                caller_display_name, caller_purpose, caller_device_id, caller_transport,
                device_id, monotonic_seq, recorded_at)
               VALUES ('bad', ?, 'colour', 'colour', 'rework', 'a', 'a', 'a', 'a', 'a',
                       'device:other', 1, '2026-09-01')""",
            (workflow_id,),
        )


def test_a_database_before_the_nudge_ledger_still_briefs(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "old.db")
    try:
        _website(conn)
        conn.execute("DROP TABLE owner_workflow_nudges")
        guide = _guide(conn)
        assert guide["nudge"] is True and "review" not in guide
    finally:
        conn.close()


# -- through the daemon, the MCP tools and the CLI ------------------------------


def test_the_tools_are_listed_and_the_instructions_name_them() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    for name in ("record_workflow_nudge", "answer_workflow_review"):
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert tools[name]["description"].endswith(_UNTRUSTED_NOTE)
        assert name in INSTRUCTIONS
    assert "A small change to work already there is not raised" in INSTRUCTIONS
    assert tools["answer_workflow_review"]["inputSchema"]["properties"]["decision"][
        "enum"
    ] == ["keep", "optional", "drop"]


def test_an_agent_records_answers_and_the_owners_review(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:claude-code:local", display_name="Claude Code", device_id="local"
    ).token
    conn.close()
    workflow_id = daemon_client.add_workflow(
        name="website", steps=WEBSITE, caller=OWNER, device_id=DEVICE, domain="website"
    )
    server = VaryzServer(config, agent_token=token, resolution_client=daemon_client)
    try:
        for _ in range(REVIEW_AFTER_SKIPS):
            [guide] = server.call("start_task", {"task": NEW_PAGE, "project": "persona"})[
                "owner_workflows"
            ]
            recorded = server.call("record_workflow_nudge", {
                "workflow_id": guide["id"], "answer": "skipped",
                "steps": guide["open_before_task_step"], "task_step": guide["task_step"],
                "project": "persona",
            })
            assert recorded["recorded"] is True and len(recorded["nudge_ids"]) == 2
        [guide] = server.call("start_task", {"task": NEW_PAGE, "project": "persona"})[
            "owner_workflows"
        ]
        assert guide["review"]["step"] == "colour"
        with pytest.raises(ValueError, match="skip_id"):
            server.call("record_workflow_nudge", {"workflow_id": workflow_id,
                                                  "answer": "rework"})
        reviewed = server.call("answer_workflow_review", {
            "workflow_id": guide["id"], "step": "colour", "decision": "optional",
        })
        [after] = server.call("start_task", {"task": NEW_PAGE, "project": "persona"})[
            "owner_workflows"
        ]
    finally:
        server.close()

    assert reviewed["recorded"] is True and reviewed["workflow_id"] != workflow_id
    assert after["id"] == reviewed["workflow_id"]
    assert after["open_before_task_step"] == ["logo"]  # colour is optional now
    [current] = daemon_client.workflows()
    assert current["source"] == "agent_relay:agent:claude-code:local"
    assert current["steps"][0] == {"step": "colour", "optional": True}


def test_cli_lists_answers_and_records_a_review(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli

    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    monkeypatch.setattr(cli.console, "width", 240)
    runner = CliRunner()

    empty = runner.invoke(cli.app, ["workflows", "review", "--config", str(config)])
    assert empty.exit_code == 0, empty.output
    assert "No step has been raised yet" in empty.output

    workflow_id = str(daemon_client.add_workflow(
        name="website", steps=WEBSITE, caller=OWNER, device_id=DEVICE, domain="website"
    ))
    for _ in range(REVIEW_AFTER_SKIPS):
        daemon_client.record_workflow_nudge(
            workflow_id, answer="skipped", caller=OWNER, device_id=DEVICE,
            steps=["logo"], task_step="page content", project="persona",
        )
    listed = runner.invoke(cli.app, ["workflows", "review", "--config", str(config)])
    assert listed.exit_code == 0, listed.output
    assert "logo" in listed.output and "due" in listed.output

    refused = runner.invoke(cli.app, [
        "workflows", "review", workflow_id, "logo", "maybe", "--config", str(config),
    ])
    assert refused.exit_code == 1
    decided = runner.invoke(cli.app, [
        "workflows", "review", workflow_id, "logo", "drop", "--config", str(config),
    ])
    assert decided.exit_code == 0, decided.output
    assert "Kept your answer" in decided.output
    shown = runner.invoke(cli.app, ["workflows", "list", "--config", str(config)])
    assert "colour → page content" in shown.output
