"""The portrait: a reading's rules as a few distinct traits and one habit.

A reading leaves many rules that make one point in different words. The portrait
groups them, quotes only what the owner typed, and finds the one thing the owner
keeps correcting agents about, from short pushback rather than task briefs.
Offline: rules group by shared words here, as they do without the embedding model.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from varyz import portrait
from varyz.db.connection import open_db
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.models import Caller
from varyz.resolution.principles import GLOBAL_SCOPE, Principle, SQLitePrincipleStore
from varyz.resolution.rule_miner import _turn_words
from varyz.resolution.rule_reader import Turn
from varyz.review_web import ReviewServer, ReviewSession
from varyz.scopes import session_project

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
YOURSELF = (
    "Do the setup steps yourself instead of listing them for the owner.",
    "Do setup steps yourself instead of listing the steps for the owner to run.",
    "Do the setup yourself instead of listing the steps for the owner.",
)


def _turn(entry_id: str, text: str, *, thread: str | None = None, scope: str = "shop",
          day: int = 1) -> Turn:
    return Turn(entry_id, entry_id, thread or f"t-{entry_id}",
                datetime(2026, 9, day, tzinfo=UTC).isoformat(), scope, text)


def _rule(rule_id: str, statement: str, quotes: tuple[tuple[str, str], ...], *,
          scope: str = GLOBAL_SCOPE, corrections: tuple[str, ...] = ()) -> Principle:
    provenance: dict[str, Any] = {
        "slot": "rule",
        "quotes": [{"entry_id": entry, "quote": quote} for entry, quote in quotes],
        "corrections": [{"entry_id": entry} for entry in corrections],
    }
    return Principle(
        id=rule_id, statement=statement, kind="rule", scope=scope, rationale="",
        status="candidate", source="agent:profile", provenance=provenance, caller=OWNER,
        device_id="device:test", monotonic_seq=1, supersedes_id=None, created_at=START,
    )


# -- traits ---------------------------------------------------------------------


def test_rules_that_make_one_point_are_one_trait_in_the_owners_words() -> None:
    turns = {turn.entry_id: turn for turn in (
        _turn("e1", "do it for me", scope="shop", day=1),
        _turn("e2", "why do i need to run this", scope="blog", day=9),
        _turn("e3", "just do the setup yourself please", scope="shop", day=5),
        _turn("e4", "use pathlib", scope="shop"),
        _turn("e5", "pathlib again", scope="blog"),
    )}
    rules = [
        _rule("r1", YOURSELF[0], (("e1", "do the setup for me, not a list of steps"),)),
        _rule("r2", YOURSELF[1], (("e2", "why do i need to run this myself"),),
              corrections=("e2",)),
        _rule("r3", YOURSELF[2], (("e3", "just do the setup yourself please"),)),
        _rule("r4", "Use pathlib for file paths.", (("e4", "use pathlib for every path"),
                                                    ("e5", "pathlib again for paths here"))),
    ]
    found = portrait.traits(rules, turns)

    assert [set(trait.rule_ids) for trait in found] == [{"r1", "r2", "r3"}, {"r4"}]
    first = found[0]
    assert first.conversations == 3 and first.corrections == 1
    assert set(first.projects) == {"shop", "blog"}
    # The newest line that shares the rule's words, not merely the newest line; it
    # is the owner's, with when and where.
    assert first.quote is not None
    assert first.quote.text == "just do the setup yourself please"
    assert (first.quote.when, first.quote.project) == ("2026-09-05", "shop")


def test_an_agents_words_and_a_single_conversation_are_not_a_trait() -> None:
    turns = {turn.entry_id: turn for turn in (
        _turn("e1", "use pathlib", thread="same"),
        _turn("e2", "pathlib again", thread="same"),
    )}
    rules = [
        # Quoted from a message the owner did not type (an agent's, or pasted).
        _rule("r1", "Write the report numbers first.", (("agent-1", "numbers first"),
                                                        ("agent-2", "numbers first again"))),
        # The owner's words, but from one conversation only.
        _rule("r2", "Use pathlib for file paths.", (("e1", "use pathlib for paths"),
                                                    ("e2", "pathlib again for paths"))),
    ]
    assert portrait.traits(rules, turns) == ()


def test_a_trait_seen_in_several_projects_outranks_one_projects_house_style() -> None:
    turns = {turn.entry_id: turn for turn in (
        _turn("a1", "x", scope="shop"), _turn("a2", "x", scope="shop"),
        _turn("a3", "x", scope="shop"),
        _turn("b1", "y", scope="shop"), _turn("b2", "y", scope="blog"),
        _turn("b3", "y", scope="blog"),
    )}
    rules = [
        _rule("house", "Use the teal palette for shop banners.",
              (("a1", "teal for shop banners"), ("a2", "teal banners again"),
               ("a3", "make the banners teal")), scope="shop"),
        _rule("wide", "Write the report numbers first.",
              (("b1", "numbers first in reports"), ("b2", "report the numbers first"),
               ("b3", "numbers first please"))),
    ]
    assert [trait.id for trait in portrait.traits(rules, turns)] == ["wide", "house"]


def test_a_family_is_named_only_when_the_rule_speaks_to_one() -> None:
    assert portrait._family("Measure against the baseline numbers.") == "proof"
    # Tests, "before" and shipping are three families: naming one would be a guess.
    assert portrait._family("Run the tests before you ship.") == ""
    assert portrait._family("Keep commit messages dense.") == ""


def test_headlines_are_brief_labels_and_leave_the_rule_intact() -> None:
    text = YOURSELF[0]
    assert portrait.headline(text) == "You put the AI in the driver's seat"
    assert portrait.headline("Keep explanations short and use plain everyday words.") == (
        "Fewer words. Human language.")
    assert portrait.headline("Report numbers first, failures included.") == (
        "The failures belong in the results")
    assert len(portrait.headline(text).split()) <= 12
    assert text == YOURSELF[0]


def test_headlines_never_drop_exceptions_or_reverse_a_negative_rule() -> None:
    assert portrait.headline("Always act. Except when the owner has paused the task.") == ""
    negative = "Do not automate payments."
    assert portrait.headline(negative) == "Do not automate payments"
    assert portrait.headline("Do not keep working after a stop request.") == (
        "Do not keep working after a stop request")
    assert portrait.headline("Retain the Varyz requirement recorded in note 2.") == ""
    assert portrait.headline("Never act on reversible changes without asking.") == (
        "Never act on reversible changes without asking")
    assert portrait.headline("When explicitly asked, run all setup steps yourself without "
                             "ever changing any settings outside the requested project.") == ""


def test_distinctive_repeated_findings_outrank_routine_tool_instructions() -> None:
    rare = portrait.interest("Keep explanations short.", conversations=3, projects=2)
    routine = portrait.interest("Use pathlib for file paths.", conversations=20)
    assert rare > routine
    assert portrait.interest("Report numbers first.", corrections=3) > (
        portrait.interest("Report numbers first."))


# -- the habit ------------------------------------------------------------------


def test_short_pushback_counts_and_a_task_brief_does_not() -> None:
    turns = [
        _turn("p1", "still not fixed.", day=3),
        _turn("p2", "didnt work, try again", day=4),
        _turn("p3", "Ok. it doesnt work on my phone", day=5),
        # Briefs that mention it: asking for work, not pushing back on it.
        _turn("b1", "fix anything that doesnt work, then report back", day=6),
        _turn("b2", "What did you fix in cloud today?", day=7),
        _turn("b3", "Here is the spec. " + "The app still does not sync when the network "
              "drops, and the users want a retry button with a clear status line under the "
              "header that says what is happening right now.", day=8),
    ]
    found, pushback = portrait.habits(turns, {})

    said = {item.turn.entry_id: item for item in found["unfinished"]}
    assert set(said) == {"p1", "p2", "p3"}
    assert all(item.own for item in said.values())
    assert said["p3"].line == "it doesnt work on my phone"
    assert pushback == {"p1", "p2", "p3"}


def test_the_owners_own_line_is_quoted_before_a_readings_description() -> None:
    turns = [
        _turn("p1", "still not fixed.", thread="t1", scope="shop", day=2),
        _turn("p2", "the page is still broken", thread="t2", scope="blog", day=3),
        _turn("c1", "check the live site please", thread="t3", scope="docs", day=9),
    ]
    corrections = {"c1": {"entry_id": "c1", "agent_did": "reported the deploy as done "
                          "without testing it", "quote": "check the live site please"}}
    found, pushback = portrait.habits(turns, corrections)
    habit = portrait._habit(portrait.BY_KIND["unfinished"], found["unfinished"])

    assert habit.times == 3 and habit.conversations == 3
    assert [item.own for item in found["unfinished"]] == [True, True, False]
    # The newest line is the reading's; the owner's own words come first anyway.
    assert [quote.text for quote in habit.quotes] == [
        "the page is still broken", "still not fixed.", "check the live site please"]
    assert (habit.first, habit.last) == ("2026-09-02", "2026-09-09")
    assert pushback == {"p1", "p2", "c1"}


# -- the archive ----------------------------------------------------------------

HEADER = "From: me\nConversation: Claude Code \u2014 {where}\n\n"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


def _entry(conn: sqlite3.Connection, entry_id: str, text: str, *, thread: str,
           where: str = "shop", folder: str = "", day: int = 1) -> None:
    when = datetime(2026, 9, day, tzinfo=UTC).isoformat()
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, thread_id, sensitivity, authored_by_user,
               conversation_metadata_json)
           VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, ?, 'personal', 1, ?)""",
        (entry_id, entry_id, entry_id, HEADER.format(where=where) + text, when, when, thread,
         json.dumps({"working_directory": folder} if folder else {})),
    )
    conn.commit()


def test_the_portrait_of_an_archive_names_the_habit_and_keeps_its_rules_out(
        conn: sqlite3.Connection) -> None:
    worktree = r"C:\Users\me\projects\shop\.claude\worktrees\nifty-tereshkova-96e180"
    lines = ("still not fixed.", "it still doesnt work", "didnt work, try again",
             "the same error again", "still broken on mobile")
    for number, line in enumerate(lines):
        _entry(conn, f"p{number}", line, thread=f"t{number}", day=number + 1,
               where="nifty-tereshkova-96e180" if number == 0 else "shop",
               folder=worktree if number == 0 else "")
    _entry(conn, "q1", "do the setup for me", thread="t1", day=10)
    _entry(conn, "q2", "why do i need to run it", thread="t2", where="projects", day=11)
    store = SQLitePrincipleStore(conn)
    store.propose(statement="Before saying a fix is done, test it end to end.", caller=OWNER,
                  device_id="device:test", source="agent:profile",
                  provenance={"slot": "done", "quotes": [{"entry_id": "p1", "quote": "x"}]})
    for number, statement in enumerate(YOURSELF[:2], start=1):
        store.propose(statement=statement, caller=OWNER, device_id="device:test",
                      source="agent:profile", provenance={"slot": "rule", "quotes": [
                          {"entry_id": f"q{number}", "quote": "do the setup steps for me"}]})

    found = portrait.build(conn)

    assert (found.messages, found.conversations) == (7, 5)
    assert found.since == "2026-09-01"
    # The worktree is the shop repository; a folder of projects names none.
    assert found.projects == (("shop", 5),)
    assert found.habit is not None and found.habit.kind == "unfinished"
    assert (found.habit.times, found.habit.conversations) == (5, 5)
    # The rule already written against the habit counts for it, not as a trait.
    assert len(found.habit.rule_ids) == 1
    assert [len(trait.rule_ids) for trait in found.traits] == [2]
    assert found.waiting == 3 and found.on_trial == 0
    assert found.to_dict()["projects"] == [{"name": "shop", "conversations": 5}]


# -- where a session ran --------------------------------------------------------


@pytest.mark.parametrize(("where", "folder", "project"), [
    ("shop", "", "shop"),
    ("nifty-tereshkova-96e180",
     r"C:\Users\me\projects\shop\.claude\worktrees\nifty-tereshkova-96e180", "shop"),
    ("x", "/home/me/code/Blog/.codex/worktrees/abc", "blog"),
    ("projects", r"C:\Users\me\projects", ""),
    ("Documents", "", ""),
    ("scratch-2026-10-01-ab12cd", "", ""),
    ("nifty-tereshkova-96e180", "", ""),
    ("", "", ""),
])
def test_a_session_belongs_to_its_repository(where: str, folder: str, project: str) -> None:
    assert session_project(where, folder) == project


def test_a_worktree_turn_is_scoped_to_its_repository() -> None:
    body = HEADER.format(where="nifty-tereshkova-96e180") + "keep it short"
    folder = r"C:\Users\me\projects\shop\.claude\worktrees\nifty-tereshkova-96e180"
    assert _turn_words(body, folder) == ("shop", "keep it short")
    assert _turn_words(HEADER.format(where="projects") + "hi") == (GLOBAL_SCOPE, "hi")


# -- the portrait page ----------------------------------------------------------

PUSHBACK = ("still not fixed.", "it still doesnt work", "didnt work, try again",
            "the same error again", "still broken on mobile")


@pytest.fixture
def review(tmp_path: Path, conn: sqlite3.Connection,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ReviewSession, ResolutionClient]]:
    monkeypatch.setattr(portrait, "embedder", lambda cfg: None)
    for number, line in enumerate(PUSHBACK):
        _entry(conn, f"p{number}", line, thread=f"t{number}", day=number + 1)
    _entry(conn, "q1", "do the setup for me", thread="t1", day=10)
    _entry(conn, "q2", "why do i need to run it", thread="t2", day=11)
    daemon = build_resolution_daemon(tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
                                     device_id="device:test", notifier=NullNotifier())
    try:
        with daemon:
            client = ResolutionClient(*daemon.address)
            for number, statement in enumerate(YOURSELF[:2], start=1):
                client.add_principle(
                    statement=statement, caller=OWNER, device_id="device:test",
                    source="agent:profile", status="candidate",
                    provenance={"slot": "rule", "quotes": [
                        {"entry_id": f"q{number}", "quote": "do the setup steps for me"}]})
            cfg = SimpleNamespace(db_path=tmp_path / "data" / "varyz.db")
            yield ReviewSession(client, OWNER, "device:test", cfg), client
    finally:
        daemon.stop()


def _live(client: ResolutionClient) -> list[dict[str, Any]]:
    return [rule for status in ("candidate", "confirmed") for rule in client.principles(status)]


def test_there_is_no_portrait_without_an_archive(tmp_path: Path) -> None:
    assert ReviewSession(None, OWNER, "device:test").portrait() is None
    missing = SimpleNamespace(db_path=tmp_path / "none.db")
    assert ReviewSession(None, OWNER, "device:test", missing).portrait() is None


def test_a_reworded_trait_is_kept_in_the_owners_words(
        review: tuple[ReviewSession, ResolutionClient]) -> None:
    session, client = review
    found = session.portrait()
    assert found is not None and len(found["traits"]) == 1
    trait = found["traits"][0]

    fixed = session.correct_trait(trait["id"], "reword", "  Do the setup   yourself. ")

    assert fixed["trait"]["corrected"] == "reword"
    assert fixed["trait"]["statement"] == "Do the setup yourself."
    statements = {rule["statement"]: rule["status"] for rule in _live(client)}
    # The other rule that makes the same point waits as it was.
    assert statements == {"Do the setup yourself.": "confirmed", YOURSELF[1]: "candidate"}


def test_not_me_stops_every_rule_behind_the_trait(
        review: tuple[ReviewSession, ResolutionClient]) -> None:
    session, client = review
    found = session.portrait()
    assert found is not None
    trait = found["traits"][0]

    assert session.correct_trait(trait["id"], "not_me")["trait"]["corrected"] == "not_me"
    assert _live(client) == []


def test_a_correction_the_page_cannot_make_is_refused(
        review: tuple[ReviewSession, ResolutionClient]) -> None:
    session, client = review
    found = session.portrait()
    assert found is not None
    trait_id = found["traits"][0]["id"]
    for args in (("gone", "not_me"), (trait_id, "reword", "   "), (trait_id, "maybe")):
        with pytest.raises(ValueError):
            session.correct_trait(*args)
    with pytest.raises(ValueError):
        session.keep_habit("nothing")
    assert len(client.principles("candidate")) == 2


def test_the_habit_becomes_one_rule_once(
        review: tuple[ReviewSession, ResolutionClient]) -> None:
    session, client = review
    found = session.portrait()
    assert found is not None and found["habit"]["kind"] == "unfinished"
    assert found["habit"]["kept"] is False

    kept = session.keep_habit("unfinished")

    assert session.keep_habit("unfinished") == kept
    rules = client.principles("confirmed")
    assert [(rule["id"], rule["statement"], rule["scope"], rule["provenance"]["portrait"])
            for rule in rules] == [
        (kept, portrait.BY_KIND["unfinished"].rule, GLOBAL_SCOPE, "unfinished")]
    again = session.portrait()
    assert again is not None and again["habit"]["kept"] is True


def test_the_page_serves_the_portrait_and_refuses_a_bad_correction(
        review: tuple[ReviewSession, ResolutionClient]) -> None:
    session, client = review
    server = ReviewServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.url.split("/?")[0].rstrip("/"),
                          headers={"X-Varyz-Review": session.token}, trust_env=False) as http:
            shown = http.get("/api/portrait").json()["portrait"]
            assert shown["habit"]["kind"] == "unfinished"
            refused = http.post("/api/portrait/trait", json={"id": "gone", "action": "not_me"})
            assert refused.status_code == 400 and "Reload the page" in refused.json()["error"]
            assert http.post("/api/portrait/habit", json=["x"]).status_code == 400
            assert http.post("/api/portrait/habit", json={"kind": "unfinished"}).json()["kept"]
    finally:
        server.shutdown()
        server.server_close()
    assert len(client.principles("confirmed")) == 1
