"""Rules on how text reads reach every reply, and a reply that breaks one goes back.

The case behind this: a Codex session asked to "assess security email legitimacy"
answered with em dashes although the owner forbids them, and without the owner's
email style, which was saved for outreach emails only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests import test_workflow as wf
from varyz import hooks
from varyz.resolution import task_size
from varyz.resolution.briefing import FINISH_REMINDER, finish_check, render_markdown, start_task
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.workflows import Sizing
from varyz.resolution.writing_rules import (
    MESSAGE_NOTE,
    TEXT_TITLE,
    breaches,
    forbidden_marks,
    is_message_rule,
    is_text_rule,
    select_writing,
)

D = chr(0x2014)  # the em dash itself
config_path = wf.config_path
db = wf.db

EM_DASH = "Do not use em dashes in anything you write (3 messages, latest 2026-10-08)"
OUTREACH = "Keep creator outreach emails short and honest, not a sales pitch."
TASK = "assess security email legitimacy"


@dataclass
class Rule:
    id: str
    statement: str
    scope: str = "*"
    domain: str = ""
    kind: str = "rule"
    rationale: str = ""
    trial: bool = False


# -- which rules are about text ------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [EM_DASH, "No emoji in commit messages or replies.", "Never use title case in headings.",
     "Use British spelling."],
)
def test_rules_on_how_text_reads_are_text_rules(statement: str) -> None:
    assert is_text_rule(Rule("r", statement))


@pytest.mark.parametrize(
    "statement",
    ["Never create git commits unless I ask.", "Use a dash of colour on the hero.",
     f"Run ruff before reporting {D} always."],  # a dash in the wording is not a rule on dashes
)
def test_other_rules_are_not(statement: str) -> None:
    assert not is_text_rule(Rule("r", statement))


def test_a_rule_for_one_kind_of_email_is_a_rule_on_how_emails_read() -> None:
    assert is_message_rule(Rule("r", OUTREACH))
    assert not is_message_rule(Rule("r", "Archive emails older than a year."))


def test_the_security_email_task_gets_the_text_and_email_rules() -> None:
    rules = [Rule("dash", EM_DASH), Rule("outreach", OUTREACH),
             Rule("commits", "Never create git commits unless I ask."),
             Rule("docs", "No em dashes in the varyz docs.", scope="varyz")]
    found = select_writing(rules, TASK, project="Documents")
    assert [r.id for r in found.text] == ["dash"]
    assert [r.id for r in found.messages] == ["outreach"]
    # Scope still holds: the varyz rule reaches varyz work only.
    assert {r.id for r in select_writing(rules, "fix the checkpoint test", project="varyz").text} \
        == {"dash", "docs"}


def test_coding_work_gets_no_email_rules_unless_the_session_is_about_one() -> None:
    rules = [Rule("dash", EM_DASH), Rule("outreach", OUTREACH)]
    assert select_writing(rules, "fix the flaky checkpoint test").messages == []
    later = select_writing(rules, "should it be kinder?", context=[TASK])
    assert [r.id for r in later.messages] == ["outreach"]


# -- what a finished reply may not use ------------------------------------------


def test_only_owner_rules_that_forbid_a_mark_can_stop_a_reply() -> None:
    assert forbidden_marks([Rule("dash", EM_DASH)]) == {"em dash": "dash"}
    assert forbidden_marks([Rule("dash", EM_DASH, trial=True)]) == {}
    assert forbidden_marks([Rule("ok", "Em dashes are fine in quotes.")]) == {}
    assert forbidden_marks([Rule("e", "Never put an emoji in a reply.")]) == {"emoji": "e"}


def test_marks_count_in_prose_only() -> None:
    marks = {"em dash", "emoji"}
    assert breaches(f"This looks legitimate {D} the sender matches.", marks) == ["em dash"]
    assert breaches("Looks fine ✅", marks) == ["emoji"]
    assert breaches(f"Run `a {D} b`.\n```\nx {D} y\n```\n> they wrote {D} this", marks) == []
    assert breaches("This looks legitimate: the sender matches.", marks) == []


# -- the briefing ------------------------------------------------------------------


def _owner(db: sqlite3.Connection) -> dict[str, str]:
    return {
        "dash": wf._rule(db, EM_DASH),
        "outreach": wf._rule(db, OUTREACH),
        "commits": wf._rule(db, "Never create git commits unless I ask."),
        "wordmark": wf._rule(db, "Use the lowercase wordmark with the period.", scope="persona"),
    }


def test_start_task_shows_the_writing_rules_in_their_own_section(db: sqlite3.Connection) -> None:
    ids = _owner(db)
    briefing = start_task(db, TASK, project="Documents")
    assert [item["id"] for item in briefing["writing_rules"]] == [ids["dash"]]
    assert [item["id"] for item in briefing["message_rules"]] == [ids["outreach"]]
    assert briefing["message_rules_note"] == MESSAGE_NOTE
    ranked = {item["id"] for item in briefing["how_owner_works"]}
    assert ids["dash"] not in ranked and ids["outreach"] not in ranked
    text = render_markdown(briefing)
    assert text.index(TEXT_TITLE) < text.index("How the owner works (always)")


def test_finish_check_holds_the_report_to_the_writing_rules(db: sqlite3.Connection) -> None:
    ids = _owner(db)
    assert ids["dash"] in json.dumps(finish_check(db, TASK, project="Documents"))




# -- the hooks ---------------------------------------------------------------------


def _hook_cfg(config_path: Path, **agent: bool) -> Any:
    cfg = wf._cfg(config_path, **agent)
    cfg.workflows.prompt_classifier = "rule"
    return cfg


def _event(tmp_path: Path) -> dict[str, str]:
    return {"session_id": "w1", "cwd": str(tmp_path / "Documents")}


def test_a_later_follow_up_carries_the_writing_rules(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    ids = _owner(db)
    cfg = _hook_cfg(config_path)
    event = _event(tmp_path)
    hooks._briefing_for_prompt(cfg, {**event, "prompt": "Please " + TASK + " for me"})
    later = hooks._briefing_for_prompt(cfg, {**event, "prompt": "should i be kinder?"})
    text = later["hookSpecificOutput"]["additionalContext"]
    assert ids["dash"] in text and ids["outreach"] in text
    assert ids["wordmark"] not in text


def test_every_later_task_is_reminded_to_run_the_finish_check(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _owner(db)
    cfg = _hook_cfg(config_path)
    event = _event(tmp_path)
    hooks._briefing_for_prompt(cfg, {**event, "prompt": "Please " + TASK + " for me"})
    later = hooks._briefing_for_prompt(
        cfg, {**event, "prompt": "Now draft a short reply email to that sender for me"})
    assert FINISH_REMINDER in later["hookSpecificOutput"]["additionalContext"]


def _claude(path: Path, reply: str) -> str:
    return wf._transcript(
        path,
        {"type": "user", "message": {"role": "user", "content": TASK}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}},
    )


def _codex(path: Path, reply: str) -> str:
    def item(role: str, kind: str, text: str) -> dict[str, Any]:
        return {"type": "response_item", "payload": {
            "type": "message", "role": role, "content": [{"type": kind, "text": text}]}}

    return wf._transcript(path, item("user", "input_text", TASK),
                          item("assistant", "output_text", reply))


DASHED = f"It is legitimate {D} the sender domain matches."


@pytest.mark.parametrize("transcript", [_claude, _codex])
def test_a_reply_with_a_forbidden_mark_goes_back_once(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection, transcript: Any
) -> None:
    ids = _owner(db)
    cfg = _hook_cfg(config_path, done_gate=False, writing_check=True)
    event = {**_event(tmp_path), "transcript_path": transcript(tmp_path / "t.jsonl", DASHED)}
    blocked = hooks.stop(cfg, event)
    assert blocked is not None and blocked["decision"] == "block"
    assert "em dash" in blocked["reason"] and ids["dash"] in blocked["reason"]
    assert hooks.stop(cfg, event) is None  # the same reply goes back once
    assert hooks.stop(cfg, {**event, "stop_hook_active": True}) is None


def test_a_clean_reply_quoted_dash_or_switched_off_check_ends_the_turn(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    _owner(db)
    cfg = _hook_cfg(config_path, done_gate=False, writing_check=True)
    for reply in ("It is legitimate: the sender domain matches.",
                  f"The email says:\n> act now {D} or lose access\nThat is a phishing tell."):
        path = _claude(tmp_path / "t.jsonl", reply)
        assert hooks.stop(cfg, {**_event(tmp_path), "transcript_path": path}) is None
    cfg.agent.writing_check = False
    path = _claude(tmp_path / "off.jsonl", DASHED)
    assert hooks.stop(cfg, {**_event(tmp_path), "transcript_path": path}) is None


def test_a_rule_on_trial_is_shown_but_never_stops_a_reply(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    store = SQLitePrincipleStore(db)
    candidate = store.propose(statement=EM_DASH, caller=wf.OWNER, device_id=wf.DEVICE,
                              source="session_miner")
    assert candidate is not None
    trial = store.start_trial(candidate, caller=wf.OWNER, device_id=wf.DEVICE,
                              signals=["repeated"])
    assert [item["id"] for item in start_task(db, TASK, project="Documents")["writing_rules"]] \
        == [trial]
    cfg = _hook_cfg(config_path, done_gate=False, writing_check=True)
    path = _claude(tmp_path / "t.jsonl", DASHED)
    assert hooks.stop(cfg, {**_event(tmp_path), "transcript_path": path}) is None


def test_the_writing_check_rides_with_the_done_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> None:
    wf._website_owner(db)
    wf._rule(db, EM_DASH)
    cfg = _hook_cfg(config_path, done_gate=True, writing_check=True)
    monkeypatch.setattr(task_size, "gate_sizer_from_config",
                        lambda cfg: lambda task, steps: Sizing("new", "model"))
    event = {"session_id": "g1", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})
    said = {"type": "assistant", "message": {"content": [{"type": "text", "text": DASHED}]}}
    path = wf._transcript(tmp_path / "t.jsonl", wf.PROMPT, wf.EDIT, wf.RESULT, said)
    blocked = hooks.stop(cfg, {**event, "transcript_path": path})
    assert blocked is not None and blocked["decision"] == "block"
    assert blocked["reason"].startswith("varyz writing check")
    assert "favicon" in blocked["reason"]


# -- one rule per prohibition (test Mac report, item 15) ---------------------


def test_a_screen_rule_that_says_capitalize_is_not_a_text_rule() -> None:
    markets = Rule("m", "Match the markets tab show-more text to the activity tab, use a "
                        "thinner font and capitalize titles normally.")
    assert not is_text_rule(markets)
    assert is_text_rule(Rule("c", "Capitalize titles normally, never in all caps."))


def test_restatements_of_one_prohibition_show_once_and_owner_rules_win() -> None:
    rules = [
        Rule("short", "Do not use em dashes."),
        Rule("trial", "Never use em dashes in writing or code comments; replace them "
                      "with commas.", trial=True),
        Rule("copy", "Do not use em dashes in copy or code comments; replace them."),
        Rule("emoji", "Do not use emojis in responses."),
        Rule("narrow", "Acquisition emails must not use em dashes.", scope="lumen"),
    ]
    shown = [rule.id for rule in select_writing(rules, TASK, project="lumen").text]
    # The narrower project rule is never merged into the general one.
    assert shown == ["narrow", "copy", "emoji"]
    # The finished-reply check still knows both marks.
    assert set(forbidden_marks(select_writing(rules, TASK).text)) == {"em dash", "emoji"}
