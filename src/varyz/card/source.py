"""What a Taste Card may be made of: the owner's own explicit rules, read-only.

Two ledgers hold them. ``owner_principles`` keeps the standing rules the owner
confirmed ("dry-run first"); ``owner_knowledge`` keeps the answers the owner gave
to agents' questions ("Which analytics? None."). Nothing else is read: no mail,
no chats, no files, no messages, no mined suggestion the owner never confirmed.

The database is opened with SQLite's read-only mode, so the card cannot write to
the owner's ledgers even by mistake. Reads are plain SQL over the columns the
migrations define, which keeps this module independent of the resolution
package's in-process API.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from varyz.scopes import stored_scope

GENERAL_SCOPE = "*"
# Scopes that hold other people's answers (an organisation's, a published taste).
# They are never the owner's own and never reach the card.
FOREIGN_SCOPE_PREFIXES = ("org:", "published:")
# The starter rules setup seeded until 2026-09-29: never the owner's own.
STARTER_SOURCE = "standards_seed"

Kind = Literal["rule", "vision", "answer"]


@dataclass(frozen=True)
class CardRule:
    """One rule the owner stated, exactly as the ledger holds it."""

    id: str
    text: str
    kind: Kind
    scope: str
    domain: str
    created_at: str
    # How many separate times the owner stated this same rule. 1 unless the
    # ledger holds the owner saying it again (a rule re-added after it was
    # withdrawn, an answer given again unchanged).
    stated: int = 1
    # The owner's own words without the agent's question: the statement of a
    # rule, the answer of an answer. How firm a rule sounds is read from this.
    core: str = ""

    @property
    def general(self) -> bool:
        return self.scope == GENERAL_SCOPE

    @property
    def words(self) -> str:
        return self.core or self.text


@dataclass(frozen=True)
class LedgerCounts:
    """Counts taken straight from the tables; nothing here is estimated."""

    rules: int


@dataclass(frozen=True)
class OwnerRules:
    rules: tuple[CardRule, ...]
    counts: LedgerCounts


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open the archive so that no statement on this connection can write."""
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _own_scope(scope: str) -> bool:
    return not scope.startswith(FOREIGN_SCOPE_PREFIXES)


def _normal(text: str) -> str:
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return folded.rstrip(".!;:").rstrip()


def answer_text(question: str, answer: str) -> str:
    """How an owner answer reads on its own: the question, then the answer."""
    question = " ".join(question.split())
    answer = " ".join(answer.split())
    if question and question[-1] not in "?:.!":
        question += ":"
    return f"{question} {answer}".strip()


def _principles(conn: sqlite3.Connection) -> list[CardRule]:
    if not _has_table(conn, "owner_principles"):
        return []
    columns = _columns(conn, "owner_principles")
    # Trial rows are stored as active/confirmed so agents can follow them, but
    # the owner has not kept them. Old automatic promotions were not consent
    # either. A shareable card must not present either as the owner's own rule.
    authority = (
        "AND json_extract(principle.provenance_json, '$.trial') IS NULL "
        "AND coalesce(json_extract(principle.provenance_json, '$.trial_ended.how'), '') "
        "!= 'promoted'"
        if "provenance_json" in columns else ""
    )
    domain = "principle.domain" if "domain" in columns else "''"
    retracted = (
        "AND principle.id NOT IN (SELECT principle_id FROM owner_principle_retractions)"
        if _has_table(conn, "owner_principle_retractions")
        else ""
    )
    said: dict[tuple[str, str], int] = {
        (str(row[0]), str(row[1])): int(row[2])
        for row in conn.execute(
            f"""SELECT normalized_statement, scope, count(*) FROM owner_principles AS principle
               WHERE status = 'confirmed' AND source != ? {authority}
               GROUP BY normalized_statement, scope""",
            (STARTER_SOURCE,),
        )
    }
    rows = conn.execute(
        f"""SELECT principle.id, principle.statement, principle.normalized_statement,
                   principle.kind, principle.scope, {domain} AS domain,
                   principle.created_at
            FROM owner_principles AS principle
            WHERE principle.status = 'confirmed' AND principle.source != ?
              {authority}
              AND principle.id NOT IN (SELECT supersedes_id FROM owner_principles
                                       WHERE supersedes_id IS NOT NULL)
              {retracted}
            ORDER BY principle.created_at, principle.monotonic_seq""",
        (STARTER_SOURCE,),
    ).fetchall()
    return [
        CardRule(
            id=str(row["id"]),
            text=" ".join(str(row["statement"]).split()),
            kind="vision" if row["kind"] == "vision" else "rule",
            scope=stored_scope(str(row["scope"])),
            domain=str(row["domain"] or ""),
            created_at=str(row["created_at"]),
            stated=said.get((str(row["normalized_statement"]), str(row["scope"])), 1),
        )
        for row in rows
        if _own_scope(stored_scope(str(row["scope"])))
    ]


def _answers(conn: sqlite3.Connection) -> list[CardRule]:
    if not _has_table(conn, "owner_knowledge"):
        return []
    columns = _columns(conn, "owner_knowledge")
    scope = "knowledge.scope" if "scope" in columns else "'*'"
    origin = "knowledge.origin" if "origin" in columns else "'own'"
    retracted = (
        "AND knowledge.id NOT IN (SELECT knowledge_id FROM owner_knowledge_retractions)"
        if _has_table(conn, "owner_knowledge_retractions")
        else ""
    )
    history: dict[tuple[str, str, str], int] = {}
    for row in conn.execute(
        f"""SELECT knowledge.normalized_question, {scope}, knowledge.answer
            FROM owner_knowledge AS knowledge WHERE {origin} = 'own'"""
    ):
        key = (str(row[0]), str(row[1]), _normal(str(row[2])))
        history[key] = history.get(key, 0) + 1
    rows = conn.execute(
        f"""SELECT knowledge.id, knowledge.question, knowledge.normalized_question,
                   knowledge.answer, {scope} AS scope, knowledge.created_at
            FROM owner_knowledge AS knowledge
            WHERE {origin} = 'own'
              AND knowledge.id NOT IN (SELECT supersedes_id FROM owner_knowledge
                                       WHERE supersedes_id IS NOT NULL)
              {retracted}
            ORDER BY knowledge.created_at, knowledge.monotonic_seq"""
    ).fetchall()
    return [
        CardRule(
            id=str(row["id"]),
            text=answer_text(str(row["question"]), str(row["answer"])),
            kind="answer",
            core=" ".join(str(row["answer"]).split()),
            scope=stored_scope(str(row["scope"] or GENERAL_SCOPE)),
            domain="",
            created_at=str(row["created_at"]),
            stated=history.get(
                (str(row["normalized_question"]), str(row["scope"] or GENERAL_SCOPE),
                 _normal(str(row["answer"]))),
                1,
            ),
        )
        for row in rows
        if _own_scope(stored_scope(str(row["scope"] or GENERAL_SCOPE)))
    ]


def read_owner_rules(conn: sqlite3.Connection) -> OwnerRules:
    """Every current rule and answer of the owner's own, oldest first, plus the count."""
    rules = tuple(_principles(conn) + _answers(conn))
    return OwnerRules(rules=rules, counts=LedgerCounts(rules=len(rules)))


def load_owner_rules(db_path: Path) -> OwnerRules:
    conn = open_read_only(db_path)
    try:
        return read_owner_rules(conn)
    finally:
        conn.close()


__all__ = [
    "GENERAL_SCOPE",
    "CardRule",
    "LedgerCounts",
    "OwnerRules",
    "answer_text",
    "load_owner_rules",
    "open_read_only",
    "read_owner_rules",
]
