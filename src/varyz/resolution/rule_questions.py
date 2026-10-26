"""One waiting rule, asked inside a task it bears on.

A reading leaves rules it was not sure enough of to put on trial. The owner
should not have to vet those on a page, out of context, in more detail than they
know; instead a task they are doing asks about one of them, once, in one plain
line, when the rule bears on that task. At most one per conversation and
``DAILY_CAP`` a day, none in away mode, and a rule the owner let pass is not
asked again for ``QUIET_DAYS``. The answer acts at once: yes keeps the rule, no
stops it, their own words keep it in those words.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from varyz.resolution.knowledge import GLOBAL_SCOPE
from varyz.resolution.principles import Principle, SQLitePrincipleStore, normalize_scope
from varyz.resolution.trials import MODEL_SOURCES

DAILY_CAP = 2
QUIET_DAYS = 14
# Shared words with the task, below which a rule does not bear on it.
MIN_SHARED = 2
ANSWERS = ("yes", "no", "reword")
HOW = (
    "Ask the owner this once in this task, with your question tool: together with "
    "questions you already have for them, else with your report. Never stop work "
    "just for it, and never answer it yourself. Record their reply with "
    "answer_rule_question: 'yes' keeps the rule, 'no' stops it, 'reword' with their "
    "words keeps it in those words. If they do not answer, record nothing."
)


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_questions'"
    ).fetchone() is not None


def _waiting(principle: Principle) -> bool:
    provenance = principle.provenance
    return (principle.source in MODEL_SOURCES and principle.kind == "rule"
            and not isinstance(provenance.get("workflow"), dict)
            and not isinstance(provenance.get("decision"), dict))


def _evidence(principle: Principle) -> int:
    quotes = principle.provenance.get("quotes")
    threads = {str(quote.get("thread") or quote.get("entry_id"))
               for quote in (quotes if isinstance(quotes, list) else ())
               if isinstance(quote, dict)}
    corrections = principle.provenance.get("corrections")
    return len(threads) + (len(corrections) if isinstance(corrections, list) else 0)


def question(principle: Principle, matched: tuple[str, ...] = ()) -> dict[str, Any]:
    """The question an agent puts to the owner about one waiting rule."""
    statement = " ".join(principle.statement.split())
    return {
        "id": principle.id,
        "rule": statement,
        "scope": "all projects" if principle.scope == GLOBAL_SCOPE else principle.scope,
        "question": f'Keep this as a rule: "{statement}"?',
        "answers": ["Yes", "No", "In my words"],
        "bears_on": list(matched[:4]),
        "how": HOW,
    }


def pick(conn: sqlite3.Connection, task: str, *, project: str | None,
         conversation_id: str | None, now: datetime) -> dict[str, Any] | None:
    """The waiting rule to ask about in this task, or None.

    None when the task has no conversation id, this conversation already asked
    one, ``DAILY_CAP`` were asked in the last day, or no waiting rule shares
    ``MIN_SHARED`` words with the task in its scope.
    """
    if not conversation_id or not _has_table(conn):
        return None
    if conn.execute("SELECT 1 FROM rule_questions WHERE conversation_id=? LIMIT 1",
                    (conversation_id,)).fetchone():
        return None
    day = (now - timedelta(days=1)).isoformat()
    asked_today = conn.execute(
        "SELECT COUNT(*) FROM rule_questions WHERE event='asked' AND recorded_at>=?", (day,)
    ).fetchone()[0]
    if asked_today >= DAILY_CAP:
        return None
    quiet = (now - timedelta(days=QUIET_DAYS)).isoformat()
    passed = {row[0] for row in conn.execute(
        "SELECT principle_id FROM rule_questions WHERE event<>'asked' OR recorded_at>=?",
        (quiet,))}
    store = SQLitePrincipleStore(conn)
    waiting = [rule for rule in store.current("candidate")
               if _waiting(rule) and rule.id not in passed]
    if not waiting:
        return None
    from varyz.resolution.rule_selection import select

    selection = select(
        waiting, task, project=project, limit=len(waiting),
        known_projects={rule.scope for rule in store.current("confirmed")},
        normalize_scope=normalize_scope,
    )
    bearing = [(rule, matched) for rule, matched in selection.chosen
               if len(matched) >= MIN_SHARED]
    if not bearing:
        return None
    # Most shared words first, then the rule the reading saw most often.
    rule, matched = max(bearing, key=lambda pair: (len(pair[1]), _evidence(pair[0])))
    return question(rule, matched)


def open_question(conn: sqlite3.Connection, conversation_id: str | None) -> dict[str, Any] | None:
    """The question this conversation asked and the owner has not answered yet."""
    if not conversation_id or not _has_table(conn):
        return None
    row = conn.execute(
        """SELECT principle_id FROM rule_questions AS asked
           WHERE conversation_id=? AND event='asked' AND NOT EXISTS (
               SELECT 1 FROM rule_questions AS answer
               WHERE answer.principle_id=asked.principle_id AND answer.event<>'asked')""",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    principle = SQLitePrincipleStore(conn).get(str(row[0]))
    if principle is None or principle.status != "candidate":
        return None
    return question(principle)


def record(conn: sqlite3.Connection, principle_id: str, conversation_id: str, event: str, *,
           project: str | None, stamp: str) -> bool:
    """Keep that a rule was asked about or answered; False when it already was."""
    if event not in ("asked", *ANSWERS):
        raise ValueError("event is 'asked', 'yes', 'no' or 'reword'")
    if not conversation_id:
        raise ValueError("conversation_id is required")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO rule_questions VALUES (?,?,?,?,?)",
        (principle_id, conversation_id, project, event, stamp),
    )
    conn.commit()
    return cursor.rowcount > 0


__all__ = ["ANSWERS", "DAILY_CAP", "HOW", "MIN_SHARED", "QUIET_DAYS", "open_question", "pick",
           "question", "record"]
