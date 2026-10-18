"""Bounded local A/B shadow journal. Its failures never change a live result."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from varyz.config import Config
from varyz.resolution.shadow_policy import (
    POLICY_VERSION,
    Record,
    compile_answer,
    compile_packet,
    digest,
    packet_ids,
    project_records,
)

log = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_events (
    seq INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
    record_id TEXT NOT NULL, logical_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json))
);
CREATE TABLE IF NOT EXISTS comparisons (
    seq INTEGER PRIMARY KEY, capture_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, origin TEXT NOT NULL, phase TEXT NOT NULL,
    task_hash TEXT NOT NULL, conversation_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL, elapsed_ms REAL NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json))
);
CREATE INDEX IF NOT EXISTS comparisons_date ON comparisons(created_at);
CREATE TABLE IF NOT EXISTS reviews (
    seq INTEGER PRIMARY KEY, comparison_seq INTEGER NOT NULL REFERENCES comparisons(seq),
    outcome TEXT NOT NULL CHECK(outcome IN ('better','worse','equivalent','unclear')),
    window TEXT NOT NULL CHECK(window IN ('development','held_out','replication')),
    created_at TEXT NOT NULL
);
"""


def directory(cfg: Config) -> Path:
    return cfg.data_dir / "rules" / "shadow"


def enabled(cfg: Config) -> bool:
    root = directory(cfg)
    return (root / "ENABLED").is_file() and not (root / "PAUSED").exists()


def control(cfg: Config, action: Literal["enable", "pause", "resume", "disable"]) -> None:
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    if action == "enable":
        (root / "ENABLED").touch()
        (root / "PAUSED").unlink(missing_ok=True)
    elif action == "pause":
        (root / "PAUSED").touch()
    elif action == "resume":
        if not (root / "ENABLED").is_file():
            raise ValueError("shadow collection has not been enabled")
        (root / "PAUSED").unlink(missing_ok=True)
    else:
        (root / "ENABLED").unlink(missing_ok=True)


def _key(cfg: Config) -> bytes:
    path = directory(cfg) / "hash-key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("invalid shadow hash key")
    return key


def _private_hash(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _connect(cfg: Config) -> sqlite3.Connection:
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "shadow.sqlite3", timeout=0.05)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # Reserve half the byte budget for SQLite's transient rollback journal.
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        max_pages = max(1, cfg.rule_shadow.max_bytes // (2 * page_size))
        conn.execute(f"PRAGMA max_page_count={max_pages}")
        conn.executescript(_SCHEMA)
        for table in ("registry_events", "comparisons", "reviews"):
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table} BEGIN
                    SELECT RAISE(ABORT, 'shadow journal is append-only'); END""")
        return conn
    except Exception:
        conn.close()
        raise


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _write(
    cfg: Config, records: list[Record], task: str, conversation: str | None,
    origin: str, phase: str, result: dict[str, Any], started: float,
) -> bool:
    # A filesystem kill switch is read on every observation, including just
    # before writing, so long-running MCP/daemon processes can be paused at once.
    if not enabled(cfg):
        return False
    root = directory(cfg)
    size = sum(path.stat().st_size for path in root.glob("shadow.sqlite3*"))
    if size >= cfg.rule_shadow.max_bytes:
        return False
    key, now = _key(cfg), _utc()
    if result.get("comparison_kind") == "exact_answer_control":
        # Use keyed hashes for short answers too: ordinary hashes of choices
        # such as "yes" would be recoverable by a dictionary lookup.
        for container, name in ((result, "baseline_answer_hash"),
                                (result["proposal"], "answer_hash")):
            if container.get(name):
                container[name] = _private_hash(key, container[name])
    metadata = [record.metadata() for record in records]
    source_hash = digest(metadata)
    policy_hash = digest([
        Path(__file__).read_bytes().hex(),
        Path(__file__).with_name("shadow_policy.py").read_bytes().hex(),
    ])
    task_hash = _private_hash(key, task)
    conversation_hash = _private_hash(key, conversation or "")
    capture_key = digest([origin, phase, task_hash, conversation_hash, now[:10],
                          source_hash, result, policy_hash])
    with contextlib.closing(_connect(cfg)) as journal, journal:
        journal.execute("BEGIN IMMEDIATE")
        if journal.execute("SELECT 1 FROM comparisons WHERE capture_key=?",
                           (capture_key,)).fetchone():
            return False
        count = journal.execute("SELECT count(*) FROM comparisons WHERE created_at >= ?",
                                (now[:10],)).fetchone()[0]
        events = journal.execute("SELECT count(*) FROM registry_events").fetchone()[0]
        if count >= cfg.rule_shadow.max_comparisons_per_day:
            return False
        pending = []
        for record, value in zip(records, metadata, strict=True):
            event_key = digest(value)
            if not journal.execute("SELECT 1 FROM registry_events WHERE event_key=?",
                                   (event_key,)).fetchone():
                pending.append((event_key, record.id, record.logical_id, now, json.dumps(value)))
        if events + len(pending) > cfg.rule_shadow.max_registry_events:
            return False
        journal.executemany("INSERT INTO registry_events"
                            " (event_key,record_id,logical_id,observed_at,metadata_json)"
                            " VALUES (?,?,?,?,?)", pending)
        journal.execute("""INSERT INTO comparisons
            (capture_key,created_at,origin,phase,task_hash,conversation_hash,policy_version,
             policy_hash,source_hash,elapsed_ms,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (capture_key, now, origin, phase, task_hash, conversation_hash, POLICY_VERSION,
             policy_hash, source_hash, (time.perf_counter() - started) * 1000,
             json.dumps(result, sort_keys=True)))
    (root / "last-error.json").unlink(missing_ok=True)
    return True


def _failure(cfg: Config, exc: Exception) -> None:
    # Do not copy an exception message, prompt or row content into the log.
    log.debug("Rule shadow capture failed (%s)", type(exc).__name__)
    with contextlib.suppress(OSError):
        directory(cfg).mkdir(parents=True, exist_ok=True)
        (directory(cfg) / "last-error.json").write_text(json.dumps({
            "at": _utc(), "error_type": type(exc).__name__,
        }), encoding="utf-8")


def observe_briefing(
    conn: sqlite3.Connection, cfg: Config, task: str, project: str | None,
    conversation: str | None, baseline: dict[str, Any], *, origin: str,
    phase: str = "start_task", limit: int = 12,
) -> bool:
    if not enabled(cfg) or baseline.get("withheld_hint") or baseline.get("withheld"):
        return False
    started = time.perf_counter()
    try:
        records = project_records(conn, max_records=cfg.rule_shadow.max_source_records)
        domains = {item["domain"] if isinstance(item, dict) else str(item)
                   for item in baseline.get("kinds_of_work", [])}
        checklist = baseline.get("definition_of_done", [])
        if isinstance(checklist, dict):
            domains.update(checklist)
        else:
            domains.update(item.get("domain", "") for item in checklist)
        proposal = compile_packet(records, task, project, domains, limit=limit)
        current = packet_ids(baseline)
        result = {"baseline_ids": current, "proposal": proposal,
                  "added_ids": sorted(set(proposal["ids"]) - set(current)),
                  "removed_ids": sorted(set(current) - set(proposal["ids"])),
                  "baseline_serialized_chars": len(json.dumps(baseline, default=str)),
                  "comparison_kind": "packet_selection", "gold_label": None}
        return _write(cfg, records, task, conversation, origin, phase, result, started)
    except Exception as exc:
        _failure(cfg, exc)
        return False


def observe_answer(
    conn: sqlite3.Connection, cfg: Config, request: Any, baseline: Any, *,
    owner_answers_only: bool = False,
) -> bool:
    # The full projection must not be read on behalf of unregistered or withheld callers.
    if not enabled(cfg) or not request.caller.capability_id or baseline.withheld:
        return False
    started = time.perf_counter()
    try:
        records = project_records(conn, max_records=cfg.rule_shadow.max_source_records)
        proposal = compile_answer(records, request.question, request.project,
                                  owner_answers_only=owner_answers_only)
        result = {"comparison_kind": "exact_answer_control", "gold_label": None,
                  "baseline_disposition": str(baseline.disposition),
                  "baseline_basis": str(baseline.basis),
                  "baseline_answer_hash": digest(baseline.answer) if baseline.answer else None,
                  "proposal": proposal}
        return _write(cfg, records, request.question, request.conversation_id,
                      "daemon", "resolve", result, started)
    except Exception as exc:
        _failure(cfg, exc)
        return False


def status(cfg: Config) -> dict[str, Any]:
    root = directory(cfg)
    result: dict[str, Any] = {
        "enabled": enabled(cfg), "paused": (root / "PAUSED").exists(),
        "journal": str(root / "shadow.sqlite3"), "policy_version": POLICY_VERSION,
        "live_output": "current", "comparisons": 0, "prospective_comparisons": 0,
        "registry_events": 0, "reviews": {}, "last_capture_at": None,
        "limits": cfg.rule_shadow.model_dump(),
    }
    path = root / "shadow.sqlite3"
    if (root / "last-error.json").exists():
        result["last_error"] = json.loads((root / "last-error.json").read_text(encoding="utf-8"))
    if not path.exists():
        return result
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        result["comparisons"] = conn.execute("SELECT count(*) FROM comparisons").fetchone()[0]
        result["prospective_comparisons"] = conn.execute(
            "SELECT count(*) FROM comparisons WHERE origin != 'probe'").fetchone()[0]
        result["registry_events"] = conn.execute(
            "SELECT count(*) FROM registry_events").fetchone()[0]
        result["last_capture_at"] = conn.execute(
            "SELECT max(created_at) FROM comparisons").fetchone()[0]
        result["phases"] = dict(conn.execute(
            "SELECT phase,count(*) FROM comparisons GROUP BY phase"))
        result["today_comparisons"] = conn.execute(
            "SELECT count(*) FROM comparisons WHERE created_at >= ?", (_utc()[:10],)).fetchone()[0]
        # Only each comparison's latest judgment counts; corrections stay in history.
        reviews = conn.execute("""SELECT outcome,window FROM reviews WHERE seq IN
            (SELECT max(seq) FROM reviews GROUP BY comparison_seq)""").fetchall()
        result["reviews"] = dict(Counter(f"{window}:{outcome}" for outcome, window in reviews))
        result["bytes"] = sum(p.stat().st_size for p in root.glob("shadow.sqlite3*"))
    return result


def comparisons(cfg: Config, limit: int = 20) -> list[dict[str, Any]]:
    path = directory(cfg) / "shadow.sqlite3"
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT seq,created_at,origin,phase,elapsed_ms,result_json"
                            " FROM comparisons ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(seq=r[0], at=r[1], origin=r[2], phase=r[3], elapsed_ms=r[4],
                     result=json.loads(r[5])) for r in rows]


def review(cfg: Config, seq: int, outcome: str, window: str) -> None:
    with contextlib.closing(_connect(cfg)) as conn, conn:
        conn.execute("INSERT INTO reviews(comparison_seq,outcome,window,created_at)"
                     " VALUES (?,?,?,?)", (seq, outcome, window, _utc()))


def doctor(cfg: Config) -> dict[str, Any]:
    result = status(cfg)
    path = directory(cfg) / "shadow.sqlite3"
    result["integrity"] = "not_initialized"
    if path.exists():
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            result["integrity"] = conn.execute("PRAGMA quick_check").fetchone()[0]
            result["foreign_key_errors"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    result["budget_reached"] = result.get("bytes", 0) >= cfg.rule_shadow.max_bytes
    result["daily_limit_reached"] = (
        result.get("today_comparisons", 0) >= cfg.rule_shadow.max_comparisons_per_day)
    result["event_limit_reached"] = (
        result["registry_events"] >= cfg.rule_shadow.max_registry_events)
    return result
