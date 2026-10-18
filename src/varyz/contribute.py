"""Anonymized examples for the Varyz network, sent only after the owner opts in.

Built from the archive's resolution tables: how agents asked, what they were
told, how the owner corrected them and which rules the owner kept. Everything
is scrubbed on this machine first and the scrubber fails closed: an example
that still holds a credential, or that Varyz withheld from an agent, is never
queued. Chats, files, sources, vision notes and the archive itself never leave.

Consent, pause and the local queue live in the analytics state file, so the one
popup and ``varyz analytics consent`` govern counts and contributions together.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from varyz import __version__
from varyz.analytics import ORIGIN, _audit, _get, _put, connection, state_path
from varyz.analytics_schema import CONSENT_VERSION
from varyz.config import Config
from varyz.contribution_schema import MAX_TEXT, Contribution, ContributionBatch
from varyz.privacy.secrets_scan import find as find_credentials

ENDPOINT = ORIGIN + "/api/contributions"
BATCH = 200
_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b(?:https?|ftp|file|ssh)://\S+|\bwww\.\S+", re.I), "[link]"),
    (re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|]*"), "[path]"),
    (re.compile(r"(?<![\w/])(?:~|\.{1,2})?/(?:[\w.@-]+/)+[\w.@-]*"), "[path]"),
    (re.compile(r"\\\\[\w.-]+\\[^\s\"'<>|]*"), "[path]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[address]"),
    (re.compile(r"(?<![\w])\+?\d[\d ().-]{7,}\d\b"), "[number]"),
    (re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,}"), "[handle]"),
    (re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b"), "[id]"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "[id]"),
)
_AGENTS = ("claude", "codex", "chatgpt", "gemini", "cursor")


def _empty(value: Any) -> bool:
    return value in (None, "", "[]", "{}", "null")


def _agent(caller: str | None) -> str:
    caller = (caller or "").lower()
    if "openai" in caller or "gpt" in caller:
        return "chatgpt" if "chat" in caller else "codex"
    return next((name for name in _AGENTS if name in caller), "other")


class Scrubber:
    """Replaces identifiers with placeholders; ``None`` means drop the example."""

    def __init__(self, archive: sqlite3.Connection) -> None:
        names: set[str] = {Path.home().name}
        if _has(archive, "people"):
            for (name,) in archive.execute("SELECT canonical_name FROM people"):
                names.add(name or "")
                # Surnames and first names on their own, but never short or
                # lowercase words that would also match ordinary prose.
                names.update(t for t in (name or "").split() if len(t) >= 4 and t[0].isupper())
        if _has(archive, "person_aliases"):
            names.update(
                v for (v,) in archive.execute(
                    "SELECT alias_value FROM person_aliases WHERE alias_type='display_name'"
                )
            )
        projects: set[str] = set()
        if _has(archive, "resolution_ledger"):
            for (raw,) in archive.execute(
                "SELECT DISTINCT json_extract(request_json,'$.project') FROM resolution_ledger"
            ):
                projects.add(str(raw or ""))
        if _has(archive, "owner_principles"):
            projects.update(s for (s,) in archive.execute(
                "SELECT DISTINCT scope FROM owner_principles WHERE scope<>'*'"))
        self.terms = [
            (re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I), label)
            for terms, label in ((projects, "[project]"), (names, "[person]"))
            for term in sorted({t.strip() for t in terms if len(t.strip()) >= 3}, key=len,
                               reverse=True)
        ]

    def __call__(self, text: str | None) -> str | None:
        text = (text or "").strip()
        if not text:
            return ""
        if find_credentials(text):
            return None
        for pattern, label in (*_PATTERNS, *self.terms):
            text = pattern.sub(label, text)
        if find_credentials(text):
            return None
        return text[:MAX_TEXT]


def _has(archive: sqlite3.Connection, table: str) -> bool:
    return bool(archive.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def _day(stamp: str) -> str | None:
    day = str(stamp or "")[:10]
    return day if day >= (datetime.now(UTC).date() - timedelta(days=30)).isoformat() else None


def _seconds(start: str | None, end: str | None) -> int:
    try:
        delta = datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))
        return max(0, min(int(delta.total_seconds()), 31_536_000))
    except ValueError:
        return 0


def _queue(db: sqlite3.Connection, scrub: Scrubber, **fields: Any) -> bool:
    for key in ("question", "answer", "correction"):
        if key in fields:
            clean = scrub(fields[key])
            if clean is None:
                return False
            fields[key] = clean
    if not fields.get("question") and fields.get("kind") != "rule":
        return False
    try:
        record = Contribution(record_id=str(uuid4()), **fields)
    except ValidationError:
        return False  # Never queue what the receiver would refuse or a field it can't hold.
    db.execute("INSERT INTO contributions VALUES(?,?)", (record.record_id,
                                                          record.model_dump_json()))
    return True


def _rows(db, archive, key: str, sql: str, stamp: str) -> list[sqlite3.Row]:
    rows = archive.execute(sql + " ORDER BY t.rowid LIMIT 2000",
                           (int(_get(db, key, "0")), _get(db, "since"))).fetchall()
    if rows:
        _put(db, key, str(rows[-1]["row_id"]))
    return [r for r in rows if _day(r[stamp])]


def collect(db: sqlite3.Connection, cfg: Config) -> int:
    """Queue new examples created since the owner opted in. Returns how many."""
    if not cfg.db_path.exists():
        return 0
    archive = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    archive.row_factory = sqlite3.Row
    queued = 0
    try:
        scrub = Scrubber(archive)
        if _has(archive, "resolution_outcomes") and _has(archive, "resolution_ledger"):
            knowledge = _has(archive, "owner_knowledge")
            for row in _rows(db, archive, "c:outcomes", (
                "SELECT t.rowid AS row_id,t.outcome,t.recorded_at,l.question,l.answer,l.basis,"
                "l.tier,l.confidence,l.caller_agent_id,l.withheld_json,"
                + ("k.answer AS correction " if knowledge else "'' AS correction ")
                + "FROM resolution_outcomes t JOIN resolution_ledger l ON l.id=t.ledger_id "
                + ("LEFT JOIN owner_knowledge k ON k.id=t.knowledge_id " if knowledge else "")
                + "WHERE t.rowid>? AND t.recorded_at>=?"
            ), "recorded_at"):
                if not _empty(row["withheld_json"]):
                    continue  # Varyz kept part of this from the agent; it stays here too.
                queued += _queue(
                    db, scrub, kind="correction", day=_day(row["recorded_at"]),
                    agent=_agent(row["caller_agent_id"]), outcome=row["outcome"],
                    question=row["question"], answer=row["answer"],
                    correction=row["correction"] if row["outcome"] != "adopted" else "",
                    basis=row["basis"] or "", tier=int(row["tier"] or 0),
                    confidence=float(row["confidence"] or 0),
                )
        if _has(archive, "resolution_escalations") and _has(archive, "owner_knowledge"):
            for row in _rows(db, archive, "c:escalations", (
                "SELECT t.rowid AS row_id,t.question,t.caller_agent_id,t.tried_tiers_json,"
                "t.urgency,t.created_at,t.answered_at,k.answer FROM resolution_escalations t "
                "JOIN owner_knowledge k ON k.id=t.answer_knowledge_id "
                "WHERE t.rowid>? AND t.status='answered' AND t.answered_at>=?"
            ), "answered_at"):
                try:
                    tiers = [str(t) for t in json.loads(row["tried_tiers_json"] or "[]")]
                except (ValueError, TypeError):
                    tiers = []
                queued += _queue(
                    db, scrub, kind="escalation", day=_day(row["answered_at"]),
                    agent=_agent(row["caller_agent_id"]), outcome="answered",
                    question=row["question"], answer=row["answer"],
                    urgency=str(row["urgency"] or ""), tried_tiers=tiers[:8],
                    seconds_to_answer=_seconds(row["created_at"], row["answered_at"]),
                )
        if _has(archive, "owner_principles"):
            # Rules only: vision notes describe the owner's plans and never leave.
            for row in _rows(db, archive, "c:principles", (
                "SELECT t.rowid AS row_id,t.statement,t.scope,t.supersedes_id,t.created_at,"
                "t.caller_agent_id FROM owner_principles t WHERE t.rowid>? AND "
                "t.created_at>=? AND t.kind='rule' AND t.status='confirmed'"
            ), "created_at"):
                queued += _queue(
                    db, scrub, kind="rule", day=_day(row["created_at"]),
                    agent=_agent(row["caller_agent_id"]),
                    outcome="revised" if row["supersedes_id"] else "confirmed",
                    answer=row["statement"], rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                )
        if _has(archive, "owner_principle_retractions") and _has(archive, "owner_principles"):
            for row in _rows(db, archive, "c:retractions", (
                "SELECT t.rowid AS row_id,t.retracted_at,p.statement,p.scope,p.caller_agent_id "
                "FROM owner_principle_retractions t JOIN owner_principles p "
                "ON p.id=t.principle_id WHERE t.rowid>? AND t.retracted_at>=? "
                "AND p.kind='rule' AND t.reason IN ('rejected in review',"
                "'dropped in a spot-check of trial rules')"
            ), "retracted_at"):
                queued += _queue(
                    db, scrub, kind="rule", day=_day(row["retracted_at"]),
                    agent=_agent(row["caller_agent_id"]), outcome="rejected",
                    answer=row["statement"], rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                )
    finally:
        archive.close()
    db.execute(
        "DELETE FROM contributions WHERE json_extract(payload,'$.day')<?",
        ((datetime.now(UTC).date() - timedelta(days=30)).isoformat(),),
    )
    return queued


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=10, follow_redirects=False, trust_env=False)


def erase(cfg: Config, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Ask the server to delete what earlier opt-ins shared. Retried until done."""
    if not state_path(cfg).exists():
        return {"status": "none", "erased": 0}
    with connection(cfg) as db:
        db.execute("BEGIN IMMEDIATE")
        keys = _get(db, "erase_keys").split()
        if not keys:
            return {"status": "none", "erased": 0}
        left: list[str] = []
        with _client(transport) as client:
            for key in keys:
                try:
                    response = client.delete(ENDPOINT, headers={"Authorization": f"Bearer {key}"})
                    if response.status_code not in (200, 404):
                        raise httpx.HTTPStatusError("", request=response.request,
                                                    response=response)
                except httpx.HTTPError:
                    left.append(key)
        _put(db, "erase_keys", " ".join(left))
        _audit(db, "erase_requested" if not left else "erase_pending")
        return {"status": "erased" if not left else "erase_pending",
                "erased": len(keys) - len(left)}


def preview(cfg: Config, limit: int = 20) -> dict[str, Any]:
    """What the next upload would hold, without changing any state."""
    if not state_path(cfg).exists():
        return {"status": "off", "records": []}
    with connection(cfg) as db:
        db.execute("BEGIN IMMEDIATE")
        if _get(db, "consent") != CONSENT_VERSION:
            db.rollback()
            return {"status": "off", "records": []}
        collect(db, cfg)
        records = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM contributions ORDER BY rowid LIMIT ?", (limit,))]
        pending = db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]
        db.rollback()
        return {"status": "preview", "endpoint": ENDPOINT, "pending": pending,
                "records": records}


def sync(
    cfg: Config, *, force: bool = False, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    if not state_path(cfg).exists():
        return {"status": "off", "sent": 0}
    erase(cfg, transport=transport)
    with connection(cfg) as db:
        # Held until sending ends, so turning sharing off waits and then wins.
        db.execute("BEGIN IMMEDIATE")
        if _get(db, "consent") != CONSENT_VERSION:
            return {"status": "off", "sent": 0}
        if _get(db, "paused") == "1":
            return {"status": "paused", "sent": 0}
        now = time.time()
        if not force and now < float(_get(db, "contrib_next", "0")):
            return {"status": "not_due", "sent": 0}
        collect(db, cfg)
        _put(db, "contrib_next", str(now + 6 * 3600))
        rows = db.execute(
            "SELECT id,payload FROM contributions ORDER BY rowid LIMIT ?", (BATCH,)
        ).fetchall()
        if not rows:
            return {"status": "empty", "sent": 0}
        key = _get(db, "contributor_key")
        if not key:
            key = secrets.token_urlsafe(32)
            _put(db, "contributor_key", key)
        batch = ContributionBatch(
            batch_id=str(uuid4()), version=__version__,
            records=[Contribution.model_validate_json(r["payload"]) for r in rows],
        )
        with _client(transport) as client:
            for attempt in range(3):
                try:
                    response = client.post(ENDPOINT, json=batch.model_dump(),
                                           headers={"Authorization": f"Bearer {key}"})
                    response.raise_for_status()
                    if response.json().get("accepted") != batch.batch_id:
                        raise ValueError("invalid_receipt")
                    db.executemany("DELETE FROM contributions WHERE id=?",
                                   [(r["id"],) for r in rows])
                    _put(db, "contrib_last_delivery", datetime.now(UTC).isoformat())
                    _put(db, "contrib_last_error", "")
                    _audit(db, "contributions_delivered")
                    return {"status": "sent", "sent": len(rows)}
                except (httpx.HTTPError, ValueError):
                    if attempt < 2:
                        time.sleep(0.1 * 2**attempt + random.uniform(0, 0.1))
        _put(db, "contrib_last_error", "delivery_failed")  # Never log provider text.
        _put(db, "contrib_next", str(now + 3600))
        _audit(db, "contributions_failed")
        return {"status": "delivery_failed", "sent": 0}


def status(cfg: Config) -> dict[str, Any]:
    if not state_path(cfg).exists():
        return {"sharing": False, "pending": 0, "erase_pending": 0}
    with connection(cfg) as db:
        return {
            "sharing": _get(db, "consent") == CONSENT_VERSION,
            "paused": _get(db, "paused") == "1",
            "pending": db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0],
            "last_delivery": _get(db, "contrib_last_delivery"),
            "last_error": _get(db, "contrib_last_error"),
            "erase_pending": len(_get(db, "erase_keys").split()),
            "endpoint": ENDPOINT,
        }
