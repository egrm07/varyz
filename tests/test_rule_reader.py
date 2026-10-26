"""How the owner's turns reach a model: which turns are theirs, and which model reads.

What the models find in them is tested in test_profile_reader.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from varyz.config import Config, WorkflowsConfig
from varyz.db.connection import open_db
from varyz.resolution.local_classifier import machine_fit
from varyz.resolution.rule_miner import MiningStats
from varyz.resolution.rule_reader import owner_turns, reading_order

HEADER = "From: me\nConversation: Claude Code \u2014 shop\n\n"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


def _entry(conn, entry_id, text, *, thread="ai:claude_code:s1"):
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, thread_id, sensitivity, authored_by_user)
           VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, ?, 'personal', 1)""",
        (entry_id, entry_id, entry_id, HEADER + text,
         datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
         datetime(2026, 9, 1, tzinfo=UTC).isoformat(), thread),
    )
    conn.commit()


# -- the machine ---------------------------------------------------------------

GB = 1024
QWEN_4B = int(3.4 * 1024**3)


def test_an_8gb_mac_is_tight_and_a_16gb_one_fits() -> None:
    def fit(ram, disk=50 * GB, gpu=None):
        return machine_fit(QWEN_4B, free_disk=lambda: disk, gpu_memory=lambda: gpu,
                           ram=lambda: ram)[0]

    assert fit(8 * GB) == "tight"
    assert fit(16 * GB) == "fits"
    assert fit(4 * GB) == "no"
    # A full disk is what restarted the 8 GB Mac: too little, and it does not run.
    assert fit(16 * GB, disk=1 * GB) == "no"
    assert fit(16 * GB, disk=5 * GB) == "tight"
    # With an NVIDIA card, loading checks the card; the machine's memory does not decide.
    assert fit(4 * GB, gpu=(1_000, 12_000)) == "fits"
    assert fit(None) == "fits"


def test_the_cloud_reads_first_and_the_local_model_where_the_machine_runs_it() -> None:
    def order(level, cloud):
        cfg = Config(workflows=WorkflowsConfig(inference_mode="legacy", rule_reader_cloud=cloud))
        return reading_order(cfg, (level, ""))

    assert order("fits", True) == ["cloud", "local"]
    assert order("tight", True) == ["cloud", "local"]
    assert order("no", True) == ["cloud"]
    # Kept on this machine: only the local model, and only where it runs.
    assert order("fits", False) == ["local"]
    assert order("tight", False) == ["local"]
    assert order("no", False) == []


# -- pasted prompts --------------------------------------------------------------

DOCTOR = (
    "Help me fix these Claude Code settings issues. For each issue: briefly explain what "
    "the fix will do, then ask me to confirm before running any shell command that deletes "
    "files, modifies global config, or changes my installation."
)


def test_a_long_prompt_pasted_in_several_sessions_is_not_the_owners_words(
    conn: sqlite3.Connection,
) -> None:
    _entry(conn, "doctor1", DOCTOR, thread="ai:claude_code:s1")
    _entry(conn, "doctor2", DOCTOR + " Issue: stale plugin.", thread="ai:claude_code:s2")
    # A short rule said in two sessions is the owner's, twice.
    _entry(conn, "emoji1", "do not use emojis", thread="ai:claude_code:s1")
    _entry(conn, "emoji2", "do not use emojis", thread="ai:claude_code:s3")
    stats = MiningStats()
    assert [turn.entry_id for turn in owner_turns(conn, stats, cloud=False)] == [
        "emoji1", "emoji2"
    ]
    assert stats.pasted == 2


# -- what agents wrote ----------------------------------------------------------

BRIEF = (
    '<paseo-system version="1" kind="agent-message" source-agent-id="a1">'
    "Core contract is landed. Implement the dashboard against service.py and report back."
    "</paseo-system>"
)


def test_a_message_an_agent_sent_is_not_the_owners_words(conn: sqlite3.Connection) -> None:
    _entry(conn, "brief", BRIEF, thread="ai:codex:s1")
    _entry(conn, "failover", (
        "This task is continuing from an earlier Codex session that stopped because its "
        "account reached its usage limit. <previous-session> User: never push to main "
        "without asking </previous-session>"), thread="ai:codex:s2")
    _entry(conn, "own", "never push to main without asking me first", thread="ai:codex:s3")
    stats = MiningStats()
    assert [turn.entry_id for turn in owner_turns(conn, stats, cloud=False)] == ["own"]
    assert stats.agent_written == 2


def test_in_a_thread_an_agent_started_only_the_owners_typing_is_read(
    conn: sqlite3.Connection,
) -> None:
    _entry(conn, "start", "You are a subagent directed by the parent; implement the UI "
                          "files only and report every change.", thread="ai:codex:w")
    # Workers' status notes, sent into the thread without an envelope.
    _entry(conn, "status", "Dashboard integration has a few unresolved contract details "
                           "that the core owner should confirm.", thread="ai:codex:w")
    # The owner stepping in.
    _entry(conn, "nudge", "pause that agent and wait for me", thread="ai:codex:w")
    _entry(conn, "typed", "the replies sound weird, they should be in my voice and short",
           thread="ai:codex:w")
    # The same capitalised words in a thread the owner started are theirs.
    _entry(conn, "mine", "Dashboard copy should always be short and plain.",
           thread="ai:codex:o")
    assert [turn.entry_id for turn in owner_turns(conn, MiningStats(), cloud=False)] == [
        "nudge", "typed", "mine"
    ]


def test_a_failover_carrying_the_owners_request_keeps_the_thread_theirs(
    conn: sqlite3.Connection,
) -> None:
    _entry(conn, "relay", (
        "This task is continuing from an earlier Claude Code session that stopped because "
        "every account reached its usage limit. <previous-session> User: update the setup "
        "style to match the new one, less text in general [ran `ls`] Assistant: done"),
        thread="ai:codex:c")
    _entry(conn, "next", "Also make the logo smaller on every page of the setup.",
           thread="ai:codex:c")
    assert [turn.entry_id for turn in owner_turns(conn, MiningStats(), cloud=False)] == [
        "next"
    ]
