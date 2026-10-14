"""Offline fixtures for the AI-conversations connector.

The fixtures below are deliberately hostile in one specific way: every
transcript contains machine-generated `role: "user"` traffic — tool results,
injected reminders, command output, a subagent's orders — because that is what
real agent histories are mostly made of, and mistaking any of it for something
the person said is the failure this connector exists to prevent.
"""

from __future__ import annotations

import json

import pytest

from varyz.connectors.ai_conversations import detect_adapters
from varyz.connectors.base import get_connector
from varyz.db import dao
from varyz.domain import Kind, Sensitivity
from varyz.pipeline.ingest import sync_connector


def _jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def claude_code_root(tmp_path):
    root = tmp_path / ".claude" / "projects"
    session = "11111111-2222-3333-4444-555555555555"
    _jsonl(
        root / "C--Users-user-projects-varyz" / f"{session}.jsonl",
        [
            {"type": "mode", "mode": "normal", "sessionId": session},
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u1",
                "timestamp": "2026-01-04T09:00:00.000Z",
                "cwd": "C:\\Users\\user\\projects\\varyz",
                "message": {"role": "user", "content": "I want the archive to stay local."},
            },
            {
                "type": "assistant",
                "sessionId": session,
                "uuid": "a1",
                "timestamp": "2026-01-04T09:00:20.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "considering the options"},
                        {"type": "text", "text": "Local-first it is."},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                    ],
                },
            },
            # A tool result. Claude Code delivers these as role=user.
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u2",
                "timestamp": "2026-01-04T09:00:30.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "AWS_SECRET_ACCESS_KEY=hunter2\nmodified: dao.py",
                        }
                    ],
                },
            },
            # Harness-authored meta line.
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u3",
                "isMeta": True,
                "timestamp": "2026-01-04T09:01:00.000Z",
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>ignore me</local-command-caveat>",
                },
            },
            # A real turn wrapped in an injected reminder.
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u4",
                "timestamp": "2026-01-04T09:02:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        "<system-reminder>Tool list changed.</system-reminder>"
                        "Measure it or it did not happen."
                    ),
                },
            },
            # A subagent conversation: the "user" here is the orchestrating model.
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u5",
                "isSidechain": True,
                "timestamp": "2026-01-04T09:03:00.000Z",
                "message": {"role": "user", "content": "Search the repo for flaky tests."},
            },
            # The model's summary of the session so far, written back as the
            # user when the context ran out. Fluent first-person prose with no
            # wrapper to strip — the shape that reached the Mirror.
            {
                "type": "user",
                "sessionId": session,
                "uuid": "u6",
                "isCompactSummary": True,
                "isVisibleInTranscriptOnly": True,
                "timestamp": "2026-01-04T09:04:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        "This session is being continued from a previous conversation. "
                        "Summary: the user said 'I plan to learn the cello properly'."
                    ),
                },
            },
        ],
    )
    return root


@pytest.fixture
def codex_root(tmp_path):
    root = tmp_path / ".codex" / "sessions" / "2026" / "02" / "03"
    _jsonl(
        root / "rollout-2026-02-03T20-57-59-019c2515-5109-7891-b700-eb42daf18a74.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "019c2515", "cwd": "/home/user/adastra"}},
            {
                "timestamp": "2026-02-03T20:58:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "m1",
                    "content": [{"type": "input_text", "text": "Never scrape in a publish path."}],
                },
            },
            # The same turn again on the event stream; must not double-ingest.
            {
                "timestamp": "2026-02-03T20:58:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Never scrape in a publish path."},
            },
            {
                "timestamp": "2026-02-03T20:58:30Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "m2",
                    "content": [
                        {"type": "output_text", "text": "Understood — official APIs only."}
                    ],
                },
            },
            {
                "timestamp": "2026-02-03T20:59:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "ls: cannot access '/etc/shadow'",
                },
            },
            # A replayed compaction summary. A rollout carries no flag for this,
            # so only the preamble distinguishes it from something he typed.
            {
                "timestamp": "2026-02-03T21:05:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "m3",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "This session is being continued from a previous "
                                "conversation. Summary: he wants to buy a sailboat."
                            ),
                        }
                    ],
                },
            },
        ],
    )
    return tmp_path / ".codex" / "sessions"


@pytest.fixture
def openclaw_root(tmp_path):
    root = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    _jsonl(
        root / "session-2026-03-01.jsonl",
        [
            {
                "session_id": "oc-1",
                "timestamp": "2026-03-01T08:00:00Z",
                "message": {"role": "user", "content": "Keep the kill switch reachable."},
            },
            {
                "session_id": "oc-1",
                "timestamp": "2026-03-01T08:00:10Z",
                "message": {"role": "assistant", "content": "Pause sentinel added."},
            },
            {"session_id": "oc-1", "type": "tool_result", "output": "exit 0"},
        ],
    )
    return root


def _options(root, adapter, **extra):
    return {"approved": True, "adapters": [adapter], "paths": [str(root)], **extra}


def test_a_since_that_cannot_be_parsed_stops_the_sync_instead_of_importing_all(
    cfg, conn, vault, claude_code_root
):
    """A cut-off that reads back as None is not a cut-off.

    The date was parsed leniently and a failure became None, which the
    filter treats as 'no cut-off' -- so a typo imported the whole history
    and said nothing about it.
    """
    cfg.connectors["ai_conversations"] = _options(
        claude_code_root, "claude_code", since="last tuesday"
    )
    with pytest.raises(ValueError, match="not a date"):
        sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0

    cfg.connectors["ai_conversations"] = _options(
        claude_code_root, "claude_code", since="2000-01-01"
    )
    assert sync_connector(conn, vault, get_connector("ai_conversations"), cfg).new == 2


def test_only_the_users_own_turns_are_ingested(cfg, conn, vault, claude_code_root):
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    assert stats.new == 2
    bodies = [
        row["body"]
        for row in conn.execute("SELECT body FROM entries ORDER BY occurred_start")
    ]
    assert "I want the archive to stay local." in bodies[0]
    assert "Measure it or it did not happen." in bodies[1]
    # The injected wrapper is stripped, not indexed alongside the real words.
    assert "system-reminder" not in bodies[1]

    everything = " ".join(bodies)
    assert "hunter2" not in everything  # tool result
    assert "modified: dao.py" not in everything  # tool result
    assert "ignore me" not in everything  # isMeta
    assert "flaky tests" not in everything  # sidechain
    assert "Local-first it is." not in everything  # assistant, not requested
    # The compaction summary. Measured on the developer's own archive before
    # this line existed: 75 of 1,414 indexed turns were one of these, and the
    # Mirror was reporting a quoted test fixture as one of his goals.
    assert "learn the cello" not in everything


def test_assistant_turns_are_opt_in_and_never_authored(cfg, conn, vault, claude_code_root):
    cfg.connectors["ai_conversations"] = _options(
        claude_code_root, "claude_code", include_assistant=True
    )
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    rows = {
        row["external_id"]: row
        for row in conn.execute("SELECT external_id, body, authored_by_user FROM entries")
    }
    assert len(rows) == 3
    assistant = rows["claude_code:11111111-2222-3333-4444-555555555555:a1"]
    assert assistant["authored_by_user"] == 0
    assert "Local-first it is." in assistant["body"]
    # Thinking and tool_use blocks in the same message are dropped.
    assert "considering the options" not in assistant["body"]
    assert all(
        rows[key]["authored_by_user"] == 1
        for key in rows
        if key.endswith(":u1") or key.endswith(":u4")
    )


def test_transcripts_are_sensitive_and_addressed(cfg, conn, vault, claude_code_root):
    cfg.sensitivity.source_defaults["ai_conversations"] = "sensitive"
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    row = conn.execute(
        "SELECT * FROM entries WHERE external_id LIKE '%:u1'"
    ).fetchone()
    assert row["kind"] == Kind.MESSAGE
    assert row["sensitivity"] == Sensitivity.SENSITIVE
    assert row["thread_id"] == "ai:claude_code:11111111-2222-3333-4444-555555555555"
    assert row["authored_by_user"] == 1
    assert "From: me" in row["body"]
    assert "Conversation: Claude Code — varyz" in row["body"]
    assert row["occurred_start"] == "2026-01-04T09:00:00+00:00"


def test_repeat_scan_is_idempotent_and_resumes_at_new_turns(
    cfg, conn, vault, claude_code_root
):
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    connector = get_connector("ai_conversations")
    first = sync_connector(conn, vault, connector, cfg)
    second = sync_connector(conn, vault, connector, cfg)

    assert first.new == 2
    assert second.total == 0  # unchanged files are not even re-read

    session_file = next(claude_code_root.rglob("*.jsonl"))
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "11111111-2222-3333-4444-555555555555",
                    "uuid": "u9",
                    "timestamp": "2026-01-05T10:00:00.000Z",
                    "message": {"role": "user", "content": "Ship the honest version."},
                }
            )
            + "\n"
        )
    third = sync_connector(conn, vault, connector, cfg)
    assert third.new == 1 and third.unchanged == 0
    assert dao.count_entries(conn) == 3


def test_prompt_history_keeps_sessions_whose_transcripts_are_gone(
    cfg, conn, vault, claude_code_root
):
    """Claude Code deletes transcripts after about 30 days; its prompt history
    keeps what the person typed. Sessions a transcript was read for are not
    imported twice, and harness commands and pastes are not speech."""
    kept = "11111111-2222-3333-4444-555555555555"
    gone = "99999999-8888-7777-6666-555555555555"
    base = {"project": "C:\\Users\\user\\projects\\site", "pastedContents": {}}
    _jsonl(
        claude_code_root.parent / "history.jsonl",
        [
            {**base, "display": "I want the archive to stay local.", "sessionId": kept,
             "timestamp": 1767517200000},
            {**base, "display": "Pick the colours first.", "sessionId": gone,
             "timestamp": 1767603600000},
            {**base, "display": "/clear", "sessionId": gone, "timestamp": 1767603660000},
            {**base, "display": "! git status", "sessionId": gone, "timestamp": 1767603670000},
            {**base, "display": "continue", "sessionId": gone, "timestamp": 1767603680000,
             "pastedContents": {"1": {"content": "SECRET=hunter2"}}},
            {**base, "display": "continue", "sessionId": "another", "timestamp": 1767603690000},
        ],
    )
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    connector = get_connector("ai_conversations")
    assert sync_connector(conn, vault, connector, cfg).new == 5

    rows = conn.execute(
        "SELECT body, thread_id, authored_by_user FROM entries WHERE external_id LIKE ?",
        ("claude_code:%:prompt-%",),
    ).fetchall()
    assert len(rows) == 3
    assert all(row["authored_by_user"] == 1 for row in rows)
    colours = next(row for row in rows if "Pick the colours first." in row["body"])
    assert colours["thread_id"] == f"ai:claude_code:{gone}"
    assert "Conversation: Claude Code — site" in colours["body"]
    everything = " ".join(row["body"] for row in rows)
    assert "/clear" not in everything and "git status" not in everything
    assert "hunter2" not in everything
    assert "archive to stay local" not in everything  # its transcript was read

    assert sync_connector(conn, vault, connector, cfg).total == 0


def test_rewritten_file_is_reread_without_duplicating(cfg, conn, vault, claude_code_root):
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    connector = get_connector("ai_conversations")
    sync_connector(conn, vault, connector, cfg)

    # A shorter file under the same name: line numbers no longer mean the same
    # thing, so the whole file has to be read again.
    session_file = next(claude_code_root.rglob("*.jsonl"))
    _jsonl(
        session_file,
        [
            {
                "type": "user",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "uuid": "u1",
                "cwd": "C:\\Users\\user\\projects\\varyz",
                "timestamp": "2026-01-04T09:00:00.000Z",
                "message": {"role": "user", "content": "I want the archive to stay local."},
            }
        ],
    )
    stats = sync_connector(conn, vault, connector, cfg)
    assert stats.new == 0 and stats.unchanged == 1
    assert dao.count_entries(conn) == 2


def test_codex_rollout_ingests_once_despite_duplicate_event_stream(
    cfg, conn, vault, codex_root
):
    cfg.connectors["ai_conversations"] = _options(codex_root, "codex", include_assistant=True)
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    assert stats.new == 2
    rows = list(conn.execute("SELECT body, authored_by_user FROM entries"))
    authored = [r["body"] for r in rows if r["authored_by_user"]]
    assert len(authored) == 1
    assert "Never scrape in a publish path." in authored[0]
    assert "Conversation: Codex — adastra" in authored[0]
    assert "/etc/shadow" not in " ".join(r["body"] for r in rows)


def test_a_replayed_summary_is_not_the_users_words_in_any_format(
    cfg, conn, vault, codex_root
):
    """A rollout has no `isCompactSummary`, so the preamble is the only signal.

    21 of the 75 summaries in the developer's live archive came in this way,
    flagless, from Codex. Without this the archive reads a model's account of a
    conversation as the account of the person who had it.
    """
    cfg.connectors["ai_conversations"] = _options(codex_root, "codex")
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    bodies = " ".join(row["body"] for row in conn.execute("SELECT body FROM entries"))
    assert "sailboat" not in bodies
    assert "Never scrape in a publish path." in bodies


def test_writing_about_compaction_is_still_the_users_words(cfg, conn, vault, tmp_path):
    """The guard is anchored, so the sentence survives anywhere but the opening.

    Measured before it was written: all 399 real occurrences on this machine sit
    at offset 0 of the message. A person complaining about compaction is not one
    of them, and dropping their sentence would be the same defect inverted.
    """
    root = tmp_path / ".codex" / "sessions"
    _jsonl(
        root / "rollout-2026-02-04T10-00-00-019c2515-5109-7891-b700-eb42daf18a75.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "019c2516", "cwd": "/home/user/varyz"}},
            {
                "timestamp": "2026-02-04T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "m1",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "I hate it when the banner says This session is being "
                                "continued from a previous conversation."
                            ),
                        }
                    ],
                },
            },
        ],
    )
    cfg.connectors["ai_conversations"] = _options(root, "codex")
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    assert stats.new == 1
    body = conn.execute("SELECT body FROM entries").fetchone()["body"]
    assert "I hate it when the banner says" in body


def test_a_report_the_harness_posted_back_is_not_something_he_said(cfg, conn, vault, tmp_path):
    """A finished background task arrives as a `role: "user"` turn and is not one.

    Measured on this machine before it was written: 336 of the 1,392 turns stored
    as his own words opened with one of these wrappers — 24% — and
    `<task-notification>` alone was 302 of them, 7 of which the harness never
    closed. Closed ones strip to nothing and are dropped for being empty; the
    unclosed ones have no wrapper to strip, which is why the opening is checked
    as well. The check is only for unclosed openings on purpose — a reminder
    prepended to something he did type also opens the turn, and that sentence is
    kept (`test_only_the_users_own_turns_are_ingested`).
    """
    root = tmp_path / ".claude" / "projects"
    session = "99999999-8888-7777-6666-555555555555"
    _jsonl(
        root / "C--Users-user-projects-varyz" / f"{session}.jsonl",
        [
            {
                "type": "user",
                "sessionId": session,
                "uuid": "n1",
                "timestamp": "2026-03-02T09:00:00.000Z",
                "cwd": "C:\\Users\\user\\projects\\varyz",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>\nAgent bench-runner finished: 41 rows written "
                        "to data/bench/out.jsonl. I decided to keep the second pass."
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": session,
                "uuid": "n2",
                "timestamp": "2026-03-02T09:02:00.000Z",
                "cwd": "C:\\Users\\user\\projects\\varyz",
                "message": {
                    "role": "user",
                    "content": (
                        '<in-app-browser-context source="ambient-ui-state">\n'
                        "The user opened san7.app in the in-app browser.\n"
                        "</in-app-browser-context>"
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": session,
                "uuid": "n3",
                "timestamp": "2026-03-02T09:05:00.000Z",
                "cwd": "C:\\Users\\user\\projects\\varyz",
                "message": {
                    "role": "user",
                    "content": (
                        "The task-notification blocks are cluttering my scrollback, "
                        "can we route them somewhere else?"
                    ),
                },
            },
        ],
    )
    cfg.connectors["ai_conversations"] = _options(root, "claude_code")
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    assert stats.new == 1
    bodies = " ".join(row["body"] for row in conn.execute("SELECT body FROM entries"))
    assert "I decided to keep the second pass." not in bodies
    assert "opened san7.app in the in-app browser" not in bodies
    assert "cluttering my scrollback" in bodies


def test_resumed_rollout_still_knows_which_project_it_was(cfg, conn, vault, codex_root):
    """A Codex file states its project once, in a header the resume skips past.

    Without the checkpointed header the second half of a conversation would be
    filed under a bare uuid while the first half kept the project name — the
    same thread, split in two.
    """
    cfg.connectors["ai_conversations"] = _options(codex_root, "codex")
    connector = get_connector("ai_conversations")
    sync_connector(conn, vault, connector, cfg)

    rollout = next(codex_root.rglob("rollout-*.jsonl"))
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-02-03T21:10:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "m9",
                        "content": [{"type": "input_text", "text": "Official APIs only."}],
                    },
                }
            )
            + "\n"
        )
    stats = sync_connector(conn, vault, connector, cfg)

    assert stats.new == 1
    resumed = conn.execute(
        "SELECT body, thread_id FROM entries WHERE body LIKE '%Official APIs only.%'"
    ).fetchone()
    assert "Conversation: Codex — adastra" in resumed["body"]
    assert resumed["thread_id"] == "ai:codex:019c2515"


def test_generic_adapter_reads_openclaw_and_skips_tool_lines(cfg, conn, vault, openclaw_root):
    cfg.connectors["ai_conversations"] = _options(openclaw_root, "openclaw")
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    assert stats.new == 1
    row = conn.execute("SELECT * FROM entries").fetchone()
    assert "Keep the kill switch reachable." in row["body"]
    assert row["thread_id"] == "ai:openclaw:oc-1"
    assert row["authored_by_user"] == 1


def test_since_bounds_the_first_import(cfg, conn, vault, claude_code_root):
    cfg.connectors["ai_conversations"] = _options(
        claude_code_root, "claude_code", since="2026-01-04T09:01:00+00:00"
    )
    stats = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert stats.new == 1


def test_unknown_adapter_is_a_configuration_error(cfg, conn, vault, tmp_path):
    cfg.connectors["ai_conversations"] = _options(tmp_path, "gemini_cli")
    with pytest.raises(ValueError, match="Unknown AI conversation adapter"):
        sync_connector(conn, vault, get_connector("ai_conversations"), cfg)


def test_unapproved_history_is_never_read(cfg, conn, vault, claude_code_root):
    cfg.connectors["ai_conversations"] = {
        "adapters": ["claude_code"],
        "paths": [str(claude_code_root)],
    }
    with pytest.raises(ValueError, match="requires explicit approval"):
        sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert dao.count_entries(conn) == 0


def test_configured_paths_replace_the_default_locations(cfg, conn, vault, tmp_path):
    """An explicit path is the whole reach, never a hint added to the defaults.

    Otherwise "read this one exported folder" silently also reads every agent
    session on the machine, and nothing in the output would say so.
    """
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    cfg.connectors["ai_conversations"] = _options(empty, "claude_code")
    with pytest.raises(ValueError, match="No AI conversation history found"):
        sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert dao.count_entries(conn) == 0


def test_detect_adapters_reads_directory_names_only(tmp_path, claude_code_root, codex_root):
    found = {adapter.adapter_id for adapter, _ in detect_adapters(tmp_path)}
    assert found == {"claude_code", "codex"}


# --- the Personal Insight gate ----------------------------------------------


def _claude_line(session, uuid, text, **extra):
    return {
        "type": "user",
        "sessionId": session,
        "uuid": uuid,
        "timestamp": "2026-09-22T09:00:00.000Z",
        "cwd": "C:\\Users\\user\\projects\\varyz",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _codex_rollout(root, name, meta, text):
    _jsonl(
        root / name,
        [
            {"type": "session_meta", "payload": meta},
            {
                "timestamp": "2026-09-22T20:06:11Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": f"msg-{meta['id']}",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        ],
    )


def test_a_program_started_session_is_not_the_person_speaking(cfg, conn, vault, tmp_path):
    """`claude -p`, a Codex guardian review, and a spawned Codex worker.

    Measured on 2026-09-26: 92 guardian preambles ("The following is the Codex
    agent history...") and two benchmark prompts written *about* Egor were
    stored as his own words. Each such file says who started it; the one the
    person started is still read.
    """
    claude_root = tmp_path / ".claude" / "projects"
    _jsonl(
        claude_root / "p" / "aaaa.jsonl",
        [_claude_line("aaaa", "s1", "You are advising a coding agent acting for Egor.",
                      entrypoint="sdk-cli")],
    )
    _jsonl(
        claude_root / "p" / "bbbb.jsonl",
        [_claude_line("bbbb", "c1", "Ship the landing page today.", entrypoint="cli")],
    )
    codex_root = tmp_path / ".codex" / "sessions"
    _codex_rollout(
        codex_root,
        "rollout-2026-09-22T22-06-10-01a0cab9.jsonl",
        {"id": "01a0cab9", "session_id": "01a0c0c5", "cwd": "/p/varyz",
         "source": {"subagent": {"other": "guardian"}}, "thread_source": "guardian_review"},
        "The following is the Codex agent history whose request action you are assessing.",
    )
    _codex_rollout(
        codex_root,
        "rollout-2026-09-22T22-07-10-01a0cab8.jsonl",
        {"id": "01a0cab8", "cwd": "/p/varyz", "source": "exec", "originator": "codex_exec"},
        "Summarise the failing tests.",
    )
    _codex_rollout(
        codex_root,
        "rollout-2026-09-22T22-08-10-01a0cab7.jsonl",
        {"id": "01a0cab7", "cwd": "/p/varyz", "source": "vscode", "thread_source": "user"},
        "Keep the archive local.",
    )
    cfg.connectors["ai_conversations"] = {
        "approved": True,
        "adapters": ["claude_code", "codex"],
        "paths": [str(claude_root), str(codex_root)],
    }
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    bodies = [row["body"] for row in conn.execute("SELECT body FROM entries")]
    assert len(bodies) == 2
    joined = " ".join(bodies)
    assert "Ship the landing page today." in joined
    assert "Keep the archive local." in joined


@pytest.mark.parametrize(
    ("text", "speech"),
    [
        ("[Request interrupted by user]", False),
        ("[Request interrupted by user for tool use]", False),
        ("# AGENTS.md instructions for c:\\Users\\user\\projects\\sanity", False),
        ("/compact", False),
        ("/model fable 5", False),
        ("/effort ultracode", False),
        ("/cd SAN7.10", False),
        ("<heartbeat>\n  <automation_id>nightly</automation_id>\n</heartbeat>", False),
        ('<codex_internal_context source="goal">\nContinue.\n</codex_internal_context>', False),
        ("/btw status?", True),
        ("/btw still working?", True),
        ("/goal ship the site by Friday", True),
        ("I pressed stop: [Request interrupted by user] was me.", True),
        ("fix the padding", True),
    ],
)
def test_harness_lines_are_not_speech(text, speech):
    from varyz.connectors.ai_conversations import is_machine_text

    assert is_machine_text(text) is not speech


def test_turns_stored_under_older_rules_are_withdrawn_once(
    cfg, conn, vault, tmp_path, monkeypatch
):
    """A tightened rule must also reach what the archive already holds.

    Ingestion is incremental, so a transcript read before a rule existed is
    never read again and its machine turns stayed his words for good. The first
    sync on a new rules version judges the stored turns again, once.
    """
    import re

    from varyz.connectors import ai_conversations as module

    root = tmp_path / ".claude" / "projects"
    _jsonl(
        root / "p" / "cccc.jsonl",
        [
            _claude_line("cccc", "c1", "[Request interrupted by user]"),
            _claude_line("cccc", "c2", "Measure it or it did not happen."),
        ],
    )
    _jsonl(
        root / "p" / "dddd.jsonl",
        [_claude_line("dddd", "d1", "Score these answers for Egor.", entrypoint="sdk-cli")],
    )
    cfg.connectors["ai_conversations"] = _options(root, "claude_code")

    # The archive as an older version left it.
    monkeypatch.setattr(module, "AUTHORSHIP_RULES", 1)
    monkeypatch.setattr(module, "_HARNESS_LINE", re.compile(r"(?!)"))
    monkeypatch.setattr(module, "_PROGRAM_ENTRYPOINTS", frozenset())
    first = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert first.new == 3
    monkeypatch.undo()

    second = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert second.deleted == 2
    bodies = [row["body"] for row in conn.execute("SELECT body FROM entries")]
    assert len(bodies) == 1 and "Measure it or it did not happen." in bodies[0]
    cursor = dao.get_cursor(conn, "ai_conversations")
    assert cursor is not None and cursor.data["authorship_rules"] == module.AUTHORSHIP_RULES

    # Judged once per version: the next sync neither re-reads nor deletes.
    third = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert third.deleted == 0 and third.new == 0


def test_a_program_run_from_inside_the_desktop_app_is_not_the_person(
    cfg, conn, vault, tmp_path, monkeypatch
):
    """`claude -p` inherits the desktop app's entrypoint; `turnOrigin` does not.

    Found 2026-09-26 by running headless jobs from a desktop session: every
    line said "claude-desktop", so the harness prompts would have been filed
    as the owner's words. Stray program turns inside a typed session go alone.
    """
    from varyz.connectors import ai_conversations as module

    root = tmp_path / ".claude" / "projects"
    desktop = {"entrypoint": "claude-desktop"}
    _jsonl(
        root / "jobs" / "eeee.jsonl",
        [
            _claude_line("eeee", "e1", "Build a one-page landing site for tallyho.",
                         turnOrigin="sdk", **desktop),
            {"type": "assistant", "sessionId": "eeee", "uuid": "e2", **desktop,
             "timestamp": "2026-09-22T09:00:05.000Z",
             "message": {"role": "assistant", "content": "Done: index.html"}},
        ],
    )
    _jsonl(
        root / "p" / "ffff.jsonl",
        [
            _claude_line("ffff", "f1", "Keep it local.", turnOrigin="human",
                         origin={"kind": "human"}, **desktop),
            _claude_line("ffff", "f2", "Background command finished.",
                         turnOrigin="task_notification",
                         origin={"kind": "task-notification"}, **desktop),
            _claude_line("ffff", "f3", "Scheduled check-in: run the digest.",
                         turnOrigin="sdk", **desktop),
            _claude_line("ffff", "f4", "And ship it.", turnOrigin="human", **desktop),
        ],
    )
    cfg.connectors["ai_conversations"] = _options(root, "claude_code")

    # Stored by the rules before this one, then withdrawn by it.
    monkeypatch.setattr(module, "AUTHORSHIP_RULES", 2)
    monkeypatch.setattr(module, "_typed_by_program", lambda data: False)
    monkeypatch.setattr(module, "_program_threads", lambda adapter_id, path: set())
    first = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    stored = conn.execute("SELECT COUNT(*) FROM entries WHERE authored_by_user=1").fetchone()[0]
    assert stored == 5
    monkeypatch.undo()

    second = sync_connector(conn, vault, get_connector("ai_conversations"), cfg)
    assert second.deleted == first.new - 2
    owner = sorted(
        row["body"].split("\n\n", 1)[1]
        for row in conn.execute("SELECT body FROM entries WHERE authored_by_user=1")
    )
    assert owner == ["And ship it.", "Keep it local."]
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2

    # A first read of the same files keeps the same two.
    options = {**cfg.connectors["ai_conversations"], "_data_dir": str(tmp_path / "fresh")}
    fresh = [item for item, _ in get_connector("ai_conversations").discover(options, None)]
    assert len(fresh) == 2


@pytest.mark.parametrize(("cwd", "project"), [
    ("/Users/egor/projects/SAN7.14", "SAN7.14"),
    ("C:\\Users\\user\\projects\\varyz", "varyz"),
    ("file:///home/user/tools", "tools"),
    # A home folder, a scratch folder and a Codex chat folder name no project:
    # the last is named after the chat's first words ("my", "help-me-tailor").
    ("/Users/egor", None),
    ("/home/user", None),
    ("/private/tmp", None),
    ("/Users/egor/Documents/Codex/2026-09-12/my", None),
    ("/Users/egor/Documents/Codex/2026-09-12/help-me-tailor-and-update-my", None),
])
def test_only_a_project_folder_names_a_project(cwd, project):
    from varyz.connectors.ai_conversations import _project_name

    assert _project_name(cwd) == project


def test_a_chat_in_no_project_is_filed_under_none(cfg, conn, vault, tmp_path):
    root = tmp_path / ".codex" / "sessions"
    _jsonl(root / "rollout-2026-09-12T10-00-00-019c9999.jsonl", [
        {"type": "session_meta",
         "payload": {"id": "019c9999", "cwd": "/Users/egor/Documents/Codex/2026-09-12/my"}},
        {"timestamp": "2026-09-12T10:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "id": "m1",
                     "content": [{"type": "input_text", "text": "My cover letter needs work."}]}},
    ])
    cfg.connectors["ai_conversations"] = _options(root, "codex")
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    [body] = [row["body"] for row in conn.execute("SELECT body FROM entries")]
    # Not "Codex — my", and not the session's id either.
    assert body.startswith("From: me\nConversation: Codex\n\n")


def test_a_turn_keeps_what_the_agent_had_just_said_beside_it_not_in_it(
    cfg, conn, vault, claude_code_root
):
    cfg.connectors["ai_conversations"] = _options(claude_code_root, "claude_code")
    sync_connector(conn, vault, get_connector("ai_conversations"), cfg)

    rows = [
        (row["body"], json.loads(row["conversation_metadata_json"] or "{}"))
        for row in conn.execute(
            "SELECT body, conversation_metadata_json FROM entries ORDER BY occurred_start")
    ]
    [(_first, opening), (reply, answered)] = rows
    # "Measure it" answers the agent's "Local-first it is." -- context to read it
    # by, kept out of the body because the owner did not say it.
    assert "context_before" not in opening
    assert answered["context_before"] == "Local-first it is."
    assert "Local-first it is." not in reply


def test_turns_are_filed_again_once_under_the_new_project_rule(
    cfg, conn, vault, codex_root
):
    from varyz.connectors import ai_conversations

    cfg.connectors["ai_conversations"] = _options(codex_root, "codex")
    connector = get_connector("ai_conversations")
    sync_connector(conn, vault, connector, cfg)
    conn.execute("UPDATE entries SET body = replace(body, 'adastra', 'hel'), content_hash = 'old'")
    conn.execute("UPDATE sync_state SET cursor_json = json_remove(cursor_json, "
                 "'$.project_rules')")
    conn.commit()

    sync_connector(conn, vault, connector, cfg)
    bodies = [row["body"] for row in conn.execute("SELECT body FROM entries")]
    assert bodies and all("Codex — adastra" in body for body in bodies)
    again = sync_connector(conn, vault, connector, cfg)
    assert again.new == 0 and ai_conversations.PROJECT_RULES == 2


@pytest.mark.parametrize("folder", [".codex", ".claude", "session-workspace", "cowork-recents"])
def test_app_workspace_is_not_a_project(tmp_path, folder):
    from varyz.connectors.ai_conversations import _project_name

    path = tmp_path / folder
    path.mkdir()
    assert _project_name(str(path)) is None


def test_project_subfolder_uses_checkout_identity(tmp_path):
    from varyz.connectors.ai_conversations import _project_name

    project = tmp_path / "my-product"
    (project / ".git").mkdir(parents=True)
    site = project / "deploy" / "site"
    site.mkdir(parents=True)
    assert _project_name(str(site)) == "my-product"


def test_managed_worktree_is_still_a_real_project(tmp_path):
    from varyz.connectors.ai_conversations import _project_name

    project = tmp_path / ".codex" / "worktrees" / "123" / "my-product"
    project.mkdir(parents=True)
    (project / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert _project_name(str(project)) == "my-product"
