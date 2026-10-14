"""Live reading and realtime Cloud sync: a rule said in a chat is in effect at once.

No model is called: the reader is the agent's subagent, played here by the test.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_profile_reader import _agent_server, _config, daemon_client  # noqa: F401
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution.profile_reader import (
    LIVE_OFFER_REPEAT_SECONDS,
    LIVE_PROMPT,
    READER_AGENT,
    ProfileState,
    _offer_times,
    agent_reading_offer,
    live_offer,
    live_since,
    next_task,
    profile_path,
    reading_lock,
    reading_taken,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
HEADER = "From: me\nConversation: Claude Code — shop\n\n"
OLD = "the headings are bold again. use thin fonts, never bold ones please"
NEW = "from now on write the report numbers first, before any prose at all"


def _said(conn: sqlite3.Connection, entry_id: str, text: str, when: datetime) -> None:
    stamp = when.isoformat()
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, thread_id, sensitivity, authored_by_user,
               conversation_metadata_json)
           VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, ?, 'personal', 1, '{}')""",
        (entry_id, entry_id, entry_id, HEADER + text, stamp, stamp,
         f"ai:claude_code:{entry_id}"),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return load_config(_config(tmp_path))


# -- what the live reader reads ------------------------------------------------


def test_the_live_reading_reads_only_what_was_said_in_the_last_day(
    conn: sqlite3.Connection, cfg: Any
) -> None:
    _said(conn, "old", OLD, NOW - timedelta(days=3))
    _said(conn, "new", NEW, NOW - timedelta(hours=1))
    state = ProfileState(profile_path(cfg, "cloud"))

    task = next_task(state, conn, tier="cloud", reader="agent",
                     since=live_since(cfg, NOW.timestamp()))

    assert task is not None and "report numbers first" in task.text
    assert "thin fonts" not in task.text


def test_a_standing_rule_is_asked_about_at_once_and_the_rest_wait_their_minutes(
    conn: sqlite3.Connection, cfg: Any
) -> None:
    now = NOW.timestamp()
    _said(conn, "new", NEW, NOW - timedelta(minutes=1))
    reading_taken(cfg, now - 60, live=True)  # a live reader just ran

    assert live_offer(cfg, conn, now, "make the header a little taller please") is None
    offer = live_offer(cfg, conn, now, NEW)
    assert offer is not None and offer["live"] and offer["turns_waiting"] == 1
    assert READER_AGENT in offer["do"] and offer["subagent_prompt"] == LIVE_PROMPT
    # The hook and start_task brief the same prompt: asked once, not twice.
    assert live_offer(cfg, conn, now + 1, NEW) is None
    assert live_offer(cfg, conn, now + LIVE_OFFER_REPEAT_SECONDS + 1, NEW) is not None
    later = now + cfg.workflows.live_reading_min_minutes * 60 + LIVE_OFFER_REPEAT_SECONDS * 3
    assert live_offer(cfg, conn, later, "make the header a little taller please") is not None


def test_a_short_prompt_with_nothing_new_is_not_worth_a_reader(
    conn: sqlite3.Connection, cfg: Any
) -> None:
    assert live_offer(cfg, conn, NOW.timestamp(), "ok thanks") is None
    assert live_offer(cfg, conn, NOW.timestamp(),
                      "do I need to mention varyz in the prompt for the website?") is None
    # A prompt long enough to hold a rule reaches the archive as the reader starts.
    assert live_offer(cfg, conn, NOW.timestamp(), NEW)["turns_waiting"] == 0


def test_the_live_reading_is_off_with_either_switch(conn: sqlite3.Connection, cfg: Any) -> None:
    _said(conn, "new", NEW, NOW - timedelta(minutes=1))
    cfg.workflows.live_reading = False
    assert live_offer(cfg, conn, NOW.timestamp(), NEW) is None
    cfg.workflows.live_reading = True
    cfg.workflows.rule_reader_agent = False
    assert agent_reading_offer(cfg, conn, NOW.timestamp(), prompt=NEW) is None


def test_a_live_reader_does_not_put_off_the_daily_one(
    conn: sqlite3.Connection, cfg: Any
) -> None:
    now = NOW.timestamp()
    _said(conn, "old", OLD, NOW - timedelta(days=3))
    reading_taken(cfg, now, live=True)
    assert "taken_at" not in _offer_times(cfg)
    offer = agent_reading_offer(cfg, conn, now, prompt=NEW)
    assert offer is not None and "live" not in offer  # the full reading, still due
    assert agent_reading_offer(cfg, conn, now, prompt=NEW, daily=False)["live"] is True


def test_two_readers_take_turns_with_the_reading_state(cfg: Any, monkeypatch) -> None:
    from varyz.resolution import profile_reader

    monkeypatch.setattr(profile_reader, "READING_LOCK_SECONDS", 0.2)
    with reading_lock(cfg), pytest.raises(TimeoutError), reading_lock(cfg):
        pass
    with reading_lock(cfg):
        pass


def test_the_agents_live_reader_reads_the_newest_turns_after_reading_the_chats(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client, monkeypatch  # noqa: F811
) -> None:
    from varyz.resolution import live

    now = datetime.now(UTC)
    _said(conn, "old", OLD, now - timedelta(days=3))
    _said(conn, "new", NEW, now - timedelta(minutes=5))
    ingested: list[float] = []
    monkeypatch.setattr(live, "ingest_new_turns",
                        lambda cfg, wait=0.0: ingested.append(wait) or 0)
    server = _agent_server(tmp_path, daemon_client)
    try:
        read = server.call("profile_reading_task", {"recent": True})
        assert read["kind"] == "read" and "report numbers first" in read["text"]
        assert "thin fonts" not in read["text"]
        server.call("submit_profile_reading", {"task_id": read["task_id"], "items": []})
        assert server.call("profile_reading_task", {"recent": True}) == {"done": True}
        assert len(ingested) == 1  # read once a minute, not on every call
        # The backlog is still there for the daily reading.
        assert "thin fonts" in server.call("profile_reading_task", {})["text"]
    finally:
        server.close()
    cfg = load_config(_config(tmp_path))
    assert "live_taken_at" in _offer_times(cfg)


# -- a rule said to hold from now on goes on trial ------------------------------


@pytest.mark.parametrize(("quote", "moment", "standing"), [
    ("from now on write the numbers first", False, True),
    ("write the numbers first in this one", False, False),
    ("from now on write the numbers first", True, False),
])
def test_said_to_hold_from_now_on_puts_a_rule_on_trial(
    conn: sqlite3.Connection, quote: str, moment: bool, standing: bool
) -> None:
    from tests.test_trial_rules import DEVICE, OWNER
    from varyz.resolution.principles import SQLitePrincipleStore
    from varyz.resolution.trials import assess

    store = SQLitePrincipleStore(conn)
    identifier = store.propose(
        statement="Write the report numbers first.", caller=OWNER, device_id=DEVICE,
        source="agent:profile", scope="*",
        provenance={"slot": "rule", "messages": 1, "threads": 1, "quotes": [
            {"entry_id": "e1", "thread": "t1", "quote": quote, "moment": moment}]},
    )
    verdict = assess(store.get(identifier))

    assert ("said to hold from now on" in verdict.signals) is standing
    assert verdict.trial is standing


# -- the archive reads the chats at once -----------------------------------------


def test_a_nudge_reads_the_chats_once_for_prompts_that_come_together(cfg: Any) -> None:
    from varyz.resolution.live import LiveIngest

    reads: list[int] = []
    started = threading.Event()

    def ingest(_cfg: Any) -> int:
        reads.append(1)
        started.set()
        time.sleep(0.1)
        return 1

    live = LiveIngest(cfg, delay=0.05, ingest=ingest)
    assert live.nudge() is True
    assert live.nudge() is False and live.nudge() is False  # folded into one more read
    assert started.wait(2)
    deadline = time.monotonic() + 3
    while live._running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not live._running and len(reads) == 2
    assert live.nudge() is True


def test_the_chats_are_read_only_when_approved_unpaused_and_autosync_is_not_reading(
    cfg: Any, conn: sqlite3.Connection, monkeypatch
) -> None:
    from varyz.autosync import claim_path, paused_path
    from varyz.resolution import live

    monkeypatch.setattr(live, "_ingest", lambda cfg: 3)
    assert live.ingest_new_turns(cfg) is None  # the owner never approved the connector
    cfg.connectors["ai_conversations"] = {"approved": True}
    assert live.ingest_new_turns(cfg) == 3
    for sentinel in (claim_path(cfg), paused_path(cfg)):
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("x", encoding="utf-8")
        assert live.ingest_new_turns(cfg) is None
        sentinel.unlink()


def test_the_daemon_reads_the_chats_when_nudged(daemon_client, cfg: Any) -> None:  # noqa: F811
    assert daemon_client.nudge_live() is False  # no live reading in this daemon


def test_the_daemon_with_live_reading_starts_a_read(tmp_path: Path, cfg: Any) -> None:
    from varyz.resolution.client import ResolutionClient
    from varyz.resolution.daemon import build_resolution_daemon
    from varyz.resolution.escalation import NullNotifier
    from varyz.resolution.live import LiveIngest

    reads = threading.Event()
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
        device_id="device:test", notifier=NullNotifier(),
    )
    daemon.live = LiveIngest(cfg, delay=0.0, ingest=lambda _cfg: reads.set())
    try:
        with daemon:
            host, port = daemon.address
            assert ResolutionClient(host, port).nudge_live() is True
            assert reads.wait(2)
    finally:
        daemon.stop()


def test_a_prompt_without_a_briefing_still_gets_the_live_reader(
    cfg: Any, conn: sqlite3.Connection
) -> None:
    from varyz import hooks

    _said(conn, "new", NEW, datetime.now(UTC) - timedelta(minutes=1))
    text = hooks._live_job_text(cfg, NEW)
    assert "background job" in text and READER_AGENT in text
    assert hooks._live_job_text(cfg, NEW) == ""  # asked once for the same prompt


# -- realtime Cloud sync ------------------------------------------------------------


def _cloud(tmp_path: Path, **settings: Any):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from varyz.cloud.app import Settings, create_app, password_hash

    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite"),
                              email_sender=lambda message: None, **settings))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) VALUES(?,?,?,?,1)",
            ("alice", "alice@example.test", password_hash("correct-password"), "active"),
        )
    headers = {"Authorization": "Bearer " + store.credential(
        "alice", "access", 3600, "varyz-local")}
    return TestClient(app, base_url="http://127.0.0.1:8787"), headers


RULE = {"kind": "rule", "id": "one", "base_version": 0,
        "payload": {"statement": "Use plain words", "scope": "*"}}


def test_a_device_waiting_on_the_cloud_hears_another_devices_change_at_once(
    tmp_path: Path,
) -> None:
    client, headers = _cloud(tmp_path)
    answer: dict[str, Any] = {}

    def wait() -> None:
        started = time.monotonic()
        answer.update(client.get("/api/sync", headers=headers,
                                 params={"cursor": 0, "wait": 20}).json())
        answer["took"] = time.monotonic() - started

    waiting = threading.Thread(target=wait)
    waiting.start()
    time.sleep(0.5)
    assert client.post("/api/sync", headers=headers, json={"records": [RULE]}).status_code == 200
    waiting.join(25)

    assert answer["waited"] is True and answer["took"] < 10
    assert [record["id"] for record in answer["records"]] == ["one"]
    # Records already there are answered without waiting.
    assert "waited" not in client.get("/api/sync", headers=headers,
                                      params={"cursor": 0, "wait": 10}).json()
    assert client.get("/api/sync", headers=headers, params={"wait": 26}).status_code == 422


def test_a_full_cloud_answers_at_once_and_the_device_polls(tmp_path: Path) -> None:
    client, headers = _cloud(tmp_path, sync_waiters=0)
    started = time.monotonic()
    page = client.get("/api/sync", headers=headers, params={"cursor": 0, "wait": 20}).json()
    assert page["records"] == [] and "waited" not in page
    assert time.monotonic() - started < 10


class FakeCloud:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.pulls: list[tuple[int, int]] = []

    def pull(self, cursor: int, wait: int = 0) -> dict[str, Any]:
        self.pulls.append((cursor, wait))
        if self.pages:
            return self.pages.pop(0)
        time.sleep(0.05)
        return {"records": [], "waited": True}

    def close(self) -> None:
        pass


def _live_sync(tmp_path: Path, cloud: FakeCloud, monkeypatch):
    from varyz.cloud import live_sync

    monkeypatch.setattr(live_sync, "WATCH_SECONDS", 0.05)
    config = _config(tmp_path)
    syncing = live_sync.LiveCloudSync(config, tmp_path / "data" / "varyz.db",
                                      client=lambda path: cloud)
    calls: list[bool] = []
    monkeypatch.setattr(syncing, "_sync",
                        lambda conn, only_if_planned: calls.append(only_if_planned) or True)
    return syncing, calls


def _until(check, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.02)
    return check()


def test_a_change_here_is_sent_as_soon_as_it_is_committed(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch
) -> None:
    syncing, calls = _live_sync(tmp_path, FakeCloud([]), monkeypatch)
    syncing.start()
    try:
        time.sleep(0.2)
        assert calls == []
        _said(conn, "new", NEW, NOW)  # another connection commits
        assert _until(lambda: True in calls)
    finally:
        syncing.stop()


def test_a_change_on_another_device_is_pulled_as_soon_as_the_cloud_has_it(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch
) -> None:
    cloud = FakeCloud([{"records": [RULE], "next_cursor": 1}])
    syncing, calls = _live_sync(tmp_path, cloud, monkeypatch)
    journal = tmp_path / "config.cloud-sync.json"
    journal.write_text(json.dumps({"cursor": 7, "records": {}, "enabled": True}),
                       encoding="utf-8")
    syncing.start()
    try:
        assert _until(lambda: False in calls)  # woken: syncs whatever the plan says
        assert cloud.pulls[0] == (7, 25)
    finally:
        syncing.stop()
