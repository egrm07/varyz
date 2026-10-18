"""The owner's turns to coding agents are quotable, and `privacy release` lets
go of what an older rule locked away -- without letting go of anything today's
rules still hold back."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from varyz import cli
from varyz.config import SensitivityConfig, load_config
from varyz.db.connection import open_db
from varyz.domain import Sensitivity
from varyz.pipeline.sensitivity import SensitivityGate, release

runner = CliRunner()


def _entry(conn: sqlite3.Connection, entry_id: str, source: str, body: str) -> None:
    stamp = datetime(2026, 9, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, sensitivity, authored_by_user)
           VALUES (?, 'message', ?, ?, ?, ?, ?, ?, 'sensitive', 1)""",
        (entry_id, source, entry_id, entry_id, body, stamp, stamp),
    )


def _seed(conn: sqlite3.Connection) -> None:
    _entry(conn, "turn", "ai_conversations", "Always write the report first.")
    _entry(conn, "health", "ai_conversations", "Book the therapist for Tuesday.")
    _entry(conn, "chat", "whatsapp", "See you at eight.")
    conn.commit()


def _labels(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT id, sensitivity FROM entries")}


def test_an_agent_turn_is_personal_but_a_private_topic_in_it_is_not():
    gate = SensitivityGate(SensitivityConfig())

    assert gate.label("ai_conversations", None, "Use pathlib.") == Sensitivity.PERSONAL
    assert gate.label("ai_conversations", None, "My therapist says") == Sensitivity.SENSITIVE
    assert gate.label("whatsapp", None, "See you at eight.") == Sensitivity.SENSITIVE


def test_release_counts_first_and_lowers_only_what_todays_rules_let_go(conn):
    _seed(conn)
    gate = SensitivityGate(SensitivityConfig())

    counted = release(conn, gate)
    assert counted.released == {"ai_conversations": 1}
    assert counted.held == {"ai_conversations": 1, "whatsapp": 1}
    assert set(_labels(conn).values()) == {"sensitive"}

    release(conn, gate, apply=True)
    assert _labels(conn) == {"turn": "personal", "health": "sensitive", "chat": "sensitive"}
    events = conn.execute(
        """SELECT aggregate_id FROM internal_events
           WHERE event_type = 'entry_sensitivity_released'"""
    ).fetchall()
    assert [row[0] for row in events] == ["turn"]


def test_release_can_be_limited_to_one_source(conn):
    _seed(conn)
    _entry(conn, "note", "documents", "Always pin versions.")
    conn.commit()

    result = release(
        conn, SensitivityGate(SensitivityConfig()), sources=("documents",), apply=True
    )

    assert result.released == {"documents": 1}
    assert _labels(conn)["turn"] == "sensitive"


def test_the_cli_is_a_dry_run_until_told_to_apply(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.toml"
    assert runner.invoke(cli.app, ["init", "--config", str(config)]).exit_code == 0
    db = open_db(load_config(config).db_path)
    _seed(db)

    dry = runner.invoke(cli.app, ["privacy", "release", "--config", str(config)])
    assert dry.exit_code == 0, dry.output
    assert "would be released" in dry.output
    assert "--apply" in dry.output
    assert _labels(db)["turn"] == "sensitive"

    applied = runner.invoke(
        cli.app,
        ["privacy", "release", "--source", "ai_conversations", "--apply", "--config", str(config)],
    )
    assert applied.exit_code == 0, applied.output
    assert _labels(db) == {"turn": "personal", "health": "sensitive", "chat": "sensitive"}
    db.close()
