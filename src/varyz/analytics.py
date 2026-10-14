"""Consent-gated aggregate analytics. Personal content never enters a report."""

from __future__ import annotations

import contextlib
import json
import platform
import random
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx

from varyz import __version__
from varyz.analytics_schema import CONSENT_VERSION, METRICS, AggregateReport, OperatingSystem
from varyz.config import Config

ORIGIN = "https://cloud.varyzai.com"
CONSENT_MESSAGE = (
    "Hi, I'm Egor, the solo developer behind Varyz. Anonymous usage counts "
    "would really help me find problems and make Varyz better. "
    "I never receive or see your messages, questions, answers or learned rules through "
    "these reports."
)
CONSENT_COUNTS = (
    "Only counts are sent: answer outcomes, repeated questions, rule reviews, setup "
    "results, failures and response-time ranges, grouped by day, app version and "
    "operating system. No names, project names, file paths, account details or permanent "
    "user ID are included."
)
CONSENT_PRIVACY = (
    "My server sees your network address during delivery and uses it for temporary "
    "rate limits, so delivery isn't fully anonymous. Sharing is optional and off by "
    "default, needs no Cloud account, and can be turned off in setup or with "
    "varyz analytics consent off. Reports are kept for 30 days. Varyz works the same "
    "if you say no."
)
CONSENT_COPY = "\n\n".join((CONSENT_MESSAGE, CONSENT_COUNTS, CONSENT_PRIVACY))
TABLES = {
    "resolution_ledger": "resolved_at",
    "resolution_outcomes": "recorded_at",
    "owner_principles": "created_at",
    "owner_principle_retractions": "retracted_at",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(day TEXT,metric TEXT,value INTEGER NOT NULL,
 PRIMARY KEY(day,metric));
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS analytics_audit_no_update BEFORE UPDATE ON audit
 BEGIN SELECT RAISE(ABORT,'analytics audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS analytics_audit_no_delete BEFORE DELETE ON audit
 BEGIN SELECT RAISE(ABORT,'analytics audit is append-only'); END;
"""


def state_path(cfg: Config) -> Path:
    return cfg.data_dir / "analytics" / "analytics.sqlite3"


@contextlib.contextmanager
def connection(cfg: Config, *, timeout: float = 30) -> Iterator[sqlite3.Connection]:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def _get(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _put(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _audit(db: sqlite3.Connection, action: str) -> None:
    db.execute("INSERT INTO audit(at,action) VALUES(?,?)", (datetime.now(UTC).isoformat(), action))


def status(cfg: Config) -> dict[str, Any]:
    if not state_path(cfg).exists():
        return {"consent": False, "paused": False, "pending_reports": 0, "endpoint": ORIGIN}
    with connection(cfg) as db:
        return {
            "consent": _get(db, "consent") == CONSENT_VERSION,
            "paused": _get(db, "paused") == "1",
            "endpoint": ORIGIN,
            "pending_reports": db.execute("SELECT COUNT(*) FROM pending").fetchone()[0],
            "last_delivery": _get(db, "last_delivery"),
            "last_error": _get(db, "last_error"),
            "local_counters": dict(
                db.execute("SELECT metric,SUM(value) FROM counters GROUP BY metric")
            ),
            "consent_version": _get(db, "consent"),
        }


def consent(cfg: Config, enabled: bool) -> None:
    """A new opt-in starts from now. Off discards every unsent count and batch."""
    with connection(cfg) as db:
        db.execute("BEGIN IMMEDIATE")
        if enabled and _get(db, "consent") == CONSENT_VERSION:
            return
        db.execute("DELETE FROM counters")
        db.execute("DELETE FROM pending")
        db.execute("DELETE FROM state")
        _put(db, "consent", CONSENT_VERSION if enabled else "")
        _put(db, "since", datetime.now(UTC).isoformat())
        _audit(db, "consent_on" if enabled else "consent_off")


def pause(cfg: Config, paused: bool) -> None:
    with connection(cfg) as db:
        db.execute("BEGIN IMMEDIATE")
        _put(db, "paused", "1" if paused else "0")
        _audit(db, "paused" if paused else "resumed")


def _add(db: sqlite3.Connection, metric: str, count: int = 1, day: str | None = None) -> None:
    if metric not in METRICS or count <= 0:
        return
    db.execute(
        "INSERT INTO counters VALUES(?,?,?) ON CONFLICT(day,metric) "
        "DO UPDATE SET value=MIN(1000000,counters.value+excluded.value)",
        (day or datetime.now(UTC).date().isoformat(), metric, min(count, 1000000)),
    )


def record(cfg: Config, metric: str) -> None:
    """Best effort: analytics never fails a user's task; no arbitrary text accepted."""
    if not state_path(cfg).exists() or metric not in METRICS:
        return
    with contextlib.suppress(Exception), connection(cfg, timeout=0.05) as db:
        db.execute("BEGIN IMMEDIATE")
        if _get(db, "consent") == CONSENT_VERSION:
            _add(db, metric)


def latency(cfg: Config, elapsed: float) -> None:
    record(
        cfg,
        "latency_"
        + (
            "under_100ms"
            if elapsed < 0.1
            else "under_1s"
            if elapsed < 1
            else "under_5s"
            if elapsed < 5
            else "over_5s"
        ),
    )


def _collect(db: sqlite3.Connection, cfg: Config) -> None:
    """Read at most 2,000 new rows per table. All text matching remains local."""
    if not cfg.db_path.exists():
        return
    archive = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    archive.row_factory = sqlite3.Row
    try:
        for table, stamp in TABLES.items():
            columns = {
                "resolution_ledger": (
                    "disposition,basis,normalized_question,purpose,conversation_id,request_json"
                ),
                "resolution_outcomes": "outcome,source",
                "owner_principles": "status,supersedes_id,provenance_json",
                "owner_principle_retractions": "reason",
            }[table]
            if not archive.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            rows = archive.execute(
                f"SELECT rowid AS analytics_row,{stamp},{columns} FROM {table} "
                f"WHERE rowid>? AND {stamp}>=? ORDER BY rowid LIMIT 2000",
                (int(_get(db, table, "0")), _get(db, "since")),
            ).fetchall()
            for row in rows:
                day = str(row[stamp])[:10]
                if day < (datetime.now(UTC).date() - timedelta(days=30)).isoformat():
                    continue
                if table == "resolution_ledger":
                    _add(db, "answers_" + row["disposition"], day=day)
                    _add(db, "basis_" + (row["basis"] or ""), day=day)
                    # Retries in the same conversation are not repeated owner interruptions.
                    prior = archive.execute(
                        "SELECT conversation_id,request_json FROM resolution_ledger "
                        "WHERE normalized_question=? AND purpose=? AND rowid<? "
                        "AND resolved_at>=? ORDER BY rowid DESC LIMIT 1",
                        (
                            row["normalized_question"],
                            row["purpose"],
                            row["analytics_row"],
                            _get(db, "since"),
                        ),
                    ).fetchone()
                    if prior and row["disposition"] == "clarification":
                        current_project = json.loads(row["request_json"]).get("project")
                        old_project = json.loads(prior["request_json"]).get("project")
                        if (
                            current_project == old_project
                            and row["conversation_id"]
                            and (prior["conversation_id"] != row["conversation_id"])
                        ):
                            _add(db, "repeated_questions", day=day)
                elif table == "resolution_outcomes":
                    _add(db, "outcome_" + row["outcome"], day=day)
                    if row["outcome"] == "corrected" and row["source"].startswith(
                        "owner_feedback:"
                    ):
                        _add(db, "correction_" + row["source"].split(":", 1)[1], day=day)
                elif table == "owner_principles" and row["status"] == "confirmed":
                    provenance = json.loads(row["provenance_json"])
                    ended = provenance.get("trial_ended") or {}
                    if ended.get("how") == "kept":
                        _add(db, "trial_kept", day=day)
                    elif not provenance.get("trial"):
                        _add(
                            db,
                            "rules_revised" if row["supersedes_id"] else "rules_confirmed",
                            day=day,
                        )
                elif table == "owner_principle_retractions":
                    # Only human review rejects are learning-quality signals.
                    if row["reason"] == "dropped in a spot-check of trial rules":
                        _add(db, "trial_dropped", day=day)
                    elif row["reason"] == "rejected in review":
                        _add(db, "rules_rejected", day=day)
            if rows:
                _put(db, table, str(rows[-1]["analytics_row"]))
    finally:
        archive.close()


def _prepare(db: sqlite3.Connection, cfg: Config) -> None:
    cutoff = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    db.execute("DELETE FROM counters WHERE day<?", (cutoff,))
    db.execute("DELETE FROM pending WHERE json_extract(payload,'$.day')<?", (cutoff,))
    _collect(db, cfg)
    if db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] >= 30:
        return
    os_name = platform.system()
    for row in db.execute("SELECT DISTINCT day FROM counters ORDER BY day LIMIT 30").fetchall():
        counters = dict(db.execute("SELECT metric,value FROM counters WHERE day=?", (row[0],)))
        report = AggregateReport(
            report_id=str(uuid4()),
            day=row[0],
            version=__version__,
            os=cast(
                OperatingSystem, os_name if os_name in {"Windows", "Darwin", "Linux"} else "Other"
            ),
            counters=counters,
        )
        db.execute("INSERT INTO pending VALUES(?,?)", (report.report_id, report.model_dump_json()))
        db.execute("DELETE FROM counters WHERE day=?", (row[0],))


def sync(
    cfg: Config,
    *,
    dry_run: bool = False,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not state_path(cfg).exists():
        return {"status": "off", "sent": 0}
    with connection(cfg) as db:
        # Held until sending ends: off/pause waits, then guarantees no later send.
        db.execute("BEGIN IMMEDIATE")
        if _get(db, "consent") != CONSENT_VERSION:
            return {"status": "off", "sent": 0}
        if _get(db, "paused") == "1":
            return {"status": "paused", "sent": 0}
        now = time.time()
        if not dry_run and not force and now < float(_get(db, "next_attempt", "0")):
            return {"status": "not_due", "sent": 0}
        _prepare(db, cfg)
        reports = [
            AggregateReport.model_validate_json(row[0])
            for row in db.execute("SELECT payload FROM pending ORDER BY rowid LIMIT 1")
        ]
        if dry_run:
            preview = {
                "status": "preview",
                "sent": 0,
                "endpoint": ORIGIN,
                "reports": [report.model_dump() for report in reports],
            }
            db.rollback()
            return preview
        _put(db, "next_attempt", str(now + 86400))
        if not reports:
            return {"status": "empty", "sent": 0}
        report = reports[0]
        error = ""
        with httpx.Client(
            transport=transport, timeout=5, follow_redirects=False, trust_env=False
        ) as client:
            for attempt in range(3):
                try:
                    response = client.post(ORIGIN + "/api/analytics", json=report.model_dump())
                    response.raise_for_status()
                    if response.json() != {"accepted": report.report_id}:
                        raise ValueError("invalid_receipt")
                    db.execute("DELETE FROM pending WHERE id=?", (report.report_id,))
                    _put(db, "last_delivery", datetime.now(UTC).isoformat())
                    _put(db, "last_error", "")
                    _audit(db, "delivered")
                    return {"status": "sent", "sent": 1}
                except (httpx.HTTPError, ValueError):
                    error = "delivery_failed"  # Never log provider text or a URL.
                    if attempt < 2:
                        time.sleep(0.1 * 2**attempt + random.uniform(0, 0.1))
        _put(db, "last_error", error)
        _put(db, "next_attempt", str(now + 3600))
        _audit(db, "delivery_failed")
        return {"status": error, "sent": 0}


_tick_lock = threading.Lock()
_last_tick = 0.0


def tick(cfg: Config) -> None:
    """Run in the existing daemon; no new process, scheduler or terminal window."""
    global _last_tick
    if time.monotonic() - _last_tick < 60 or not state_path(cfg).exists():
        return
    if not _tick_lock.acquire(blocking=False):
        return
    _last_tick = time.monotonic()

    def work() -> None:
        try:
            sync(cfg)
        except Exception:
            record(cfg, "collection_failed")
        finally:
            _tick_lock.release()

    threading.Thread(target=work, name="varyz-analytics", daemon=True).start()


def doctor(cfg: Config) -> dict[str, Any]:
    result = status(cfg)
    if state_path(cfg).exists():
        with connection(cfg) as db:
            result["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
            result["audit_entries"] = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    result["ok"] = result.get("integrity", "ok") == "ok" and not result.get("last_error")
    result["delivery_verified"] = bool(result.get("last_delivery"))
    return result
