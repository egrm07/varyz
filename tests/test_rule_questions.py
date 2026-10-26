"""A rule the reading was unsure of is asked inside a task it bears on.

The owner does not vet waiting rules on a page. A task that a waiting rule bears
on asks about it once, in one plain line; the owner's answer acts at once. At
most one per conversation and ``DAILY_CAP`` a day, none in away mode, and a rule
the owner let pass rests for ``QUIET_DAYS``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from varyz.db.connection import open_db
from varyz.mcp_server import _UNTRUSTED_NOTE, TOOLS, VaryzServer
from varyz.resolution.away import set_mode
from varyz.resolution.briefing import finish_check, render_markdown, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.rule_questions import (
    DAILY_CAP,
    QUIET_DAYS,
    open_question,
    pick,
    record,
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
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TASK = "Add a schema migration for the export table and test it"
MIGRATIONS = "Run the migration tests before you report a schema change."  # 3 shared
NAMING = "Name each migration after the table it changes."  # 2 shared
EXPORT = "Keep the export table append-only."  # 2 shared
TABS = "Use tabs in the makefile."  # none


def _provenance(threads: int = 1) -> dict[str, Any]:
    quotes = [{"entry_id": f"e{n}", "thread": f"t{n}", "quote": f"said it {n}"}
              for n in range(threads)]
    return {"slot": "rule", "quotes": quotes, "messages": threads, "threads": threads}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SQLitePrincipleStore:
    return SQLitePrincipleStore(conn)


def _wait(store: SQLitePrincipleStore, statement: str, *, source: str = "model:profile",
          scope: str = "*", threads: int = 1, **provenance: Any) -> str:
    rule_id = store.propose(statement=statement, caller=OWNER, device_id=DEVICE,
                            source=source, scope=scope,
                            provenance={**_provenance(threads), **provenance})
    assert rule_id is not None
    return rule_id


def _note(conn: sqlite3.Connection, rule_id: str, conversation: str, event: str = "asked",
          *, at: datetime = NOW) -> None:
    assert record(conn, rule_id, conversation, event, project="varyz", stamp=at.isoformat())


def _pick(conn: sqlite3.Connection, conversation: str | None = "c1", *, task: str = TASK,
          project: str = "varyz", now: datetime = NOW) -> dict[str, Any] | None:
    return pick(conn, task, project=project, conversation_id=conversation, now=now)


# -- which rule, and when ------------------------------------------------------


def test_the_waiting_rule_that_bears_most_on_the_task_is_asked_in_one_line(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    _wait(store, NAMING)
    migrations = _wait(store, MIGRATIONS)
    _wait(store, TABS)

    asked = _pick(conn)

    assert asked is not None and asked["id"] == migrations
    assert asked["question"] == f'Keep this as a rule: "{MIGRATIONS}"?'
    assert asked["answers"] == ["Yes", "No", "In my words"]
    assert asked["scope"] == "all projects" and len(asked["bears_on"]) == 3
    assert "never answer it yourself" in asked["how"]


def test_between_rules_that_bear_equally_the_one_seen_most_often_is_asked(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    _wait(store, NAMING)
    export = _wait(store, EXPORT, threads=3)

    asked = _pick(conn)

    assert asked is not None and asked["id"] == export


def test_nothing_is_asked_without_a_conversation_or_a_rule_that_bears_on_the_task(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    _wait(store, MIGRATIONS)

    assert _pick(conn, None) is None
    assert _pick(conn, task="Rename the login button") is None
    assert _pick(conn, task="Write a schema doc") is None  # one shared word is not enough


def test_another_projects_rules_and_rules_not_from_a_reading_are_not_asked(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    _wait(store, MIGRATIONS, scope="persona")
    _wait(store, NAMING, source="mined:ai_conversations")
    _wait(store, EXPORT, workflow={"name": "migrations", "steps": ["write", "test"]})

    assert _pick(conn) is None
    assert _pick(conn, project="persona") is not None


def test_one_question_per_conversation_and_a_rule_let_pass_rests(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    migrations = _wait(store, MIGRATIONS)
    naming = _wait(store, NAMING)
    _note(conn, migrations, "c1")

    assert _pick(conn, "c1") is None
    second = _pick(conn, "c2")
    rested = _pick(conn, "c3", now=NOW + timedelta(days=QUIET_DAYS, hours=1))

    assert second is not None and second["id"] == naming
    assert rested is not None and rested["id"] == migrations


def test_an_answered_rule_is_never_asked_again(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    migrations = _wait(store, MIGRATIONS)
    _note(conn, migrations, "c1")
    _note(conn, migrations, "c1", "no")

    assert _pick(conn, "c2", now=NOW + timedelta(days=QUIET_DAYS * 3)) is None


def test_at_most_the_daily_cap_is_asked_in_a_day(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    for statement in (MIGRATIONS, NAMING, EXPORT):
        _wait(store, statement)
    for number in range(DAILY_CAP):
        asked = _pick(conn, f"c{number}")
        assert asked is not None
        _note(conn, asked["id"], f"c{number}", at=NOW + timedelta(minutes=number))

    assert _pick(conn, "c9", now=NOW + timedelta(hours=1)) is None
    assert _pick(conn, "c9", now=NOW + timedelta(days=1, hours=1)) is not None


def test_the_question_stays_open_in_its_conversation_until_answered(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    migrations = _wait(store, MIGRATIONS)
    _note(conn, migrations, "c1")

    still = open_question(conn, "c1")
    elsewhere = open_question(conn, "c2")
    _note(conn, migrations, "c1", "yes")

    assert still is not None and still["id"] == migrations
    assert elsewhere is None and open_question(conn, "c1") is None


def test_the_ledger_cannot_be_rewritten(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    migrations = _wait(store, MIGRATIONS)
    _note(conn, migrations, "c1")

    assert record(conn, migrations, "c1", "asked", project="varyz",
                  stamp=NOW.isoformat()) is False
    with pytest.raises(ValueError, match="event"):
        record(conn, migrations, "c1", "maybe", project="varyz", stamp=NOW.isoformat())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE rule_questions SET event='yes'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM rule_questions")


def test_the_briefing_carries_it_and_away_mode_holds_it_back(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    migrations = _wait(store, MIGRATIONS)

    brief = start_task(conn, TASK, project="varyz", conversation_id="c1")
    _note(conn, migrations, "c1")
    asked_at_finish = finish_check(conn, TASK, project="varyz", conversation_id="c1")
    set_mode(conn, active=True, scope="task", project="varyz", conversation_id="c1",
             instruction="keep working while I am away")
    away_start = start_task(conn, TASK, project="varyz", conversation_id="c1")
    away_finish = finish_check(conn, TASK, project="varyz", conversation_id="c1")

    assert brief["rule_question"]["id"] == migrations
    text = render_markdown(brief)
    assert "## One rule to check with the owner" in text and MIGRATIONS in text
    assert asked_at_finish["rule_question"]["id"] == migrations
    assert away_start["rule_question"] is None and away_finish["rule_question"] is None


# -- through the daemon, the MCP tool and the hook -------------------------------


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0, device_id=DEVICE,
        notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


def _queue(client: ResolutionClient, statement: str) -> str:
    rule_id = client.add_principle(
        statement=statement, caller=OWNER, device_id=DEVICE, source="model:profile",
        provenance=_provenance(1), status="candidate",
    )
    assert rule_id is not None and client.last_trial_id is None  # waiting, not on trial
    return rule_id


def _server(tmp_path: Path, client: ResolutionClient, *, registered: bool = True) -> VaryzServer:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    token = None
    if registered:
        conn = open_db(tmp_path / "data" / "varyz.db")
        token = register_agent(
            conn, agent_id="agent:claude-code:local", display_name="Claude Code",
            device_id="local",
        ).token
        conn.close()
    return VaryzServer(config, agent_token=token, resolution_client=client)


def _task(conversation: str) -> dict[str, Any]:
    return {"task": TASK, "project": "varyz", "conversation_id": conversation}


def test_the_tool_is_listed_for_agents() -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == "answer_rule_question")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["description"].endswith(_UNTRUSTED_NOTE)
    assert tool["inputSchema"]["properties"]["answer"]["enum"] == ["yes", "no", "reword"]


def test_an_agent_asks_once_and_the_owners_yes_keeps_the_rule(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    rule_id = _queue(daemon_client, MIGRATIONS)
    server = _server(tmp_path, daemon_client)
    try:
        first = server.call("start_task", _task("c1"))["rule_question"]
        again = server.call("start_task", _task("c1"))["rule_question"]
        before = server.call("finish_check", _task("c1"))["rule_question"]
        kept = server.call("answer_rule_question", {
            "id": rule_id, "answer": "yes", "project": "varyz", "conversation_id": "c1"})
        after = server.call("finish_check", _task("c1"))["rule_question"]
        twice = server.call("answer_rule_question", {
            "id": rule_id, "answer": "no", "project": "varyz", "conversation_id": "c1"})
    finally:
        server.close()

    assert first["id"] == rule_id and again is None and before["id"] == rule_id
    assert kept["recorded"] is True and kept["statement"] == MIGRATIONS
    assert after is None
    assert twice["recorded"] is False and "no longer waiting" in twice["error"]
    assert [rule["statement"] for rule in daemon_client.principles("confirmed")] == [MIGRATIONS]
    assert not daemon_client.principles("candidate")


def test_no_stops_the_rule_and_in_my_words_keeps_the_owners_wording(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    migrations = _queue(daemon_client, MIGRATIONS)
    naming = _queue(daemon_client, NAMING)
    server = _server(tmp_path, daemon_client)
    try:
        first = server.call("start_task", _task("c1"))["rule_question"]
        stopped = server.call("answer_rule_question", {
            "id": first["id"], "answer": "no", "conversation_id": "c1"})
        second = server.call("start_task", _task("c2"))["rule_question"]
        reworded = server.call("answer_rule_question", {
            "id": second["id"], "answer": "reword", "conversation_id": "c2",
            "statement": "Name a migration  after its table."})
    finally:
        server.close()

    assert (first["id"], second["id"]) == (migrations, naming)
    assert stopped["recorded"] is True and stopped["note"].startswith("Stopped")
    assert reworded["statement"] == "Name a migration after its table."
    assert [rule["statement"] for rule in daemon_client.principles("confirmed")] == [
        "Name a migration after its table."]
    assert not daemon_client.principles("candidate")


def test_answers_are_checked_and_an_unregistered_agent_is_not_asked(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    rule_id = _queue(daemon_client, MIGRATIONS)
    unregistered = _server(tmp_path, daemon_client, registered=False)
    try:
        unasked = unregistered.call("start_task", _task("c1"))["rule_question"]
    finally:
        unregistered.close()
    server = _server(tmp_path, daemon_client)
    try:
        with pytest.raises(ValueError, match="statement"):
            server.call("answer_rule_question", {
                "id": rule_id, "answer": "reword", "conversation_id": "c1"})
        with pytest.raises(ValueError, match="conversation_id"):
            server.call("answer_rule_question", {"id": rule_id, "answer": "yes"})
        asked = server.call("start_task", _task("c1"))["rule_question"]
    finally:
        server.close()

    assert unasked is None
    assert asked["id"] == rule_id  # the unregistered draw was not kept as asked


def test_the_hook_shows_the_question_only_once_it_is_kept_as_asked(
    daemon_client: ResolutionClient,
) -> None:
    from varyz import hooks

    rule_id = _queue(daemon_client, MIGRATIONS)
    cfg = SimpleNamespace(db_path=None, resolution=SimpleNamespace(
        bind_host=daemon_client.host, port=daemon_client.port, auth_mode="session",
        device_id=DEVICE))
    asked = {"id": rule_id, "question": "q", "scope": "all projects", "how": "h"}

    first: dict[str, Any] = {"rule_question": dict(asked)}
    hooks._ask_rule_question(cfg, first, "session-1", "varyz")
    again: dict[str, Any] = {"rule_question": dict(asked)}
    hooks._ask_rule_question(cfg, again, "session-1", "varyz")
    no_session: dict[str, Any] = {"rule_question": dict(asked)}
    hooks._ask_rule_question(cfg, no_session, None, "varyz")

    assert first["rule_question"] == asked
    assert again["rule_question"] is None and no_session["rule_question"] is None
