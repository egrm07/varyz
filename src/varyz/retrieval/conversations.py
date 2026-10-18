"""Find what the archive's conversations concluded, without quoting them.

This is the one retrieval path in varyz that returns no excerpt. Everything
else answers with the archive's own words and a reference the owner can open;
here the words are other people's messages, and the whole reason the pass that
produced these sentences exists is so that the knowledge in a chat history can
be used without the chat history being sent anywhere.

So an insight carries a conclusion and an address -- which conversation, in
which thread, between which dates, over how many turns -- and the owner can
open that conversation on their own machine. The messages themselves stay
there. This mirrors what the Mirror already does with the owner's own writing:
the conclusion and its citation travel, the wording never does.

The sensitivity gate is unchanged and still fail-closed. A conclusion drawn
from a conversation the model called sensitive carries that label, and is
withheld from any caller not allowed to read sensitive material -- because a
conclusion about a diagnosis is a disclosure of the diagnosis, whoever
paraphrased it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from varyz.domain import Sensitivity
from varyz.retrieval.capsule import DEFAULT_SENSITIVITY
from varyz.retrieval.search import fts_escape


@dataclass(frozen=True, slots=True)
class ConversationInsight:
    """One conclusion, and the conversation it can be checked against."""

    conclusion_id: str
    window_id: str
    text: str
    kind: str
    sensitivity: str
    source: str
    thread_id: str | None
    started_at: str
    ended_at: str
    turns: int


def search(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int,
    allowed: tuple[Sensitivity, ...] = DEFAULT_SENSITIVITY,
) -> tuple[list[ConversationInsight], int]:
    """Conclusions matching the question, best first, plus a withheld count.

    Over-fetches, because the gate is applied after ranking: a sensitive
    conclusion that outranks an allowed one must not also take its place in the
    result. The count of what was held back is returned rather than hidden, so
    the caller can say that the archive has an answer it may not read, which is
    a different and more useful thing to report than having no answer.
    """
    if not _has_conclusions(conn):
        return [], 0
    match = fts_escape(question)
    if not match:
        return [], 0
    rows = conn.execute(
        "SELECT c.id, c.window_id, c.text, c.kind, c.sensitivity,"
        "       w.source, w.thread_id, w.started_at, w.ended_at, w.turns"
        " FROM conversation_conclusions_fts f"
        " JOIN conversation_window_conclusions c ON c.rowid = f.rowid"
        " JOIN conversation_windows w ON w.id = c.window_id"
        " WHERE conversation_conclusions_fts MATCH ?"
        " ORDER BY bm25(conversation_conclusions_fts) LIMIT ?",
        (match, max(limit * 4, limit)),
    ).fetchall()

    names = {str(level) for level in allowed}
    insights: list[ConversationInsight] = []
    withheld = 0
    for row in rows:
        if row["sensitivity"] not in names:
            withheld += 1
            continue
        if len(insights) >= limit:
            continue
        insights.append(
            ConversationInsight(
                conclusion_id=row["id"],
                window_id=row["window_id"],
                text=row["text"],
                kind=row["kind"],
                sensitivity=row["sensitivity"],
                source=row["source"],
                thread_id=row["thread_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                turns=row["turns"],
            )
        )
    return insights, withheld


def _has_conclusions(conn: sqlite3.Connection) -> bool:
    """Whether this archive has been read at all.

    An archive whose conversation pass has never run has the tables, from the
    migration, and nothing in them. Asking first keeps every caller from having
    to know that, and keeps the absence of a pass from looking like a question
    with no answer.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM conversation_window_conclusions LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # An archive older than the migration. Nothing to report, and nothing
        # broken: the pass has simply never existed here.
        return False
    return row is not None


def payload(insight: ConversationInsight) -> dict[str, Any]:
    """The wire shape. No excerpt field, by construction rather than by policy.

    There is deliberately nowhere in this dictionary for a message to go. A
    later change that wanted to include one would have to add the field, which
    is a change somebody reviews, rather than widen a value nobody looks at.
    """
    return {
        "conclusion": insight.text,
        "kind": insight.kind,
        "source": insight.source,
        "thread": insight.thread_id,
        "occurred": insight.started_at,
        "until": insight.ended_at,
        "turns": insight.turns,
        "sensitivity": insight.sensitivity,
        "reference": insight.window_id,
        "conclusion_id": insight.conclusion_id,
        "quoted": False,
    }
