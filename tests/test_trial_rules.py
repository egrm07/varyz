"""Trial rules: strong mined rules go into effect before the owner reads them.

The owner should not have to read every rule a reading of their turns found.
A candidate with strong evidence goes into effect on trial, labelled as such;
the rest wait. Trials end by promotion, a spot-check, a correction or an undo,
and a poor spot-check pauses the lane.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from varyz.db.connection import open_db
from varyz.resolution.briefing import render_markdown, shown_rule_ids, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.trials import (
    TRIAL_DROPPED,
    TRIAL_UNDONE,
    TrialRules,
    TrialSettings,
    assess,
    off_path,
    task_key,
)
from varyz.review_web import ReviewSession, is_trial

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"
START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
# Unlike each other, so none is held back as close to another in effect.
DISTINCT = (
    "Write the report numbers first.",
    "Use pathlib for file paths.",
    "Keep commit messages dense.",
    "Name tests after the behaviour.",
    "Smoke-test a few steps before long training.",
)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(conn: sqlite3.Connection, clock: Clock) -> SQLitePrincipleStore:
    return SQLitePrincipleStore(conn, clock=clock)


@pytest.fixture
def trials(conn: sqlite3.Connection, store: SQLitePrincipleStore, clock: Clock,
           tmp_path: Path) -> TrialRules:
    return TrialRules(conn, store, settings=TrialSettings(), clock=clock,
                      off=off_path(tmp_path / "data" / "varyz.db"))


def _provenance(threads: int = 2, *, slot: str = "rule", moment: bool = False,
                corrections: int = 0, projects: tuple[str, ...] = ()) -> dict[str, Any]:
    quotes = [{"entry_id": f"e{n}", "thread": f"t{n}", "quote": f"said it {n}",
               "moment": moment, "project": projects[n] if n < len(projects) else None}
              for n in range(threads)]
    provenance: dict[str, Any] = {"slot": slot, "quotes": quotes, "messages": threads,
                                  "threads": threads}
    if corrections:
        provenance["corrections"] = [{"agent_did": "x", "wanted": "y"}] * corrections
    return provenance


def _propose(store: SQLitePrincipleStore, statement: str, *, source: str = "model:profile",
             scope: str = "*", **evidence: Any) -> str:
    candidate = store.propose(statement=statement, caller=OWNER, device_id=DEVICE,
                              source=source, scope=scope, provenance=_provenance(**evidence))
    assert candidate is not None
    return candidate


# -- which lane --------------------------------------------------------------


@pytest.mark.parametrize(("statement", "source", "evidence", "lane"), [
    ("Write the report numbers first.", "model:profile", {"threads": 2}, True),
    ("Write the report numbers first.", "cloud:profile", {"threads": 1, "corrections": 1}, True),
    ("Write the report numbers first.", "agent:profile",
     {"threads": 1, "projects": ("alpha",)}, False),
    ("Write the report numbers first.", "model:profile", {"threads": 1}, False),
    ("Write the report numbers first.", "mined:ai_conversations", {"threads": 3}, False),
    ("Write the report numbers first.", "model:profile", {"threads": 3, "slot": "vision"}, False),
    ("Write the report numbers first.", "model:profile", {"threads": 3, "slot": "decision"},
     False),
    ("Write the report numbers first.", "model:profile", {"threads": 3, "moment": True}, False),
    ("Push to main once the tests pass.", "model:profile", {"threads": 3}, False),
    ("Never push to main without asking first.", "model:profile", {"threads": 3}, True),
])
def test_only_strong_model_read_rules_go_on_trial(
    store: SQLitePrincipleStore, statement: str, source: str, evidence: dict[str, Any],
    lane: bool,
) -> None:
    candidate = store.get(_propose(store, statement, source=source, **evidence))
    assert candidate is not None

    verdict = assess(candidate)

    assert verdict.trial is lane
    assert bool(verdict.holds_back) is not lane


def test_said_in_two_projects_counts_as_strong(store: SQLitePrincipleStore) -> None:
    candidate = store.get(_propose(store, "Keep the changelog dated.", threads=1))
    assert candidate is not None
    lifted = candidate.__class__(**{**candidate.__dict__, "provenance": {
        **candidate.provenance, "lifted_from": ["alpha", "beta"]}})

    assert assess(lifted).signals == ["said in 2 projects"]


def test_a_rule_close_to_one_in_effect_waits_for_the_owner(
    store: SQLitePrincipleStore,
) -> None:
    store.add(statement="Always report the numbers first in a report.", caller=OWNER,
              device_id=DEVICE, source="manual")
    candidate = store.get(_propose(store, "Report the numbers first in every report."))
    assert candidate is not None

    verdict = assess(candidate, store.current("confirmed"))

    assert not verdict.trial
    assert any("close to a rule in effect" in reason for reason in verdict.holds_back)


# -- a trial's life ------------------------------------------------------------


def test_exposure_does_not_confirm_a_trial_but_explicit_keep_does(
    conn: sqlite3.Connection, store: SQLitePrincipleStore, trials: TrialRules, clock: Clock,
) -> None:
    candidate = _propose(store, "Write the report numbers first.")
    trial_id = trials.consider(candidate, caller=OWNER, device_id=DEVICE)
    assert trial_id is not None
    assert store.current("candidate") == ()

    briefing = start_task(conn, "write the weekly report", project="alpha")
    assert trial_id in shown_rule_ids(briefing)
    text = render_markdown(briefing)
    assert f"Write the report numbers first. (on trial, id {trial_id})" in text
    assert "remember_owner_rule with replaces" in text

    for n in range(3):  # three tasks on day one: not long enough
        assert trials.record_uses([trial_id], task_key(f"c{n}", "x"), device_id=DEVICE) == []
    assert trials.record_uses([trial_id], task_key("c0", "x"), device_id=DEVICE) == []
    assert trials.uses(trial_id) == 3

    clock.now = START + timedelta(days=8)
    promoted = trials.record_uses([trial_id], task_key("c3", "x"), device_id=DEVICE)

    assert promoted == []
    assert store.get(trial_id).trial
    assert store.get(trial_id).advice()["authority"] == "learned_trial"
    kept = store.end_trial(trial_id, caller=OWNER, device_id=DEVICE, how="kept")
    rule = store.get(kept)
    assert rule is not None and not rule.trial
    assert rule.provenance["trial_ended"]["how"] == "kept"
    assert rule.advice()["authority"] == "owner_confirmed"
    assert trials.status()["promoted"] == 0
    assert "on trial" not in render_markdown(start_task(conn, "write a report", project="a"))


def test_a_trial_rule_not_shown_for_enough_tasks_stays_on_trial(
    store: SQLitePrincipleStore, trials: TrialRules, clock: Clock,
) -> None:
    trial_id = trials.consider(_propose(store, "Write the report numbers first."),
                               caller=OWNER, device_id=DEVICE)
    assert trial_id is not None
    trials.record_uses([trial_id], "conversation:one", device_id=DEVICE)
    clock.now = START + timedelta(days=30)

    assert trials.promote_due(device_id=DEVICE) == []
    assert [rule.id for rule in trials.trials()] == [trial_id]


def test_corrections_spot_checks_and_undo_end_trials_and_are_counted(
    store: SQLitePrincipleStore, trials: TrialRules, clock: Clock,
) -> None:
    ids = [trials.consider(_propose(store, statement), caller=OWNER, device_id=DEVICE)
           for statement in DISTINCT[:4]]
    assert all(ids)
    kept, dropped, corrected, undone = (str(value) for value in ids)

    store.confirm(kept, caller=OWNER, device_id=DEVICE)
    store.retract(dropped, caller=OWNER, device_id=DEVICE, reason=TRIAL_DROPPED)
    store.add(statement="Keep commit messages short.", caller=OWNER, device_id=DEVICE,
              source="manual", supersedes_id=corrected, provenance={"corrects": corrected})
    assert trials.undo(START - timedelta(days=1), caller=OWNER, device_id=DEVICE) == 1

    status = trials.status()
    assert {key: status[key] for key in ("kept", "dropped", "corrected", "undone",
                                         "on_trial")} == {
        "kept": 1, "dropped": 1, "corrected": 1, "undone": 1, "on_trial": 0}
    assert status["kept_share"] == 0.5
    # A rule put on trial was not accepted by the owner: acceptance stays theirs.
    assert store.stats()["accepted"] == 0
    retracted = store._conn.execute(
        "SELECT reason FROM owner_principle_retractions WHERE principle_id = ?", (undone,)
    ).fetchone()[0]
    assert retracted == TRIAL_UNDONE


def test_a_poor_spot_check_pauses_new_trials(
    store: SQLitePrincipleStore, trials: TrialRules,
) -> None:
    ids = [trials.consider(_propose(store, statement), caller=OWNER, device_id=DEVICE)
           for statement in DISTINCT]
    for trial_id in ids[:4]:
        store.confirm(str(trial_id), caller=OWNER, device_id=DEVICE)
    store.retract(str(ids[4]), caller=OWNER, device_id=DEVICE, reason=TRIAL_DROPPED)

    assert trials.paused() is not None and "80%" in str(trials.paused())
    candidate = _propose(store, "Answer in plain words.")
    assert trials.consider(candidate, caller=OWNER, device_id=DEVICE) is None
    assert [rule.id for rule in store.current("candidate")] == [candidate]


def test_the_off_sentinel_and_config_stop_new_trials(
    store: SQLitePrincipleStore, trials: TrialRules, conn: sqlite3.Connection,
) -> None:
    assert trials.off is not None
    trials.off.parent.mkdir(parents=True, exist_ok=True)
    trials.off.write_text("off", encoding="utf-8")
    assert trials.consider(_propose(store, "Write the report numbers first."),
                           caller=OWNER, device_id=DEVICE) is None
    disabled = TrialRules(conn, store, settings=TrialSettings(enabled=False))
    assert "config" in str(disabled.paused())


def test_scan_sorts_the_queue_and_starts_trials_only_when_applied(
    store: SQLitePrincipleStore, trials: TrialRules,
) -> None:
    strong = _propose(store, "Write the report numbers first.")
    weak = _propose(store, "Use tabs in the makefile.", threads=1)

    dry = trials.scan(apply=False, caller=OWNER, device_id=DEVICE)
    assert ([item["id"] for item in dry["trial"]], [item["id"] for item in dry["review"]]) \
        == ([strong], [weak])
    assert dry["started"] == 0 and len(store.current("candidate")) == 2

    assert trials.scan(apply=True, caller=OWNER, device_id=DEVICE)["started"] == 1
    assert [rule.id for rule in store.current("candidate")] == [weak]


# -- through the daemon ----------------------------------------------------------


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


def test_the_daemon_puts_a_queued_strong_candidate_on_trial_and_the_review_samples_it(
    daemon_client: ResolutionClient,
) -> None:
    def queue(statement: str, threads: int) -> str | None:
        return daemon_client.add_principle(
            statement=statement, caller=OWNER, device_id=DEVICE, source="model:profile",
            provenance=_provenance(threads), status="candidate",
        )

    queue("Write the report numbers first.", 2)
    trial_id = daemon_client.last_trial_id
    queue("Use tabs in the makefile.", 1)
    assert trial_id is not None and daemon_client.last_trial_id is None

    status = daemon_client.trial_rules()
    assert status["on_trial"] == 1 and status["spot_check_ids"] == [trial_id]
    assert daemon_client.record_principle_uses(
        [trial_id], task_key="conversation:x", device_id=DEVICE) == []

    session = ReviewSession(daemon_client, OWNER, DEVICE)
    views = session.items()["items"]
    assert [view["trial"] is not None for view in views] == [True, False]
    assert session.decide([{"id": trial_id, "action": "drop"}])["done"] == [trial_id]
    assert daemon_client.trial_rules()["dropped"] == 1
    assert not any(is_trial(item) for item in session.pending())


def test_the_hook_counts_the_task_for_the_trial_rules_it_briefed(
    tmp_path: Path, daemon_client: ResolutionClient,
) -> None:
    from types import SimpleNamespace

    from varyz import hooks

    daemon_client.add_principle(
        statement="Write the report numbers first.", caller=OWNER, device_id=DEVICE,
        source="model:profile", provenance=_provenance(2), status="candidate",
    )
    trial_id = daemon_client.last_trial_id
    assert trial_id is not None
    cfg = SimpleNamespace(db_path=None, resolution=SimpleNamespace(
        bind_host=daemon_client.host, port=daemon_client.port, auth_mode="session",
        device_id=DEVICE))
    briefing = {"trial_rules": "note", "how_owner_works": [{"id": trial_id, "trial": True}]}

    hooks._record_trial_uses(cfg, briefing, "session-1", "write the report")
    hooks._record_trial_uses(cfg, briefing, "session-1", "write the report")  # same task
    hooks._record_trial_uses(cfg, {"how_owner_works": []}, "session-2", "x")  # none shown

    [rule] = daemon_client.trial_rules()["rules"]
    assert rule["id"] == trial_id and rule["tasks"] == 1
