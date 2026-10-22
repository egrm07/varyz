"""Anonymized examples for the Varyz network, sent only after the owner opts in.

Built from the archive's resolution tables: how agents asked, what they were
told, how the owner corrected them, which rules the owner kept and whether agents
followed them. Everything is scrubbed on this machine first and the scrubber
fails closed: an example that still holds a credential, or that Varyz withheld
from an agent, is never queued. Chats, files, sources, vision notes and the
archive itself never leave.

Consent, pause and the local queue live in the analytics state file, so the one
popup and ``varyz analytics consent`` govern counts and contributions together.
"""

from __future__ import annotations

import hashlib
import hmac
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
from varyz.contribution_schema import (
    MAX_ITEM,
    MAX_NOTE,
    MAX_STEP,
    MAX_TEXT,
    Contribution,
    ContributionBatch,
)
from varyz.privacy.secrets_scan import find as find_credentials
from varyz.resolution.rule_signals import ask_label, guess_hit, lineage, model_name

ENDPOINT = ORIGIN + "/api/contributions"
BATCH = 200
# How long an answer waits for a label before it is sent without one.
UNLABELLED_AFTER = timedelta(days=3)
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
# Short paraphrases, cut to MAX_NOTE.
NOTES = ("agent_did", "wanted", "agent_guess", "proposed_choice")


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
        if _has(archive, "session_progress"):
            projects.update(s for (s,) in archive.execute(
                "SELECT DISTINCT project FROM session_progress"))
        if _has(archive, "task_recipes"):
            projects.update(s for (s,) in archive.execute(
                "SELECT DISTINCT scope FROM task_recipes WHERE scope<>'*'"))
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
    for key in ("question", "answer", "correction", *NOTES):
        if key in fields:
            clean = scrub(fields[key])
            if clean is None:
                return False
            fields[key] = clean[:MAX_NOTE] if key in NOTES else clean
    for key in ("done", "checks", "pending", "steps"):
        if key in fields:
            items = [scrub(item) for item in fields[key]]
            if any(item is None for item in items):
                return False
            if key == "steps":
                fields[key] = [item[:MAX_STEP] for item in items if item][:8]
            else:
                fields[key] = [item[:MAX_ITEM] for item in items if item][:6]
    if not fields.get("question") and fields.get("kind") not in ("rule", "check", "use", "recipe"):
        return False
    try:
        record = Contribution(record_id=str(uuid4()), **fields)
    except ValidationError:
        return False  # Never queue what the receiver would refuse or a field it can't hold.
    db.execute("INSERT INTO contributions VALUES(?,?)", (record.record_id,
                                                          record.model_dump_json()))
    return True


def _ref(db: sqlite3.Connection, value: str | None) -> str:
    """An opaque reference that links records from this opt-in and nothing else.

    Keyed by a secret made at opt-in, which turning sharing off or on again
    replaces, so the server can join a rule to its checks but can never recover
    an id or link two installations or two opt-ins."""
    if not value:
        return ""
    salt = _get(db, "ref_salt")
    if not salt:
        salt = secrets.token_hex(16)
        _put(db, "ref_salt", salt)
    return hmac.new(salt.encode(), str(value).encode(), hashlib.sha256).hexdigest()[:20]


def _columns(archive: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in archive.execute(f"PRAGMA table_info({table})")}


def _pair(provenance: str | None) -> tuple[str, str]:
    """The first correction behind a rule: what the agent did, what the owner wanted."""
    try:
        corrections = json.loads(provenance or "{}").get("corrections") or []
    except (ValueError, AttributeError):
        return "", ""
    for item in corrections:
        if isinstance(item, dict) and item.get("agent_did") and item.get("wanted"):
            return str(item["agent_did"]), str(item["wanted"])
    return "", ""


def _should_have_asked(archive: sqlite3.Connection, principle_id: str) -> bool:
    return _has(archive, "ask_feedback") and bool(archive.execute(
        "SELECT 1 FROM ask_feedback WHERE principle_id=? AND label='should_have_asked'",
        (principle_id,),
    ).fetchone())


def _label_source(source: str | None) -> str:
    source = source or ""
    if source.startswith("owner_feedback"):
        return "owner"
    if source == "supersession":
        return "owner_replaced"
    return "task_finished" if source == "task_finished" else "other"


def _rows(db, archive, key: str, sql: str, stamp: str) -> list[sqlite3.Row]:
    rows = archive.execute(sql + " ORDER BY t.rowid LIMIT 2000",
                           (int(_get(db, key, "0")), _get(db, "since"))).fetchall()
    if rows:
        _put(db, key, str(rows[-1]["row_id"]))
    return [r for r in rows if _day(r[stamp])]


def _asks(db: sqlite3.Connection, archive: sqlite3.Connection, scrub: Scrubber) -> int:
    """Questions the owner answered or dismissed: whether asking was needed, and how
    well their answer was guessed before they gave it.

    Keyed by when a question closed, not when it was asked, so one left open for a
    while still leaves once it is answered."""

    key = "c:asks"
    if not _get(db, key) and _get(db, "c:escalations"):
        # Upgraded from the per-row cursor: what that sent stays sent.
        _put(db, key, datetime.now(UTC).isoformat() + "|")
    after, _, after_id = _get(db, key, "|").partition("|")
    predictions = _has(archive, "answer_predictions")
    feedback = _has(archive, "ask_feedback")
    away = _has(archive, "away_reviews")
    rows = archive.execute(
        "SELECT t.id,t.question,t.caller_agent_id,t.tried_tiers_json,t.urgency,t.status,"
        + ("t.conversation_id," if "conversation_id" in _columns(
            archive, "resolution_escalations") else "NULL AS conversation_id,")
        + "t.created_at,t.answered_at,k.answer,"
        + ("p.model,p.agent_guess,p.memory_guess," if predictions
           else "'' AS model,'' AS agent_guess,'' AS memory_guess,")
        + ("a.kind AS decision_kind,a.proposed_choice," if away
           else "'' AS decision_kind,'' AS proposed_choice,")
        + ("f.label AS feedback " if feedback else "NULL AS feedback ")
        + "FROM resolution_escalations t "
        "LEFT JOIN owner_knowledge k ON k.id=t.answer_knowledge_id "
        + ("LEFT JOIN answer_predictions p ON p.escalation_id=t.id " if predictions else "")
        + ("LEFT JOIN ask_feedback f ON f.escalation_id=t.id " if feedback else "")
        + ("LEFT JOIN away_reviews a ON a.escalation_id=t.id " if away else "")
        + "WHERE t.status IN ('answered','expired') AND t.answered_at>=? "
        "AND (t.answered_at>? OR (t.answered_at=? AND t.id>?)) "
        "ORDER BY t.answered_at,t.id LIMIT 2000",
        (_get(db, "since"), after, after, after_id),
    ).fetchall()
    if rows:
        _put(db, key, f"{rows[-1]['answered_at']}|{rows[-1]['id']}")
    queued = 0
    for row in rows:
        if not _day(row["answered_at"]):
            continue
        answered = row["status"] == "answered"
        if answered and row["answer"] is None:
            continue
        try:
            tiers = [str(t) for t in json.loads(row["tried_tiers_json"] or "[]")]
        except (ValueError, TypeError):
            tiers = []
        guess, memory = row["agent_guess"] or "", row["memory_guess"] or ""
        queued += _queue(
            db, scrub, kind="escalation", day=_day(row["answered_at"]),
            agent=_agent(row["caller_agent_id"]),
            outcome="answered" if answered else "dismissed",
            question=row["question"], answer=row["answer"] or "",
            urgency=str(row["urgency"] or ""), tried_tiers=tiers[:8],
            seconds_to_answer=_seconds(row["created_at"], row["answered_at"]) if answered else 0,
            model=model_name(row["model"]),
            ask_label=ask_label(row["status"], row["answer"], row["feedback"]),
            label_source="owner" if answered or row["feedback"] else "other",
            agent_guess=guess,
            agent_guess_hit=guess_hit(guess, row["answer"]) if answered else "",
            memory_guess_hit=guess_hit(memory, row["answer"]) if answered else "",
            task_ref=_ref(db, row["conversation_id"]),
            # Asked while the owner was away: the agent's proposal against their answer.
            decision_kind=_decision_kind(row["decision_kind"]),
            proposed_choice=row["proposed_choice"] or "",
            proposed_hit=guess_hit(row["proposed_choice"], row["answer"]) if answered else "",
        )
    return queued


def _decision_kind(value: str | None) -> str:
    kind = str(value or "")
    return kind if kind in ("preference", "delegated", "blocking", "permission") else ""


def _conversation(task_key: str | None) -> str:
    """A briefing's task key names its conversation when it has one."""
    key = str(task_key or "")
    return key.removeprefix("conversation:")


def _answers(db: sqlite3.Connection, archive: sqlite3.Connection, scrub: Scrubber) -> int:
    """Answers Varyz gave that nobody labelled within UNLABELLED_AFTER.

    Sent without a label; a later adopted, overridden or corrected record for the
    same answer carries the same ``ledger_ref``, so the newest one wins."""
    cutoff = (datetime.now(UTC) - UNLABELLED_AFTER).isoformat()
    task = ("l.conversation_id" if "conversation_id" in _columns(archive, "resolution_ledger")
            else "NULL")
    labelled = ("AND NOT EXISTS (SELECT 1 FROM resolution_outcomes o WHERE o.ledger_id=l.id) "
                if _has(archive, "resolution_outcomes") else "")
    rows = archive.execute(
        "SELECT l.rowid AS row_id,l.id,l.question,l.answer,l.basis,l.tier,l.confidence,"
        f"l.caller_agent_id,l.withheld_json,l.resolved_at,{task} AS task "
        "FROM resolution_ledger l WHERE l.rowid>? AND l.resolved_at>=? AND l.resolved_at<? "
        + labelled + "ORDER BY l.rowid LIMIT 2000",
        (int(_get(db, "c:answers", "0")), _get(db, "since"), cutoff),
    ).fetchall()
    if rows:
        _put(db, "c:answers", str(rows[-1]["row_id"]))
    queued = 0
    for row in rows:
        if not _day(row["resolved_at"]) or not _empty(row["withheld_json"]):
            continue
        queued += _queue(
            db, scrub, kind="answer", day=_day(row["resolved_at"]),
            agent=_agent(row["caller_agent_id"]), outcome="unlabelled",
            question=row["question"], answer=row["answer"] or "",
            basis=row["basis"] or "", tier=int(row["tier"] or 0),
            confidence=float(row["confidence"] or 0),
            task_ref=_ref(db, row["task"]), ledger_ref=_ref(db, row["id"]),
        )
    return queued


def _decisions(db: sqlite3.Connection, archive: sqlite3.Connection, scrub: Scrubber) -> int:
    """Choices the owner made outside an escalation: picked in the agent's own
    question box or relayed by the agent. Only the owner's own, made on this
    computer: shared and Cloud-synced answers are sent by whoever made them."""
    columns = _columns(archive, "owner_knowledge")
    own = "AND t.origin='own' " if "origin" in columns else ""
    queued = 0
    for row in _rows(db, archive, "c:decisions", (
        "SELECT t.rowid AS row_id,t.question,t.answer,t.basis,t.supersedes_id,t.created_at,"
        "t.caller_agent_id FROM owner_knowledge t WHERE t.rowid>? AND t.created_at>=? "
        "AND t.escalation_id IS NULL AND t.source NOT LIKE 'cloud-sync:%' " + own
    ), "created_at"):
        queued += _queue(
            db, scrub, kind="decision", day=_day(row["created_at"]),
            agent=_agent(row["caller_agent_id"]),
            outcome="revised" if row["supersedes_id"] else "decided",
            question=row["question"], answer=row["answer"], basis=row["basis"] or "",
            label_source="owner",
        )
    return queued


def _facts(raw: Any) -> list[str]:
    return [str(item) for item in raw if isinstance(item, str)][:6] if isinstance(raw, list) else []


def _sessions(db: sqlite3.Connection, archive: sqlite3.Connection, scrub: Scrubber) -> int:
    """The working agent's own report of a task it finished, got blocked on or
    dropped. Live reports only: reports mined from chats stay here, as do ones
    marked sensitive, the project and the artifacts list."""
    queued = 0
    for row in _rows(db, archive, "c:sessions", (
        "SELECT t.rowid AS row_id,t.agent_id,t.conversation_id,t.task,t.status,t.payload_json,"
        "t.recorded_at FROM session_progress t WHERE t.rowid>? AND t.recorded_at>=? "
        "AND t.origin='agent_report' AND t.sensitivity<>'sensitive' "
        "AND t.status IN ('complete','blocked','abandoned')"
    ), "recorded_at"):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        queued += _queue(
            db, scrub, kind="session", day=_day(row["recorded_at"]),
            agent=_agent(row["agent_id"]), outcome=row["status"], question=row["task"],
            done=_facts(payload.get("done")), checks=_facts(payload.get("checks")),
            pending=_facts(payload.get("pending")),
            task_ref=_ref(db, row["conversation_id"]), label_source="agent_report",
        )
    return queued


_RECIPE_OUTCOMES = {"worked": "passed", "failed": "failed"}


def _recipe_steps(raw: str | None) -> list[str]:
    try:
        steps = json.loads(raw or "[]")
    except ValueError:
        return []
    return [
        str(s.get("step", "")) + (f": {s['how']}" if s.get("how") else "")
        for s in steps if isinstance(s, dict) and s.get("step")
    ][:8] if isinstance(steps, list) else []


def _recipes(db: sqlite3.Connection, archive: sqlite3.Connection, scrub: Scrubber) -> int:
    """How agents learned to do a task, and whether the recipe held up next time.

    A recipe goes as written, scrubbed; a later use goes as references and its
    outcome. Recipes that arrived by Cloud sync came from another installation
    and are not counted twice. A 'changed' use is the corrected recipe's own record.
    """
    queued = 0
    for row in _rows(db, archive, "c:recipes", (
        "SELECT t.rowid AS row_id,t.id,t.name,t.scope,t.steps_json,t.check_text,t.pitfalls,"
        "t.supersedes_id,t.provenance_json,t.caller_agent_id,t.created_at "
        "FROM task_recipes t WHERE t.rowid>? AND t.created_at>=? AND t.device_id<>'cloud-sync'"
    ), "created_at"):
        try:
            conversation = json.loads(row["provenance_json"] or "{}").get("conversation_id")
        except (ValueError, AttributeError):
            conversation = None
        queued += _queue(
            db, scrub, kind="recipe", day=_day(row["created_at"]),
            agent=_agent(row["caller_agent_id"]),
            outcome="revised" if row["supersedes_id"] else "confirmed",
            question=row["name"], answer=row["check_text"], correction=row["pitfalls"],
            steps=_recipe_steps(row["steps_json"]),
            rule_scope="general" if row["scope"] == "*" else "project",
            rule_ref=_ref(db, row["id"]), replaces_ref=_ref(db, row["supersedes_id"]),
            task_ref=_ref(db, conversation), label_source="agent_report",
        )
    for row in _rows(db, archive, "c:recipe_uses", (
        "SELECT t.rowid AS row_id,t.recipe_id,t.outcome,t.note,t.conversation_id,"
        "t.caller_agent_id,t.recorded_at,r.scope FROM task_recipe_outcomes t "
        "JOIN task_recipes r ON r.id=t.recipe_id WHERE t.rowid>? AND t.recorded_at>=? "
        "AND t.device_id<>'cloud-sync' AND t.outcome IN ('worked','failed')"
    ), "recorded_at"):
        queued += _queue(
            db, scrub, kind="recipe", day=_day(row["recorded_at"]),
            agent=_agent(row["caller_agent_id"]), outcome=_RECIPE_OUTCOMES[row["outcome"]],
            correction=row["note"] if row["outcome"] == "failed" else "",
            rule_scope="general" if row["scope"] == "*" else "project",
            rule_ref=_ref(db, row["recipe_id"]), task_ref=_ref(db, row["conversation_id"]),
            label_source="agent_report",
        )
    return queued


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
            task = ("l.conversation_id" if "conversation_id" in _columns(
                archive, "resolution_ledger") else "NULL")
            for row in _rows(db, archive, "c:outcomes", (
                "SELECT t.rowid AS row_id,t.outcome,t.recorded_at,t.source,l.id AS ledger_id,"
                "l.question,l.answer,"
                f"l.basis,l.tier,l.confidence,l.caller_agent_id,l.withheld_json,{task} AS task,"
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
                    task_ref=_ref(db, row["task"]), label_source=_label_source(row["source"]),
                    ledger_ref=_ref(db, row["ledger_id"]),
                )
        if _has(archive, "resolution_ledger"):
            queued += _answers(db, archive, scrub)
        if _has(archive, "resolution_escalations") and _has(archive, "owner_knowledge"):
            queued += _asks(db, archive, scrub)
        if _has(archive, "owner_knowledge") and {
            "question", "escalation_id", "source", "created_at"
        } <= _columns(archive, "owner_knowledge"):
            queued += _decisions(db, archive, scrub)
        if _has(archive, "owner_principles"):
            # Rules only: vision notes describe the owner's plans and never leave.
            for row in _rows(db, archive, "c:principles", (
                "SELECT t.rowid AS row_id,t.id,t.statement,t.scope,t.supersedes_id,t.created_at,"
                + ("t.provenance_json," if "provenance_json" in _columns(
                    archive, "owner_principles") else "NULL AS provenance_json,")
                + "t.caller_agent_id FROM owner_principles t WHERE t.rowid>? AND "
                "t.created_at>=? AND t.kind='rule' AND t.status='confirmed'"
            ), "created_at"):
                queued += _queue(
                    db, scrub, kind="rule", day=_day(row["created_at"]),
                    agent=_agent(row["caller_agent_id"]),
                    outcome="revised" if row["supersedes_id"] else "confirmed",
                    answer=row["statement"], rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                    rule_ref=_ref(db, row["id"]), replaces_ref=_ref(db, row["supersedes_id"]),
                    label_source="owner",
                    ask_label="should_have_asked" if _should_have_asked(archive, row["id"])
                    else "",
                    **lineage(archive, row["id"]),
                    **dict(zip(("agent_did", "wanted"), _pair(row["provenance_json"]),
                               strict=True)),
                )
        if _has(archive, "owner_principle_retractions") and _has(archive, "owner_principles"):
            for row in _rows(db, archive, "c:retractions", (
                "SELECT t.rowid AS row_id,t.retracted_at,t.reason,p.id,p.statement,p.scope,"
                "p.caller_agent_id "
                "FROM owner_principle_retractions t JOIN owner_principles p "
                "ON p.id=t.principle_id WHERE t.rowid>? AND t.retracted_at>=? "
                "AND p.kind='rule' AND t.reason IN ('rejected in review',"
                "'dropped in a spot-check of trial rules','Withdrawn in Varyz Cloud')"
            ), "retracted_at"):
                queued += _queue(
                    db, scrub, kind="rule", day=_day(row["retracted_at"]),
                    agent=_agent(row["caller_agent_id"]), outcome="rejected",
                    answer=row["statement"], rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                    rule_ref=_ref(db, row["id"]), label_source="owner",
                    # Withdrawn: the owner took back a rule they had kept, in Cloud.
                    basis="withdrawn" if row["reason"] == "Withdrawn in Varyz Cloud"
                    else "review",
                )
        if _has(archive, "rule_checks") and _has(archive, "owner_principles"):
            # Whether a confirmed rule held in a task. Only clear results leave:
            # not-applicable and unchecked stay here, and so does the evidence.
            # The agent and model that did the task; before 0050, the rule's author.
            worker = ("t.agent,t.model," if "model" in _columns(archive, "rule_checks")
                      else "'' AS agent,'' AS model,")
            arms = _has(archive, "rule_exposures")
            for row in _rows(db, archive, "c:checks", (
                "SELECT t.rowid AS row_id,t.principle_id,t.conversation_id,t.status,"
                f"t.independently_verified,t.attempt,t.checked_at,{worker}p.statement,p.scope,"
                "p.caller_agent_id,(SELECT b.status FROM rule_checks b WHERE "
                "b.principle_id=t.principle_id AND b.conversation_id=t.conversation_id "
                "AND b.attempt=t.attempt-1) AS previous,"
                + ("(SELECT x.arm FROM rule_exposures x WHERE x.principle_id=t.principle_id "
                   "AND x.conversation_id=t.conversation_id) AS arm "
                   if arms else "'' AS arm ")
                + "FROM rule_checks t JOIN owner_principles p "
                "ON p.id=t.principle_id WHERE t.rowid>? AND t.checked_at>=? "
                "AND p.kind='rule' AND p.status='confirmed' "
                "AND t.status IN ('passed','failed','corrected')"
            ), "checked_at"):
                queued += _queue(
                    db, scrub, kind="check", day=_day(row["checked_at"]),
                    agent=_agent(row["agent"] or row["caller_agent_id"]), outcome=row["status"],
                    model=model_name(row["model"]),
                    answer=row["statement"], rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                    rule_ref=_ref(db, row["principle_id"]),
                    task_ref=_ref(db, row["conversation_id"]),
                    attempt=min(int(row["attempt"] or 0), 100),
                    previous=row["previous"] or "",
                    arm=row["arm"] or "",
                    label_source="owner" if row["status"] == "corrected"
                    else "inspected" if row["independently_verified"] else "agent_report",
                )
        if _has(archive, "owner_principle_uses") and _has(archive, "owner_principles"):
            # Which confirmed rule a task's briefing showed: no text, only references,
            # so it joins the rule's own record and the task's checks.
            for row in _rows(db, archive, "c:uses", (
                "SELECT t.rowid AS row_id,t.principle_id,t.task_key,t.shown_at,p.scope "
                "FROM owner_principle_uses t JOIN owner_principles p ON p.id=t.principle_id "
                "WHERE t.rowid>? AND t.shown_at>=? AND p.kind='rule' AND p.status='confirmed'"
            ), "shown_at"):
                queued += _queue(
                    db, scrub, kind="use", day=_day(row["shown_at"]), agent="other",
                    outcome="shown", rule_kind="rule",
                    rule_scope="general" if row["scope"] == "*" else "project",
                    rule_ref=_ref(db, row["principle_id"]),
                    task_ref=_ref(db, _conversation(row["task_key"])), label_source="other",
                )
        if _has(archive, "session_progress"):
            queued += _sessions(db, archive, scrub)
        if _has(archive, "task_recipes") and _has(archive, "task_recipe_outcomes"):
            queued += _recipes(db, archive, scrub)
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
