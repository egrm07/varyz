"""Models read the owner's turns for their profile; offline, with a fake model.

What a model gives back is only kept when the owner's own words back it: every
quote must be in the message it names, and an item needs two messages or more
(one for a vision, or a rule the owner said would always hold). What passes is
a candidate, and nothing is advice until the owner keeps it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from varyz.db.connection import open_db
from varyz.resolution import profile_reader
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.principles import GLOBAL_SCOPE
from varyz.resolution.profile_reader import (
    AGENT_OFFER_EVERY_SECONDS,
    AGENT_OFFER_MIN_TURNS,
    PROFILE_VERSION,
    READER_AGENT,
    SOURCES,
    ProfileItem,
    ProfileState,
    accept,
    agent_reading_offer,
    first_reading,
    known_from,
    next_task,
    profile_path,
    queue_items,
    read_with_model,
    reading_taken,
)
from varyz.resolution.rule_miner import MiningStats, _turn_scope
from varyz.resolution.rule_reader import ReaderUnavailable

HEADER = "From: me\nConversation: Claude Code — {project}\n\n"


def test_structured_replies_preserve_explicit_choices_and_skip_declined_offers():
    from varyz.resolution.profile_reader import _paired_decisions, _paired_replies, _read_text
    from varyz.resolution.rule_reader import Turn

    original = "<send_user_message_question_reply>" + json.dumps([
        {"question": "Keep Acorn's green brand?", "answer": "Keep it"},
        {"question": "Remember the grouping?", "answer": "Don't save"},
    ]) + "</send_user_message_question_reply>"
    turn = Turn("one", "hash", "thread", "2026-10-09", "acorn", original)
    assert "Structured reply handled separately" in _read_text([turn], "local")
    assert "Keep Acorn" not in _read_text([turn], "local")
    [item] = _paired_decisions({1: turn})
    assert item["question"] == "Keep Acorn's green brand?"
    assert item["answer"] == "Keep it"
    assert item["applies_to"] == "project"
    assert item["evidence"] == [{"message": 1, "quote": original}]
    assert _paired_replies("Quoted example: " + original) == []
    assert _paired_replies("<send_user_message_question_reply>bad json"
                           "</send_user_message_question_reply>") == []


def test_paired_selection_is_only_a_review_note_and_cannot_learn_a_declined_offer(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    original = "<send_user_message_question_reply>" + json.dumps([
        {"question": "Keep the shop green?", "answer": "Keep it green"},
        {"question": "Remember the grouping?", "answer": "Don't save"},
        {"question": "May I upload the document?", "answer": "Yes"},
    ]) + "</send_user_message_question_reply>"
    _entry(conn, "selection", original)
    state = ProfileState(tmp_path / "p.json")
    task = next_task(state, conn, tier="local", reader="local")
    accepted = accept(state, conn, task.id, [
        _item("rule", "Always use that grouping.", (1, original), applies="work"),
        {"slot": "rule", "statement": "Bad proof", "evidence": [{"message": []}]},
    ], tier="local")
    assert accepted.notes == 1
    [note] = state.notes
    assert (note["slot"], note["answer"], note["project"]) == (
        "decision", "Keep it green", "shop")
    assert note["evidence"][0]["quote"] == original
    assert accepted.items == []
    assert conn.execute("SELECT COUNT(*) FROM owner_knowledge").fetchone()[0] == 0


@pytest.mark.parametrize("keeps_mode", [False, True])
def test_quote_validity_does_not_allow_dropping_a_mode_condition(
    conn: sqlite3.Connection, tmp_path: Path, keeps_mode: bool,
) -> None:
    quote = "In offline mode you prompted too early; always defer permission until work is ready."
    _entry(conn, "conditional", quote)
    state = ProfileState(tmp_path / "p.json")
    task = next_task(state, conn, tier="local", reader="local")
    statement = "Defer permission until work is ready."
    if keeps_mode:
        statement = "In offline mode, " + statement.lower()
    accepted = accept(state, conn, task.id, [
        _item("rule", statement, (1, "always defer permission until work is ready."),
              applies="work",
              agent_did="prompted too early", wanted="defer permission"),
    ], tier="local")
    assert accepted.notes == int(keeps_mode)
    assert accepted.dropped == int(not keeps_mode)


# Spelled the way the repository spells test keys, so the release scan passes it.
AWS_KEY = "AKIA" + "C" * 16
THIN1 = "the headings are bold again. use thin fonts, never bold ones"
THIN2 = "why is this bold? use thin fonts like I said"
AIM = "this shop is for people who restore old bikes, it should feel like a workshop"


def _entry(conn, entry_id, text, *, day=1, project="shop", sensitivity="personal",
           content_hash=None, thread=None, said=None):
    when = datetime(2026, 9, day, tzinfo=UTC).isoformat()
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, thread_id, sensitivity, authored_by_user,
               conversation_metadata_json)
           VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (entry_id, entry_id, content_hash or entry_id, HEADER.format(project=project) + text,
         when, when, thread or f"ai:claude_code:{entry_id}", sensitivity,
         json.dumps({"context_before": said} if said else {})),
    )
    conn.commit()


def _item(slot, statement, *evidence, applies="project", **extra) -> dict[str, Any]:
    return {"slot": slot, "statement": statement, "applies_to": applies,
            "evidence": [{"message": number, "quote": quote} for number, quote in evidence],
            **extra}


def _merged(slot, statement, *notes, applies="project", **extra) -> dict[str, Any]:
    return {"slot": slot, "statement": statement, "applies_to": applies,
            "notes": list(notes), **extra}


def _reply(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


THIN = "Use thin fonts; never bold ones."
SHOP_THIN = "Use thin fonts in the shop; never bold ones."
VISION = "A shop for people who restore old bikes, that feels like a workshop."


class FakeModel:
    """Answers by the first key found in the task's text; nothing found, nothing."""

    def __init__(self, replies: dict[str, str | Exception]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        for words, reply in self.replies.items():
            if words in user:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return _reply()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def machine_fits(monkeypatch) -> None:
    """The CLI asks how this machine runs the model; here it always fits, offline."""
    from varyz.resolution import rule_reader

    monkeypatch.setattr(rule_reader, "local_fit", lambda cfg: ("fits", ""))


def _shop(conn) -> None:
    _entry(conn, "thin1", THIN1, day=1)
    _entry(conn, "thin2", THIN2, day=2)
    _entry(conn, "aim", AIM, day=3)


# -- read, then merge ---------------------------------------------------------


def test_a_read_keeps_only_notes_the_messages_they_name_back(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    _entry(conn, "secret", "never share the vault password with anyone", sensitivity="sensitive")
    state = ProfileState(tmp_path / "p.json")

    task = next_task(state, conn, tier="local", reader="local")
    assert task is not None and task.kind == "read" and task.project == "shop"
    assert task.text.startswith("Project: shop\n\nConversation 1\n\nMessage 1 (2026-09-01):\n")
    assert "vault" not in task.text

    accepted = accept(state, conn, task.id, [
        _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts"), applies="products"),
        _item("vision", VISION, (3, "people who restore old bikes")),
        _item("rule", "Always write the tests first.", (1, "write the tests first")),
        _item("rule", "Use thin fonts on every page.", (9, "use thin fonts")),
        _item("rule", "Thin fonts.", (1, "use thin fonts")),  # too short to hold a rule
        _item("workflow", "", (1, "use thin fonts"), name="fonts", steps=["thin"]),
    ], tier="local")

    assert (accepted.notes, accepted.dropped, accepted.items) == (2, 4, [])
    assert set(state.read) == {"thin1", "thin2", "aim"}
    assert state.dirty == {"shop"}
    assert [note["project"] for note in state.notes] == ["shop", "shop"]
    # Asked again, the task is not open any more.
    with pytest.raises(KeyError):
        accept(state, conn, task.id, [], tier="local")


def test_an_older_install_leaves_a_newer_reading_alone(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    path = tmp_path / "p.json"
    state = ProfileState(path)
    task = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, task.id, [], tier="local")
    state.save()
    newer = json.loads(path.read_text(encoding="utf-8"))
    newer["version"] = PROFILE_VERSION + 1
    path.write_text(json.dumps(newer), encoding="utf-8")

    older = ProfileState(path)
    assert profile_reader.waiting(older, conn, "local") == 0
    assert next_task(older, conn, tier="local", reader="local") is None
    older.save()
    assert json.loads(path.read_text(encoding="utf-8")) == newer


def test_a_merge_needs_two_messages_unless_it_is_a_vision_or_said_to_always_hold(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "header", "make the header taller than it is now", day=1)
    _entry(conn, "tests", "always run the tests before you tell me it works", day=2)
    _entry(conn, "aim", AIM, day=3)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", "Make the shop header taller than it is.", (1, "make the header taller")),
        _item("rule", "Run the shop's tests before saying it works.",
              (2, "always run the tests")),
        _item("vision", VISION, (3, "people who restore old bikes")),
    ], tier="local")

    merge = next_task(state, conn, tier="local", reader="local",
                      known=lambda scope: ["Keep the copy short."] if scope == "shop" else [])
    assert merge.kind == "merge"
    assert ("1. [rule · project] Make the shop header taller than it is. "
            "(1 messages, latest 2026-09-01)" in merge.text)
    assert "Already in the profile, do not repeat:\n- Keep the copy short." in merge.text
    accepted = accept(state, conn, merge.id, [
        _merged("rule", "Make the shop header taller than it is.", 1),
        _merged("rule", "Run the shop's tests before saying it works.", 2),
        _merged("vision", VISION, 3),
        _merged("rule", "Something that no note says at all.", 7),
    ], tier="local")

    assert [(item.slot, item.statement, item.scope) for item in accepted.items] == [
        ("rule", "Run the shop's tests before saying it works.", "shop"),
        ("vision", VISION, "shop"),
    ]
    assert accepted.dropped == 2
    assert next_task(state, conn, tier="local", reader="local") is None


def test_what_holds_in_every_project_is_merged_once_more_across_them(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    _entry(conn, "blog1", "use thin fonts here too", day=4, project="blog")
    _entry(conn, "blog2", "again: use thin fonts", day=5, project="blog")
    state = ProfileState(tmp_path / "p.json")
    for project, evidence in (("shop", ((1, "use thin fonts"), (2, "use thin fonts"))),
                              ("blog", ((1, "use thin fonts"), (2, "use thin fonts")))):
        read = next_task(state, conn, tier="local", reader="local")
        assert read.project == project
        accept(state, conn, read.id, [_item("rule", THIN, *evidence, applies="products")],
               tier="local")

    # Each project first; what holds in all of them last.
    for project in ("blog", "shop"):
        merge = next_task(state, conn, tier="local", reader="local")
        assert merge.project == project
        accepted = accept(state, conn, merge.id, [
            _merged("rule", THIN, 1, applies="products"),
            _merged("rule", "Thin fonts, the same note given twice.", 1, applies="products"),
        ], tier="local")
        assert (accepted.items, accepted.dropped) == ([], 1)

    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == GLOBAL_SCOPE and merge.text.startswith("Project: all projects")
    accepted = accept(state, conn, merge.id, [_merged("rule", THIN, 1, 2)], tier="local")
    [item] = accepted.items
    assert (item.scope, item.messages) == (GLOBAL_SCOPE, 4)


def test_what_kind_an_item_is_comes_from_its_notes_not_the_merge(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    _entry(conn, "browser", "open the browser and check it yourself, do not list steps",
           day=4)
    _entry(conn, "browser2", "again, drive the browser yourself", day=5)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("vision", VISION, (3, "people who restore old bikes")),
        _item("workflow", "", (1, "the headings are bold"), (2, "why is this bold"),
              name="a page", steps=["headings", "fonts", "check it in the browser"]),
        _item("rule", "Drive the browser yourself on the shop instead of listing steps.",
              (4, "check it yourself"), (5, "drive the browser yourself")),
    ], tier="local")
    merge = next_task(state, conn, tier="local", reader="local")
    # What a 4B model gave back: the vision and the workflow joined as a rule,
    # and the rule called a workflow.
    accepted = accept(state, conn, merge.id, [
        _merged("rule", "Restore bikes: headings, fonts, then the browser check.", 1, 2),
        _merged("workflow", "Drive the browser yourself on the shop instead of listing steps.",
                3),
    ], tier="local")
    assert sorted((item.slot, item.statement) for item in accepted.items) == [
        ("rule", "Drive the browser yourself on the shop instead of listing steps."),
        ("vision", VISION),
        ("workflow", "Take a page through these steps in order: headings; fonts; "
                     "check it in the browser."),
    ]


def test_merging_a_project_again_replaces_what_it_sent_on(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts"), applies="products")
    ], tier="local")
    for _ in range(2):
        state.dirty.add("shop")
        merge = next_task(state, conn, tier="local", reader="local")
        accept(state, conn, merge.id, [_merged("rule", THIN, 1, applies="products")], tier="local")
    assert [note["project"] for note in state.notes].count(GLOBAL_SCOPE) == 1


def test_new_turns_are_read_and_their_project_merged_again(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    path = tmp_path / "p.json"
    model = FakeModel({"Message 1": _reply(
        _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts"))
    ), "Notes:": _reply(_merged("rule", THIN, 1))})
    state = ProfileState(path)
    [item] = read_with_model(state, conn, model, tier="local", reader="local")
    state.save()
    # Said in the shop but naming nothing of it: it holds in every project.
    assert item.scope == GLOBAL_SCOPE and item.messages == 2

    _entry(conn, "thin3", "these are bold again, thin fonts please", day=6)
    again = FakeModel({"Message 1": _reply(
        _item("rule", "Keep the headings thin as well.", (1, "thin fonts please"))
    )})
    state = ProfileState(path)
    read_with_model(state, conn, again, tier="local", reader="local",
                    known=lambda scope: [THIN])
    [read, merge] = again.calls
    assert "bold again, thin fonts please" in read and "restore old bikes" not in read
    assert f"do not repeat:\n- {THIN}" in merge


# -- who sees what ------------------------------------------------------------


def test_the_cloud_sees_only_classified_turns_with_credentials_removed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "key", f"always use the key {AWS_KEY} for the uploads bucket")
    _entry(conn, "unsorted", "never deploy on a friday afternoon", sensitivity="unclassified")
    _entry(conn, "secret", "never share the vault password", sensitivity="sensitive")
    cloud = next_task(ProfileState(tmp_path / "c.json"), conn, tier="cloud", reader="agent")
    assert AWS_KEY not in cloud.text
    assert "[AWS access key id removed]" in cloud.text
    assert "friday" not in cloud.text and "vault" not in cloud.text
    # On the machine, every turn that is not sensitive, as written.
    local = next_task(ProfileState(tmp_path / "l.json"), conn, tier="local", reader="local")
    assert AWS_KEY in local.text and "friday" in local.text
    assert "vault" not in local.text


def test_the_local_model_and_the_cloud_keep_separate_profiles(tmp_path: Path) -> None:
    cfg = type("Cfg", (), {"data_dir": tmp_path})()
    assert profile_path(cfg, "local") != profile_path(cfg, "cloud")


# -- running a model ----------------------------------------------------------


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_a_malformed_reply_leaves_the_task_for_the_next_run(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    stats = MiningStats()
    state = ProfileState(tmp_path / "p.json")
    assert read_with_model(state, conn, FakeModel({"Message 1": "Sure! Here it is:"}),
                           tier="local", reader="local", stats=stats) == []
    assert stats.unread == 1 and state.read == {}


def test_the_model_going_away_stops_the_run_and_keeps_what_was_read(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    from varyz.resolution import profile_reader

    monkeypatch.setitem(profile_reader.TASK_CHARS, "local", 10)
    _shop(conn)
    for day in (10, 11, 12):
        _entry(conn, f"gone{day}", f"turn {day} after Ollama stopped answering", day=day,
               project=f"p{day}")
    path = tmp_path / "p.json"
    model = FakeModel({"Ollama stopped": httpx.ConnectError("connection refused")})
    with pytest.raises(ReaderUnavailable):
        read_with_model(ProfileState(path), conn, model, tier="local", reader="local")
    assert set(json.loads(path.read_text(encoding="utf-8"))["read"]) == {"thin1", "thin2", "aim"}


def test_a_busy_cloud_hands_over_and_an_invalid_request_does_not(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _shop(conn)
    with pytest.raises(ReaderUnavailable):
        read_with_model(ProfileState(tmp_path / "a.json"), conn,
                        FakeModel({"Message": _StatusError(429)}), tier="cloud", reader="cloud")
    with pytest.raises(_StatusError):
        read_with_model(ProfileState(tmp_path / "b.json"), conn,
                        FakeModel({"Message": _StatusError(400)}), tier="cloud", reader="cloud")


def test_the_cloud_stops_at_the_call_cap_and_leaves_the_rest_for_next_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    for day in range(1, 6):
        _entry(conn, f"t{day}", f"turn number {day} about the settings page", day=day,
               project=f"p{day}")
    stats = MiningStats()
    model = FakeModel({})
    read_with_model(ProfileState(tmp_path / "c.json"), conn, model, tier="cloud",
                    reader="cloud", max_calls=2, stats=stats)
    assert len(model.calls) == 2 and stats.deferred == 3


def test_a_workflow_is_queued_as_a_candidate_that_carries_its_steps() -> None:
    evidence = [{"entry_id": "a", "quote": "colour first", "occurred": "2026-09-01"},
                {"entry_id": "b", "quote": "then the logo", "occurred": "2026-09-02"}]
    item = ProfileItem("workflow", "", "shop", domain="website", name="a website",
                       steps=["colour", "logo", "page"], evidence=evidence)
    added: list[dict[str, Any]] = []

    def add(**fields):
        added.append(fields)
        return None if len(added) > 1 else "p1"

    assert queue_items([item, item], add, SOURCES["local"], {"tool": "t"}) == (1, 1)
    fields = added[0]
    assert fields["statement"] == (
        "Take a website through these steps in order: colour; logo; page."
    )
    assert (fields["kind"], fields["domain"], fields["status"]) == ("rule", "", "candidate")
    assert fields["provenance"]["workflow"] == {
        "name": "a website", "steps": ["colour", "logo", "page"], "domain": "website"
    }
    assert fields["provenance"]["messages"] == 2 and fields["provenance"]["tool"] == "t"


def test_a_merge_of_several_notes_that_keeps_nothing_is_handed_out_once_more(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    for day, text in enumerate((THIN1, "no emojis in the copy anywhere on the site please",
                                "always check it in the browser before you tell me"), 1):
        _entry(conn, f"m{day}", text, day=day)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", THIN, (1, "use thin fonts")),
        _item("rule", "Write the copy without any emojis.", (2, "no emojis in the copy")),
        _item("done", "Check the page in the browser before saying it is done.",
              (3, "always check it in the browser")),
    ], tier="local")
    assert len(state.notes) == 3

    merge = next_task(state, conn, tier="local", reader="local")
    skipped = accept(state, conn, merge.id, [], tier="local")
    assert skipped.redo and state.dirty == {"shop"}
    again = next_task(state, conn, tier="local", reader="local")
    assert again.id == merge.id
    meant = accept(state, conn, again.id, [], tier="local")
    assert not meant.redo and state.dirty == set()


HOSTED = "Where is this site hosted and on which domain?"


def test_a_decision_takes_the_briefings_wording_and_the_owners_latest_answer(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "netlify", "put it on netlify", day=1)
    _entry(conn, "pages", "move it to cloudflare pages, on bikes.shop", day=5)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("decision", "", (1, "put it on netlify"),
              question="where is this site hosted and on which domain",
              answer="Netlify.", why="It was quickest to set up."),
        _item("decision", "", (2, "cloudflare pages, on bikes.shop"),
              question="Where is the site hosted?", answer="Cloudflare Pages, bikes.shop."),
        _item("decision", "", (2, "cloudflare pages"),
              question="Hosting?", answer="Cloudflare."),  # too short to ask again
    ], tier="local")
    assert [note["question"] for note in state.notes] == [HOSTED, "Where is the site hosted?"]
    assert state.notes[0]["why"] == "It was quickest to set up."

    merge = next_task(state, conn, tier="local", reader="local")
    assert (f"[decision · project] {HOSTED} -> Netlify. (1 messages, latest 2026-09-01)"
            in merge.text)
    accepted = accept(state, conn, merge.id, [
        # The merge words the question; the answer stays what the owner said last.
        _merged("decision", "", 1, 2, question=HOSTED, answer="Netlify."),
    ], tier="local")

    [item] = accepted.items
    assert (item.slot, item.question, item.answer) == (
        "decision", HOSTED, "Cloudflare Pages, bikes.shop."
    )
    assert item.why == "It was quickest to set up."
    fields = item.principle(SOURCES["local"])
    assert fields["statement"] == f"{HOSTED} Cloudflare Pages, bikes.shop."
    assert fields["rationale"] == "It was quickest to set up."
    assert fields["provenance"]["decision"] == {
        "question": HOSTED, "answer": "Cloudflare Pages, bikes.shop."
    }


def test_what_the_owner_already_answered_is_not_read_again() -> None:
    known = known_from(
        [{"statement": THIN, "scope": "shop"}],
        [{"question": "Which analytics", "answer": "None.", "scope": "shop"},
         {"question": "Which editor?", "answer": "Zed.", "scope": GLOBAL_SCOPE}],
    )
    assert known("shop") == [THIN, "Which analytics? None."]
    assert known(GLOBAL_SCOPE) == ["Which editor? Zed."]


# -- CLI -----------------------------------------------------------------------


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
        device_id="device:test", notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


def _queue_only(tmp_path: Path) -> None:
    """Every candidate waits for review: trial rules (``trials``) stopped."""
    from varyz.resolution.trials import off_path

    sentinel = off_path(tmp_path / "data" / "varyz.db")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("off", encoding="utf-8")


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    return path


SHOP_MODEL = {
    "Message 1": _reply(
        _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts")),
        _item("vision", VISION, (3, "people who restore old bikes")),
        _item("workflow", "", (1, "the headings are bold"), (2, "why is this bold"),
              name="a page", steps=["headings", "fonts", "check it in the browser"]),
    ),
    "Notes:": _reply(
        _merged("rule", THIN, 1), _merged("vision", VISION, 2),
        _merged("workflow", "", 3, name="a page",
                steps=["headings", "fonts", "check it in the browser"]),
    ),
}


def test_cli_mine_queues_the_profile_and_review_shows_the_owners_words(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _shop(conn)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    monkeypatch.setattr(rule_reader, "reader_from_config",
                        lambda cfg, **kwargs: FakeModel(SHOP_MODEL))
    runner = CliRunner()

    applied = runner.invoke(cli.app, ["rules", "mine", "--apply", "--config", str(config)])
    assert applied.exit_code == 0, applied.output
    output = " ".join(applied.output.split())
    assert "found: vision │ 1" in output and "found: workflows │ 1" in output
    candidates = {
        item["provenance"]["slot"]: item for item in daemon_client.principles("candidate")
    }
    # The rule was said in two conversations: it is in effect on trial at once.
    assert set(candidates) == {"vision", "workflow"}
    assert "in effect on trial │ 1" in output
    assert candidates["vision"]["kind"] == "vision" and candidates["vision"]["scope"] == "shop"
    [trial] = daemon_client.principles("confirmed")
    assert trial["provenance"]["trial"] and trial["source"] == SOURCES["local"]
    assert (tmp_path / "data" / "rules" / "profile_local.json").is_file()

    # Nothing new: a second run asks the model nothing and queues nothing.
    again = runner.invoke(cli.app, ["rules", "mine", "--apply", "--config", str(config)])
    assert "queued for review │ 0" in " ".join(again.output.split())

    # The spot-check of the trial rule first, then the vision and the workflow;
    # keep all three.
    reviewed = runner.invoke(
        cli.app, ["rules", "review", "--terminal", "--config", str(config)],
        input="a\na\na\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    output = " ".join(reviewed.output.split())
    assert "you wrote: “use thin fonts”" in output
    assert daemon_client.principles("candidate") == ()
    [workflow] = daemon_client.workflows()
    assert workflow["name"] == "a page" and workflow["basis"] == "stated"
    assert {item["statement"] for item in daemon_client.principles("confirmed")} == {
        THIN, VISION
    }


def test_cli_mine_queues_nothing_when_no_model_can_read(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _entry(conn, "report", "No, always write the report before the summary.")
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)

    def unavailable(cfg, **kwargs):
        raise ReaderUnavailable("Ollama is not running")

    monkeypatch.setattr(rule_reader, "reader_from_config", unavailable)
    result = CliRunner().invoke(cli.app, ["rules", "mine", "--apply", "--config", str(config)])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    # The owner's sentences as typed read as no rule: nothing is queued, and the
    # owner's agent is pointed to instead.
    assert "Ollama is not running" in output and "nothing was queued" in output
    assert "Ask your AI agent to read them" in output
    assert not daemon_client.principles("candidate")


def test_cli_mine_lexical_never_asks_the_model(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _entry(conn, "report", "No, always write the report before the summary.")
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)

    def must_not_be_called(cfg, **kwargs):
        raise AssertionError("the model was asked")

    monkeypatch.setattr(rule_reader, "reader_from_config", must_not_be_called)
    result = CliRunner().invoke(
        cli.app, ["rules", "mine", "--lexical", "--apply", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert len(daemon_client.principles("candidate")) == 1


def test_cli_the_cloud_reads_first_and_no_cloud_keeps_turns_on_this_machine(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli
    from varyz.config import load_config
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _shop(conn)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)

    def no_local(cfg, **kwargs):
        raise AssertionError("the local model was asked before the cloud")

    monkeypatch.setattr(rule_reader, "reader_from_config", no_local)
    monkeypatch.setattr(rule_reader, "cloud_reader_from_config",
                        lambda cfg, **kwargs: (FakeModel(SHOP_MODEL), "claude:test"))
    runner = CliRunner()

    # The default, with nothing said: the cloud reads, even where the local model fits.
    assert load_config(config).workflows.rule_reader_cloud is True
    result = runner.invoke(cli.app, ["rules", "mine", "--apply", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "short for the local model" not in result.output
    assert {item["source"] for item in daemon_client.principles("candidate")} == {
        SOURCES["cloud"]
    }
    assert (tmp_path / "data" / "rules" / "profile_cloud.json").is_file()

    def no_cloud(cfg, **kwargs):
        raise AssertionError("the cloud was asked after --no-cloud")

    monkeypatch.setattr(rule_reader, "cloud_reader_from_config", no_cloud)
    monkeypatch.setattr(rule_reader, "reader_from_config",
                        lambda cfg, **kwargs: FakeModel(SHOP_MODEL))
    kept_here = runner.invoke(cli.app, ["rules", "mine", "--no-cloud", "--config", str(config)])
    assert kept_here.exit_code == 0, kept_here.output
    assert "on this machine only" in " ".join(kept_here.output.split())
    assert load_config(config).workflows.rule_reader_cloud is False
    assert (tmp_path / "data" / "rules" / "profile_local.json").is_file()
    # Remembered: the next run without a flag stays on this machine too.
    again = runner.invoke(cli.app, ["rules", "mine", "--config", str(config)])
    assert again.exit_code == 0, again.output

    runner.invoke(cli.app, ["rules", "mine", "--cloud", "--lexical", "--config", str(config)])
    assert load_config(config).workflows.rule_reader_cloud is True


def test_cli_the_local_model_reads_when_the_cloud_cannot(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _shop(conn)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    monkeypatch.setattr(rule_reader, "reader_from_config",
                        lambda cfg, **kwargs: FakeModel(SHOP_MODEL))
    # The suite has no cloud sign-in, as a machine that never ran `varyz connect`.
    result = CliRunner().invoke(cli.app, ["rules", "mine", "--apply", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "no cloud sign-in" in " ".join(result.output.split())
    assert {item["source"] for item in daemon_client.principles("candidate")} == {
        SOURCES["local"]
    }


def test_cli_kept_on_a_tight_machine_says_how_to_let_the_cloud_read(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch
) -> None:
    from varyz import cli
    from varyz.resolution import rule_reader

    config = _config(tmp_path)
    _shop(conn)
    monkeypatch.setattr(rule_reader, "local_fit", lambda cfg: ("tight", "8 GB memory"))
    monkeypatch.setattr(rule_reader, "reader_from_config", lambda cfg, **kwargs: FakeModel({}))

    def no_cloud(cfg, **kwargs):
        raise AssertionError("the cloud was asked after --no-cloud")

    monkeypatch.setattr(rule_reader, "cloud_reader_from_config", no_cloud)
    result = CliRunner().invoke(cli.app, ["rules", "mine", "--no-cloud", "--config", str(config)])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "short for the local model: 8 GB memory" in output
    assert "varyz rules mine --cloud" in output


# -- the agent the owner works in reads them, through a subagent ---------------


def _agent_server(tmp_path: Path, daemon_client: ResolutionClient, *, registered: bool = True):
    from varyz.mcp_server import VaryzServer
    from varyz.trust import register_agent

    token = ""
    if registered:
        conn = open_db(tmp_path / "data" / "varyz.db")
        token = register_agent(
            conn, agent_id="agent:claude-code:local", display_name="Claude Code",
            device_id="local",
        ).token
        conn.close()
    return VaryzServer(_config(tmp_path), agent_token=token, resolution_client=daemon_client)


def test_start_task_hands_the_reading_to_a_subagent_until_it_starts_and_it_queues_checked_items(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient
) -> None:
    _queue_only(tmp_path)
    _shop(conn)
    _entry(conn, "key", f"always use the key {AWS_KEY} for uploads", day=4)
    _entry(conn, "secret", "never share the vault password", sensitivity="sensitive")
    server = _agent_server(tmp_path, daemon_client)
    try:
        job = server.call("start_task", {"task": "add a settings page"})["background_job"]
        assert job["turns_waiting"] == 4
        assert "run_in_background" in job["do"] and READER_AGENT in job["do"]
        assert "profile_reading_task" in job["subagent_prompt"]
        # An ask the agent passed over is made again; one a reader took is not.
        assert "background_job" in server.call("start_task", {"task": "fix the header"})

        read = server.call("profile_reading_task", {})
        assert "background_job" not in server.call("start_task", {"task": "fix the footer"})
        assert read["kind"] == "read" and read["turns_left_to_read"] == 4
        assert AWS_KEY not in read["text"] and "vault" not in read["text"]
        noted = server.call("submit_profile_reading", {"task_id": read["task_id"], "items": [
            _item("rule", THIN, (1, "use thin fonts"), (2, "use thin fonts")),
            _item("rule", "Always write the tests first.", (1, "write the tests first")),
        ]})
        assert (noted["noted"], noted["queued"], noted["dropped"]) == (1, 0, 1)

        merge = server.call("profile_reading_task", {})
        assert merge["kind"] == "merge" and merge["turns_left_to_read"] == 0
        merged = server.call("submit_profile_reading", {"task_id": merge["task_id"],
                                                        "items": [_merged("rule", THIN, 1)]})
        # Thin fonts name nothing of the shop: merged again across projects.
        assert merged["queued"] == 0
        merge = server.call("profile_reading_task", {})
        assert merge["text"].startswith("Project: all projects")
        merged = server.call("submit_profile_reading", {"task_id": merge["task_id"],
                                                        "items": [_merged("rule", THIN, 1)]})
        assert merged["queued"] == 1
        assert server.call("profile_reading_task", {}) == {"done": True}
        stale = server.call("submit_profile_reading", {"task_id": merge["task_id"], "items": []})
        assert "not open any more" in stale["error"]
    finally:
        server.close()

    [candidate] = daemon_client.principles("candidate")
    assert (candidate["statement"], candidate["scope"]) == (THIN, GLOBAL_SCOPE)
    assert candidate["source"] == SOURCES["agent"]
    assert candidate["provenance"]["relayed_by"] == "agent:claude-code:local"


def test_an_unregistered_agent_is_neither_asked_nor_given_turns(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient
) -> None:
    _shop(conn)
    server = _agent_server(tmp_path, daemon_client, registered=False)
    try:
        assert "background_job" not in server.call("start_task", {"task": "add a page"})
        task = server.call("profile_reading_task", {})
        assert task["done"] is True and "varyz install" in task["error"]
        assert server.call("submit_profile_reading", {"task_id": "read-x", "items": []})[
            "queued"] == 0
    finally:
        server.close()


def test_a_merge_is_done_again_when_the_daemon_could_not_keep_its_items(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz.resolution.client import ResolutionClientError

    _shop(conn)
    server = _agent_server(tmp_path, daemon_client)
    try:
        read = server.call("profile_reading_task", {})
        server.call("submit_profile_reading", {"task_id": read["task_id"], "items": [
            _item("rule", SHOP_THIN, (1, "use thin fonts"), (2, "use thin fonts")),
        ]})
        merge = server.call("profile_reading_task", {})
        real = server._with_daemon

        def down(operation):
            raise ResolutionClientError("daemon stopped")

        monkeypatch.setattr(server, "_with_daemon", down)
        failed = server.call("submit_profile_reading", {"task_id": merge["task_id"],
                                                        "items": [_merged("rule", SHOP_THIN, 1)]})
        assert failed["error"] == "resolution_daemon_unavailable"
        monkeypatch.setattr(server, "_with_daemon", real)
        # Nothing was kept, so the same merge is handed out again.
        assert server.call("profile_reading_task", {})["task_id"] == merge["task_id"]
    finally:
        server.close()


def test_after_the_first_reading_the_agent_is_asked_again_only_for_enough_new_turns(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    from varyz.config import load_config

    cfg = load_config(_config(tmp_path))
    start = 1_790_000_000.0
    _entry(conn, "aim", AIM)
    assert agent_reading_offer(cfg, conn, now=start)["turns_waiting"] == 1
    # Asking records nothing; a reader taking it does, and then waits a day.
    assert agent_reading_offer(cfg, conn, now=start + 60)["turns_waiting"] == 1
    reading_taken(cfg, start)
    assert agent_reading_offer(cfg, conn, now=start + 120) is None
    state = ProfileState(profile_path(cfg, "cloud"))
    state.read["aim"] = "aim"
    state.save()
    later = start + AGENT_OFFER_EVERY_SECONDS + 1
    for number in range(AGENT_OFFER_MIN_TURNS - 1):
        _entry(conn, f"turn{number}", f"please make the header taller, version {number}")
    assert agent_reading_offer(cfg, conn, now=later) is None
    _entry(conn, "one-more", "please make the footer shorter than before")
    assert agent_reading_offer(cfg, conn, now=later)["turns_waiting"] == AGENT_OFFER_MIN_TURNS
    cfg.workflows.rule_reader_agent = False
    assert agent_reading_offer(cfg, conn, now=later * 2) is None


def test_setup_names_the_reading_the_agents_first_use_starts_without_holding_it_back(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    from varyz.config import load_config

    cfg = load_config(_config(tmp_path))
    start = 1_790_000_000.0
    assert first_reading(cfg, conn) is None
    _entry(conn, "aim", AIM)

    assert first_reading(cfg, conn) == {"turns_waiting": 1}
    # Setup only says what will happen: the agent's first start_task still starts it.
    offer = agent_reading_offer(cfg, conn, now=start)
    assert offer is not None and READER_AGENT in offer["do"]
    cfg.workflows.rule_reader_agent = False
    assert first_reading(cfg, conn) is None


def test_routine_readings_are_silent_and_only_a_major_one_is_announced_once_a_month(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch
) -> None:
    from varyz.config import load_config

    monkeypatch.setattr(profile_reader, "MAJOR_READING_TURNS", AGENT_OFFER_MIN_TURNS + 5)
    cfg = load_config(_config(tmp_path))
    start = 1_790_000_000.0
    _entry(conn, "aim", AIM)
    # The first reading is the setup step: the owner is told plainly what it does.
    first = agent_reading_offer(cfg, conn, now=start)
    assert first["major"] and first["announce"] == profile_reader.ANNOUNCEMENT
    assert first["setup"] in first["do"] and "varyz rules review" in first["do"]
    reading_taken(cfg, start)
    state = ProfileState(profile_path(cfg, "cloud"))
    state.read["aim"] = "aim"
    state.save()

    # A routine daily reading says nothing, and neither does its result.
    later = start + AGENT_OFFER_EVERY_SECONDS + 1
    for number in range(AGENT_OFFER_MIN_TURNS):
        _entry(conn, f"turn{number}", f"please make the header taller, version {number}")
    daily = agent_reading_offer(cfg, conn, now=later)
    assert not daily["major"] and daily["announce"] is None
    assert profile_reader.SILENT in daily["do"] and "Tell the user" not in daily["do"]
    live = profile_reader.live_offer(cfg, conn, later, "from now on always use thin fonts")
    assert live is not None and profile_reader.SILENT in live["do"]

    # A large backlog is major, but is announced again only after a month.
    for number in range(5):
        _entry(conn, f"more{number}", f"please make the footer shorter, version {number}")
    backlog = agent_reading_offer(cfg, conn, now=later)
    assert backlog["major"] and backlog["announce"] is None
    month = start + profile_reader.ANNOUNCE_AGAIN_SECONDS + 1
    assert agent_reading_offer(cfg, conn, now=month)["announce"] == profile_reader.ANNOUNCEMENT


def test_the_first_reading_is_an_explained_setup_step_the_owner_can_put_off(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    from varyz.config import load_config

    cfg = load_config(_config(tmp_path))
    start = 1_790_000_000.0
    _entry(conn, "aim", AIM)
    first = agent_reading_offer(cfg, conn, now=start)
    # It says what it reads, on what, what it learns, what it skips and where it goes.
    assert first["setup"] == profile_reader.setup_step(1)
    for said in ("your 1 past message to AI agents", "your own Claude or ChatGPT plan", "taste",
                 "skips anything", "on trial", "varyz rules review", "not now"):
        assert said in first["setup"]
    # On by default; "not now" asks when and keeps that time.
    assert "do not ask permission" in first["do"] and "schedule_reading" in first["do"]
    assert first["subagent_prompt"] == profile_reader.AGENT_PROMPT
    assert "—" not in first["do"]

    later = start + 3 * 3600
    profile_reader.defer_reading(cfg, later)
    assert profile_reader.deferred_until(cfg, start) == later
    assert agent_reading_offer(cfg, conn, now=start + 60) is None
    assert profile_reader.live_offer(
        cfg, conn, start + 60, "from now on always use thin fonts") is None
    # At that time it is asked for again, still as the setup step.
    assert profile_reader.deferred_until(cfg, later) is None
    assert agent_reading_offer(cfg, conn, now=later)["setup"]


# -- the review page ------------------------------------------------------------


def _owner(tmp_path: Path):
    from varyz import cli
    from varyz.config import load_config

    cfg = load_config(_config(tmp_path))
    return cli._local_owner_caller(cfg, "owner_knowledge"), cfg.resolution.device_id


def test_full_rules_view_lists_every_status_and_every_trial(
    tmp_path: Path, daemon_client: ResolutionClient,
) -> None:
    from varyz.review_web import ReviewSession

    _queue_only(tmp_path)
    caller, device_id = _owner(tmp_path)
    live = daemon_client.add_principle(
        statement="Use thin type.", caller=caller, device_id=device_id)
    pending = daemon_client.add_principle(
        statement="Use local assets.", status="candidate", caller=caller, device_id=device_id)
    withdrawn = daemon_client.add_principle(
        statement="Use a blue accent.", caller=caller, device_id=device_id)
    daemon_client.retract_principle(
        withdrawn, caller=caller, device_id=device_id, reason="Owner changed the colour.")
    for number in range(12):
        daemon_client.add_principle(
            statement=f"Trial rule {number}.", caller=caller, device_id=device_id,
            provenance={"trial": {"since": "2026-09-01", "signals": ["repeated"]}})
    result = ReviewSession(daemon_client, caller, device_id).items()
    assert result["counts"] == {"live": 1, "pending": 1, "trial": 12, "withdrawn": 1}
    views = {item["id"]: item for item in result["items"]}
    assert views[live]["status"] == "live"
    assert views[pending]["status"] == "pending"
    assert views[withdrawn]["status"] == "withdrawn"
    assert views[withdrawn]["withdrawn_reason"] == "Owner changed the colour."
    assert views[withdrawn]["withdrawn_at"]
    assert len(daemon_client.principles("confirmed")) == 13


def test_live_rule_edit_withdraw_and_stale_actions_preserve_the_ledger(
    tmp_path: Path, daemon_client: ResolutionClient,
) -> None:
    from varyz.review_web import ReviewSession

    caller, device_id = _owner(tmp_path)
    original = daemon_client.add_principle(
        statement="Keep reports short.", scope="shop", domain="writing",
        rationale="Make them easy to read.", caller=caller, device_id=device_id)
    session = ReviewSession(daemon_client, caller, device_id)
    result = session.decide([{"id": original, "action": "keep",
                              "statement": "Keep reports concise.", "scope": "*"}])
    assert result == {"done": [original], "errors": {}}
    [updated] = daemon_client.principles("confirmed")
    assert updated["supersedes_id"] == original
    assert (updated["statement"], updated["scope"], updated["domain"]) == (
        "Keep reports concise.", "*", "writing")
    assert updated["rationale"] == "Make them easy to read."
    assert session.decide([{"id": original, "action": "drop"}])["errors"]
    assert session.decide([{"id": updated["id"], "action": "drop"}])["done"] == [updated["id"]]
    [stopped] = daemon_client.principles("withdrawn")
    assert stopped["id"] == updated["id"]
    assert session.items()["counts"] == {"live": 0, "trial": 0, "pending": 0, "withdrawn": 1}
    assert session.decide([{"id": stopped["id"], "action": "keep"}])["errors"]
    assert not daemon_client.principles("confirmed")


def test_the_review_page_groups_by_project_and_keeps_and_drops_in_bulk(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient
) -> None:
    from varyz.review_web import ReviewServer, ReviewSession

    _queue_only(tmp_path)
    caller, device_id = _owner(tmp_path)
    evidence = [{"entry_id": "a", "quote": "use thin fonts", "occurred": "2026-09-01"},
                {"entry_id": "b", "quote": "thin fonts please", "occurred": "2026-09-02"}]
    items = [
        ProfileItem("rule", THIN, "shop", evidence=evidence),
        ProfileItem("vision", VISION, "shop", evidence=evidence[:1]),
        ProfileItem("rule", "Drive the browser yourself instead of listing steps.",
                    GLOBAL_SCOPE, evidence=evidence),
        ProfileItem("workflow", "", "shop", name="a page", steps=["colour", "logo"],
                    evidence=evidence),
    ]
    queue_items(items, lambda **fields: daemon_client.add_principle(
        caller=caller, device_id=device_id, **fields), SOURCES["agent"])

    session = ReviewSession(daemon_client, caller, device_id)
    views = session.items()["items"]
    assert [(view["scope"], view["slot"]) for view in views] == [
        (GLOBAL_SCOPE, "rule"), ("shop", "vision"), ("shop", "rule"), ("shop", "workflow"),
    ]
    assert views[2]["quotes"][0] == {"quote": "use thin fonts", "occurred": "2026-09-01"}
    assert views[3]["workflow"] == {"name": "a page", "steps": ["colour", "logo"]}

    server = ReviewServer(session)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = server.url.split("/?")[0]
        assert httpx.get(base + "/").status_code == 403  # no key
        assert httpx.get(server.url).status_code == 200
        assert httpx.get(base + "/api/items").status_code == 403
        header = {"X-Varyz-Review": session.token}
        assert httpx.get(base + "/api/items", headers=header,
                         ).json()["items"][0]["slot"] == "rule"
        wrong_host = httpx.get(base + "/api/items",
                               headers={**header, "Host": "evil.example"})
        assert wrong_host.status_code == 403
        decided = httpx.post(base + "/api/decide", headers=header, json={"decisions": [
            {"id": views[0]["id"], "action": "keep", "scope": "shop"},
            {"id": views[1]["id"], "action": "keep",
             "statement": "A shop for bike restorers that feels like a workshop."},
            {"id": views[2]["id"], "action": "drop"},
            {"id": views[3]["id"], "action": "keep", "name": "a landing page",
             "steps": ["colour", " logo ", "", "copy"], "scope": "*"},
        ]}).json()
        assert len(decided["done"]) == 4 and decided["errors"] == {}
    finally:
        server.shutdown()
        server.server_close()

    assert (session.kept, session.dropped) == (3, 1)
    assert daemon_client.principles("candidate") == ()
    # Kept as the owner edited them: wording, project, a workflow's name and steps.
    assert {(item["statement"], item["scope"])
            for item in daemon_client.principles("confirmed")} == {
        ("Drive the browser yourself instead of listing steps.", "shop"),
        ("A shop for bike restorers that feels like a workshop.", "shop"),
    }
    [workflow] = daemon_client.workflows()
    assert (workflow["name"], workflow["scope"]) == ("a landing page", GLOBAL_SCOPE)
    assert [step["step"] if isinstance(step, dict) else step
            for step in workflow["steps"]] == ["colour", "logo", "copy"]


def test_a_kept_decision_is_an_answer_ask_gives_and_replaces_the_earlier_one(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client: ResolutionClient
) -> None:
    from varyz.review_web import ReviewSession

    caller, device_id = _owner(tmp_path)
    earlier = daemon_client.remember(question=HOSTED, answer="Netlify.", caller=caller,
                                     device_id=device_id, scope="shop")
    evidence = [{"entry_id": "a", "quote": "cloudflare pages", "occurred": "2026-09-05"}]
    queue_items([
        ProfileItem("decision", "", "shop", evidence=evidence, why="It is free.",
                    question=HOSTED, answer="Cloudflare Pages."),
        ProfileItem("decision", "", "shop", evidence=evidence,
                    question="Which analytics does the shop use?", answer="Plausible."),
    ], lambda **fields: daemon_client.add_principle(
        caller=caller, device_id=device_id, **fields), SOURCES["agent"])

    session = ReviewSession(daemon_client, caller, device_id)
    views = {view["decision"]["question"]: view for view in session.items()["items"]}
    hosted = views[HOSTED]
    assert (hosted["slot"], hosted["why"]) == ("decision", "It is free.")
    assert hosted["decision"] == {"question": HOSTED, "answer": "Cloudflare Pages."}

    decided = session.decide([
        {"id": hosted["id"], "action": "keep", "question": HOSTED,
         "answer": "Cloudflare  Pages, on bikes.shop.", "statement": "ignored"},
        {"id": views["Which analytics does the shop use?"]["id"], "action": "drop"},
    ])
    assert decided["errors"] == {}
    assert daemon_client.principles("candidate") == ()
    assert daemon_client.principles("confirmed") == ()
    [answer] = daemon_client.knowledge()
    assert (answer["question"], answer["answer"], answer["scope"]) == (
        HOSTED, "Cloudflare Pages, on bikes.shop.", "shop"
    )
    assert answer["id"] != earlier


def test_cli_review_edits_a_decisions_answer(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli

    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    caller, device_id = _owner(tmp_path)
    evidence = [{"entry_id": "a", "quote": "cloudflare pages", "occurred": "2026-09-05"}]
    queue_items([ProfileItem("decision", "", "shop", evidence=evidence,
                             question=HOSTED, answer="Cloudflare Pages.")],
                lambda **fields: daemon_client.add_principle(
                    caller=caller, device_id=device_id, **fields), SOURCES["agent"])

    reviewed = CliRunner().invoke(
        cli.app, ["rules", "review", "--terminal", "--config", str(config)],
        input="e\nCloudflare Pages, on bikes.shop.\nshop\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    [answer] = daemon_client.knowledge()
    assert (answer["question"], answer["answer"], answer["scope"]) == (
        HOSTED, "Cloudflare Pages, on bikes.shop.", "shop"
    )
    assert daemon_client.principles("candidate") == ()


def test_a_folders_version_is_not_a_project_of_its_own(tmp_path: Path) -> None:
    assert _turn_scope("Conversation: Claude Code — SAN7.14\n\nKeep it short.") == (
        "san7", "Keep it short.")
    path = tmp_path / "p.json"
    path.write_text(json.dumps({
        "version": PROFILE_VERSION, "read": {}, "dirty": ["san7.14", "san7.15"],
        "notes": [
            {"n": 1, "project": "san7.14", "evidence": [{"project": "san7.14"}]},
            {"n": 2, "project": GLOBAL_SCOPE, "from_project": "san7.15", "evidence": []},
        ],
        "tasks": {"t": {"kind": "merge", "project": "san7.15", "notes": [1]}},
    }), encoding="utf-8")

    state = ProfileState(path)

    assert [note["project"] for note in state.notes] == ["san7", GLOBAL_SCOPE]
    assert state.notes[0]["evidence"][0]["project"] == "san7"
    assert state.notes[1]["from_project"] == "san7"
    assert state.tasks["t"]["project"] == "san7"
    assert state.dirty == {"san7"}


# -- a conversation, a moment, and what a rule applies to ----------------------

SPIN = "I want it to spin randomly in all directions, not only left"


def test_a_read_shows_each_conversation_and_what_the_agent_had_just_said(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "logo", "make the logo spin while it loads", day=1, thread="ai:c:one")
    _entry(conn, "spin", SPIN, day=2, thread="ai:c:one",
           said="Done: the logo now spins to the left while the page loads.")
    _entry(conn, "aim", AIM, day=3, thread="ai:c:two")
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    assert read.text == (
        "Project: shop\n\nConversation 1\n\n"
        "Message 1 (2026-09-01):\nmake the logo spin while it loads\n\n"
        'Message 2 (2026-09-02), after the agent said: "Done: the logo now spins to the '
        f'left while the page loads.":\n{SPIN}\n\n'
        f"Conversation 2\n\nMessage 3 (2026-09-03):\n{AIM}"
    )
    # The agent's words are context: they back nothing.
    accepted = accept(state, conn, read.id, [
        _item("rule", "Make loading animations spin to the left.", (2, "spins to the left")),
    ], tier="local")
    assert (accepted.notes, accepted.dropped) == (0, 1)


def test_asking_twice_in_one_conversation_is_one_piece_of_work_not_a_habit(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "spin1", SPIN, day=1, thread="ai:c:one")
    _entry(conn, "spin2", "still only left. spin randomly in all directions", day=2,
           thread="ai:c:one")
    _entry(conn, "tests1", "run the tests before you say it works", day=3, thread="ai:c:two")
    _entry(conn, "tests2", "you did not run the tests before you said it works", day=4,
           thread="ai:c:three")
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", "Make animations spin randomly in all directions.",
              (1, "spin randomly in all directions"), (2, "spin randomly in all directions")),
        _item("rule", "Run the tests before saying it works.",
              (3, "run the tests before"), (4, "run the tests before")),
    ], tier="local")
    merge = next_task(state, conn, tier="local", reader="local")
    accepted = accept(state, conn, merge.id, [
        _merged("rule", "Make animations spin randomly in all directions.", 1),
        _merged("rule", "Run the tests before saying it works.", 2),
    ], tier="local")
    # Both name nothing of the shop, so both go on to the merge across projects:
    # another project may say the same thing once more. That merge counts.
    assert [note["statement"] for note in state.notes if note.get("from_project")] == [
        "Make animations spin randomly in all directions.",
        "Run the tests before saying it works."]
    assert (accepted.items, accepted.dropped) == ([], 0)
    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == GLOBAL_SCOPE
    accepted = accept(state, conn, merge.id, [
        _merged("rule", "Make animations spin randomly in all directions.", 1),
        _merged("rule", "Run the tests before saying it works.", 2),
    ], tier="local")
    assert [item.statement for item in accepted.items] == [
        "Run the tests before saying it works."]
    assert accepted.dropped == 1


CLICK = "Click through the steps yourself instead of listing them for the owner."


def test_said_once_in_one_reading_and_again_in_a_later_one_is_kept(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Measured on 2026-10-08: the owner repeated a third of their corrections
    because what they said once was gone when they said it again. Each reading
    saw it once; only notes kept across readings see it twice."""
    _entry(conn, "click1", "please do the clicking yourself, dont list the steps", day=1,
           thread="ai:c:one")
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", CLICK, (1, "do the clicking yourself"), applies="work")], tier="local")
    for project in ("shop", GLOBAL_SCOPE):
        merge = next_task(state, conn, tier="local", reader="local")
        assert merge.project == project
        accepted = accept(state, conn, merge.id, [_merged("rule", CLICK, 1, applies="work")],
                          tier="local")
        # Once is not yet a habit: nothing to queue.
        assert accepted.items == []
    assert next_task(state, conn, tier="local", reader="local") is None

    # Days later, in another project's conversation, the owner says it again.
    _entry(conn, "click2", "you listed steps again, click it yourself", day=9,
           project="blog", thread="ai:c:two")
    read = next_task(state, conn, tier="local", reader="local")
    assert read.project == "blog" and "Message 1" in read.text and "click2" not in read.text
    accept(state, conn, read.id, [
        _item("rule", CLICK, (1, "click it yourself"), applies="work")], tier="local")
    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == "blog"
    accept(state, conn, merge.id, [_merged("rule", CLICK, 1, applies="work")], tier="local")
    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == GLOBAL_SCOPE
    assert "(1 messages, latest 2026-09-01)" in merge.text
    assert "(1 messages, latest 2026-09-09)" in merge.text
    accepted = accept(state, conn, merge.id, [_merged("rule", CLICK, 1, 2, applies="work")],
                      tier="local")
    [item] = accepted.items
    assert (item.scope, item.messages) == (GLOBAL_SCOPE, 2)
    # Two conversations: strong enough to go into effect on trial.
    assert item.principle(SOURCES["agent"])["provenance"]["threads"] == 2


@pytest.mark.parametrize("text", [
    "don't push right now", "skip the tests for now", "this time use the old logo",
    "leave it as it is for the moment",
])
def test_what_held_for_a_moment_is_no_rule(
    conn: sqlite3.Connection, tmp_path: Path, text: str
) -> None:
    from varyz.resolution.profile_reader import momentary

    assert momentary(text)
    _entry(conn, "one", text, day=1)
    _entry(conn, "two", text + " please", day=2)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    quote = " ".join(text.split()[:3])
    accepted = accept(state, conn, read.id, [
        _item("rule", "Hold off on this until the owner says so.", (1, quote), (2, quote)),
    ], tier="local")
    assert (accepted.notes, accepted.dropped) == (0, 1)


def test_how_the_owner_works_holds_everywhere_and_a_project_rule_stays_there(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "b1", "drive the browser yourself, do not list steps for me", day=1)
    _entry(conn, "b2", "again, drive the browser yourself", day=2)
    _entry(conn, "h1", "the shop header is too short, make it taller", day=3)
    _entry(conn, "h2", "make the shop header taller again", day=4)
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    accept(state, conn, read.id, [
        _item("rule", "Drive the browser yourself instead of listing steps.",
              (1, "drive the browser yourself"), (2, "drive the browser yourself"),
              applies="work"),
        _item("rule", "Keep the shop header tall.", (3, "make it taller"),
              (4, "make the shop header taller"), applies="project"),
    ], tier="local")
    merge = next_task(state, conn, tier="local", reader="local")
    accepted = accept(state, conn, merge.id, [
        _merged("rule", "Drive the browser yourself instead of listing steps.", 1),
        _merged("rule", "Keep the shop header tall.", 2, applies="work"),
    ], tier="local")
    # What the notes said wins over the merge: the header stays in the shop.
    [header] = accepted.items
    assert (header.statement, header.scope, header.applies_to) == (
        "Keep the shop header tall.", "shop", "project")
    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == GLOBAL_SCOPE
    [browser] = accept(state, conn, merge.id, [
        _merged("rule", "Drive the browser yourself instead of listing steps.", 1)
    ], tier="local").items
    assert (browser.scope, browser.applies_to) == (GLOBAL_SCOPE, "work")
    assert browser.principle(SOURCES["agent"])["provenance"]["applies_to"] == "work"


@pytest.mark.parametrize(("blog", "statement", "specifics", "scope", "applies"), [
    (False, "Use thin fonts, never bold.", [], GLOBAL_SCOPE, "products"),
    (True, "Use thin fonts, never bold.", [], GLOBAL_SCOPE, "products"),
    # From a reader that names no project things, the project's name is one.
    (False, "Use thin fonts in the shop, never bold.", None, "shop", "project"),
    (False, "Use thin fonts on the checkout page, never bold.", ["checkout page"], "shop",
     "project"),
    # Words the reader calls the project's that the statement does not hold are none.
    (False, "Use thin fonts, never bold.", ["checkout page"], GLOBAL_SCOPE, "products"),
])
def test_taste_said_in_one_project_holds_everywhere_unless_it_names_that_projects_things(
    conn: sqlite3.Connection, tmp_path: Path, blog: bool, statement: str,
    specifics: list[str] | None, scope: str, applies: str,
) -> None:
    _entry(conn, "f1", "use thin fonts here, never bold", day=1)
    _entry(conn, "f2", "again, use thin fonts not bold", day=2)
    if blog:
        _entry(conn, "g1", "use thin fonts on this one as well", day=3, project="blog")
        _entry(conn, "g2", "use thin fonts in the blog too", day=4, project="blog")
    named = {} if specifics is None else {"specifics": specifics}
    state = ProfileState(tmp_path / "p.json")
    for task in iter(lambda: next_task(state, conn, tier="local", reader="local"), None):
        if task.kind == "read":
            # The reader files it under the project it was said in.
            items = [_item("rule", statement, (1, "use thin fonts"), (2, "use thin fonts"),
                           **named)]
        else:
            items = [_merged("rule", statement, *range(1, task.text.count("messages") + 1))]
        accepted = accept(state, conn, task.id, items, tier="local")
    [fonts] = accepted.items
    assert (fonts.scope, fonts.applies_to) == (scope, applies)


def test_one_correction_is_learned_with_what_the_agent_did_and_what_the_owner_wanted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _entry(conn, "dash", "you put dashes in the copy again, use plain commas", day=1,
           project="website", said="Done: the hero copy is live, with a short tagline.")
    _entry(conn, "spin", "make the logo spin a bit slower please", day=2, project="website")
    state = ProfileState(tmp_path / "p.json")
    read = next_task(state, conn, tier="local", reader="local")
    # The agent's words before the message are context for the correction.
    assert 'after the agent said: "Done: the hero copy is live' in read.text
    accept(state, conn, read.id, [
        _item("rule", "Never use em-dashes in copy; use plain commas.",
              (1, "use plain commas"), agent_did="wrote copy with em-dashes",
              wanted="plain commas, no em-dashes", specifics=[]),
        # One message and no correction behind it: no rule yet.
        _item("rule", "Keep animations slow.", (2, "spin a bit slower")),
    ], tier="local")
    [note] = state.notes
    # Said on the website, naming nothing of it: kept for every project.
    assert (note["applies_to"], note["agent_did"]) == ("products", "wrote copy with em-dashes")

    merge = next_task(state, conn, tier="local", reader="local")
    assert "[rule · products · correction]" in merge.text
    assert accept(state, conn, merge.id, [
        _merged("rule", "Never use em-dashes in copy; use plain commas.", 1)
    ], tier="local").items == []
    merge = next_task(state, conn, tier="local", reader="local")
    assert merge.project == GLOBAL_SCOPE
    [rule] = accept(state, conn, merge.id, [
        _merged("rule", "Never use em-dashes in copy; use plain commas.", 1)
    ], tier="local").items
    assert rule.scope == GLOBAL_SCOPE
    provenance = rule.principle(SOURCES["local"])["provenance"]
    assert provenance["corrections"] == [{
        "agent_did": "wrote copy with em-dashes", "wanted": "plain commas, no em-dashes",
        "entry_id": "dash", "quote": "use plain commas",
        "occurred": "2026-09-01T00:00:00+00:00",
    }]


def test_the_reader_is_told_corrections_come_first_and_what_makes_an_item_a_projects():
    from varyz.resolution.profile_reader import MERGE_INSTRUCTIONS, READ_INSTRUCTIONS, SCHEMA

    assert "Corrections come first" in READ_INSTRUCTIONS
    assert "Corrections come first" in MERGE_INSTRUCTIONS
    assert "never use em-dashes" in READ_INSTRUCTIONS
    # What one reading sees once is noted, so a later reading can see it twice.
    assert "Give an item one conversation shows too" in READ_INSTRUCTIONS
    assert "A note one conversation shows is an item of its own" in MERGE_INSTRUCTIONS
    fields = SCHEMA["properties"]["items"]["items"]["properties"]
    assert {"agent_did", "wanted", "specifics"} <= set(fields)
    for field_name in ("agent_did", "wanted", "specifics"):
        assert f'"{field_name}"' in READ_INSTRUCTIONS
    assert PROFILE_VERSION >= 4


@pytest.mark.parametrize("tier", ["local", "cloud"])
def test_both_readers_are_told_and_keep_the_owners_kinds_of_work(
    conn: sqlite3.Connection, tmp_path: Path, tier: str
) -> None:
    """The cloud model reads AI chats, the local one everything; both file by kind."""
    from varyz.resolution.models import Caller
    from varyz.resolution.principles import SQLitePrincipleStore

    owner = Caller(agent_id="owner:local", display_name="Local owner",
                   purpose="owner_knowledge", device_id="device:test", transport="stdio")
    SQLitePrincipleStore(conn).add(
        statement="Only plants that are safe for cats.", caller=owner,
        device_id="device:test", source="manual", domain="gardening",
    )
    _entry(conn, "soil1", "use peat-free compost for the balcony pots", day=1)
    _entry(conn, "soil2", "again, peat-free compost only", day=2)
    systems: list[str] = []

    class Recording(FakeModel):
        def complete(self, system: str, user: str) -> str:
            systems.append(system)
            return super().complete(system, user)

    model = Recording({
        "Message 1": _reply(_item("done", "Use peat-free compost in pots.",
                                  (1, "peat-free compost"), (2, "peat-free compost"),
                                  applies="products", domain="gardening")),
        "Notes:": _reply(_merged("done", "Use peat-free compost in pots.", 1,
                                 applies="products", domain="gardening")),
    })
    items = read_with_model(ProfileState(tmp_path / "p.json"), conn, model,
                            tier=tier, reader=tier)
    assert "gardening" in systems[0]
    assert [(item.slot, item.domain) for item in items] == [("done", "gardening")]


def test_everyday_and_the_owners_own_kinds_of_work_are_read_merged_and_queued(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from varyz.resolution.models import Caller
    from varyz.resolution.principles import SQLitePrincipleStore
    from varyz.resolution.profile_reader import READ_INSTRUCTIONS, read_instructions

    assert "travel" in READ_INSTRUCTIONS and "cooking" in READ_INSTRUCTIONS
    assert "{kinds}" not in READ_INSTRUCTIONS
    assert "gardening" in read_instructions(("website", "gardening"))
    owner = Caller(agent_id="owner:local", display_name="Local owner",
                   purpose="owner_knowledge", device_id="device:test", transport="stdio")
    SQLitePrincipleStore(conn).add(
        statement="Only plants that are safe for cats.", caller=owner,
        device_id="device:test", source="manual", domain="gardening",
    )
    _entry(conn, "seat1", "book me an aisle seat, I always want the aisle", day=1)
    _entry(conn, "seat2", "aisle seat again please, not the window", day=2)
    _entry(conn, "soil1", "use peat-free compost for the balcony pots", day=3)
    _entry(conn, "soil2", "again, peat-free compost only", day=4)
    state = ProfileState(tmp_path / "p.json")

    read = next_task(state, conn, tier="local", reader="local")
    # The read names the owner's own kind of work, so a model can file under it.
    assert "gardening" in read.instructions and "travel" in read.instructions
    accept(state, conn, read.id, [
        _item("done", "Book an aisle seat.", (1, "aisle seat"), (2, "aisle seat"),
              applies="products", domain="travel"),
        _item("done", "Use peat-free compost in pots.", (3, "peat-free compost"),
              (4, "peat-free compost"), applies="products", domain="Gardening"),
        _item("done", "Bring the pots in before frost.", (3, "balcony pots"),
              (4, "peat-free compost"), applies="products", domain="knitting"),
    ], tier="local")
    assert [note["domain"] for note in state.notes] == ["travel", "gardening", ""]

    items: list[ProfileItem] = []
    while (merge := next_task(state, conn, tier="local", reader="local")) is not None:
        items += accept(state, conn, merge.id, [
            _merged("done", "Book an aisle seat.", 1, applies="products", domain="travel"),
            _merged("done", "Use peat-free compost in pots.", 2, applies="products",
                    domain="gardening"),
        ], tier="local").items
    kept = {item.statement: item.domain for item in items}
    assert kept["Book an aisle seat."] == "travel"
    assert kept["Use peat-free compost in pots."] == "gardening"
    queued = [item.principle(SOURCES["local"]) for item in items]
    assert {"travel", "gardening"} <= {fields["domain"] for fields in queued}


def test_the_review_page_shows_a_correction_first_with_what_the_agent_did(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    from varyz.review_web import ReviewSession

    caller, device_id = _owner(tmp_path)
    evidence = [{"entry_id": "a", "quote": "use plain commas", "occurred": "2026-09-01"},
                {"entry_id": "b", "quote": "plain commas please", "occurred": "2026-09-02"}]
    corrected = ProfileItem(
        "rule", "Never use em-dashes in copy.", GLOBAL_SCOPE, evidence=evidence[:1],
        applies_to="products", corrections=[{
            "agent_did": "wrote copy with em-dashes", "wanted": "plain commas",
            "entry_id": "a", "quote": "use plain commas", "occurred": "2026-09-01"}])
    queue_items([
        ProfileItem("rule", THIN, GLOBAL_SCOPE, evidence=evidence, applies_to="products"),
        corrected,
    ], lambda **fields: daemon_client.add_principle(
        caller=caller, device_id=device_id, **fields), SOURCES["agent"])

    views = ReviewSession(daemon_client, caller, device_id).items()["items"]
    assert [view["statement"] for view in views] == [corrected.statement, THIN]
    assert views[0]["correction"] == {"agent_did": "wrote copy with em-dashes",
                                      "wanted": "plain commas"}
    assert views[1]["correction"] is None


def test_candidates_an_older_reading_queued_are_replaced_and_may_come_back(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    from varyz.resolution.profile_reader import REPLACED, outdated_candidates, replace_outdated

    caller, device_id = _owner(tmp_path)
    typed = "i said use thin fonts on the shop headings"
    old = daemon_client.add_principle(
        caller=caller, device_id=device_id, statement=typed, scope="shop",
        source="mined:ai_conversations", status="candidate")
    kept = daemon_client.add_principle(
        caller=caller, device_id=device_id, statement="Keep the copy short.", scope="shop",
        source="mined:ai_conversations")
    evidence = [{"entry_id": "a", "quote": "drive the browser", "occurred": "2026-09-01"}]
    current = ProfileItem("rule", "Drive the browser yourself instead of listing steps.",
                          GLOBAL_SCOPE, evidence=evidence, applies_to="work")
    queue_items([current], lambda **fields: daemon_client.add_principle(
        caller=caller, device_id=device_id, **fields), SOURCES["agent"])
    assert outdated_candidates(daemon_client.principles("candidate")) == [old]

    assert replace_outdated(daemon_client, caller, device_id) == 1
    assert [item["statement"] for item in daemon_client.principles("candidate")] == [
        current.statement]
    # What the owner kept is never touched.
    assert [item["id"] for item in daemon_client.principles("confirmed")] == [kept]
    # Withdrawn as replaced, not refused by the owner: the wording may be queued
    # again, and it does not count as rejected.
    assert daemon_client.add_principle(
        caller=caller, device_id=device_id, statement=typed, scope="shop",
        source=SOURCES["agent"], status="candidate")
    assert REPLACED == "replaced by a newer reading of your turns"



def test_the_review_page_separates_how_you_work_what_you_build_and_each_project(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    from varyz.review_web import ReviewSession

    caller, device_id = _owner(tmp_path)
    evidence = [{"entry_id": "a", "quote": "use thin fonts", "occurred": "2026-09-01"},
                {"entry_id": "b", "quote": "thin fonts please", "occurred": "2026-09-02"}]
    browser = "Drive the browser yourself instead of listing steps."
    header = "Keep the shop header tall."
    queue_items([
        ProfileItem("rule", header, "shop", evidence=evidence),
        ProfileItem("rule", THIN, GLOBAL_SCOPE, evidence=evidence, applies_to="products"),
        ProfileItem("rule", browser, GLOBAL_SCOPE, evidence=evidence, applies_to="work"),
    ], lambda **fields: daemon_client.add_principle(
        caller=caller, device_id=device_id, **fields), SOURCES["agent"])

    session = ReviewSession(daemon_client, caller, device_id)
    views = session.items()["items"]
    assert [(view["applies_to"], view["scope"], view["statement"]) for view in views] == [
        ("work", GLOBAL_SCOPE, browser),
        ("products", GLOBAL_SCOPE, THIN),
        ("project", "shop", header),
    ]
    by_statement = {view["statement"]: view for view in views}
    decided = session.decide([
        # The owner moves the header to everything they build, and says thin
        # fonts are the shop's alone.
        {"id": by_statement[header]["id"], "action": "keep", "scope": GLOBAL_SCOPE,
         "applies_to": "products"},
        {"id": by_statement[THIN]["id"], "action": "keep", "scope": "shop",
         "applies_to": "products"},
        {"id": by_statement[browser]["id"], "action": "keep"},
    ])
    assert decided["errors"] == {}
    kept = {item["statement"]: item for item in daemon_client.principles("confirmed")}
    assert (kept[header]["scope"], kept[header]["provenance"]["applies_to"]) == (
        GLOBAL_SCOPE, "products")
    assert (kept[THIN]["scope"], kept[THIN]["provenance"]["applies_to"]) == ("shop", "project")
    assert kept[browser]["provenance"]["applies_to"] == "work"


def test_a_kept_rule_set_aside_for_a_new_reading_may_be_suggested_again(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    from varyz.resolution.profile_reader import REPLACED

    caller, device_id = _owner(tmp_path)
    suggested = daemon_client.add_principle(
        caller=caller, device_id=device_id, statement=THIN, scope="shop",
        source=SOURCES["agent"], status="candidate")
    kept = daemon_client.confirm_principle(suggested, caller=caller, device_id=device_id)
    daemon_client.retract_principle(kept, caller=caller, device_id=device_id, reason=REPLACED)
    assert daemon_client.add_principle(
        caller=caller, device_id=device_id, statement=THIN, scope="shop",
        source=SOURCES["agent"], status="candidate")
    # Withdrawn for any other reason, the owner has decided: never asked again.
    rejected = daemon_client.add_principle(
        caller=caller, device_id=device_id, statement="Keep the copy short.",
        scope="shop", source=SOURCES["agent"], status="candidate")
    daemon_client.retract_principle(rejected, caller=caller, device_id=device_id,
                                    reason="not me")
    assert daemon_client.add_principle(
        caller=caller, device_id=device_id, statement="Keep the copy short.",
        scope="shop", source=SOURCES["agent"], status="candidate") is None
