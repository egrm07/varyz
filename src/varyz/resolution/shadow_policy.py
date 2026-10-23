"""A's read-only state projection and B's conservative task compiler.

No model, escalation, sync or ledger writes. Text exists only while compiling;
the journal receives IDs, hashes and state, never the prompt or rule wording.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from varyz.resolution.knowledge import _terms, normalize_question, normalize_scope

POLICY_VERSION = "ab-shadow-1"
_SENSITIVE = re.compile(
    r"\b(commit|push|publish|upload|deploy|delete|spend|pay|password|secret|"
    r"credential|private|permission|approval|consent|send|email|message)\w*\b", re.I
)
_RESTRICT = re.compile(r"\b(never|do not|don't|must not|no .{0,30}unless|ask before|"
                       r"without (?:asking|permission|approval|consent))\b", re.I)
_PRIMARY_RESTRICTION = re.compile(
    r"^(?:never|do not|don't)\s+(?:commit|push|publish|upload|deploy|delete|send|"
    r"share|spend|pay)\b", re.I
)


def _safe_trial_restriction(text: str) -> bool:
    # A deliberately narrow exception. A negation somewhere in a mixed grant
    # does not make that grant safe; compound clauses remain under review.
    return bool(_PRIMARY_RESTRICTION.match(text.strip())) and not any(
        separator in text.casefold() for separator in (",", ";", " and ", " but ", " then ")
    )


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def scope_key(value: str | None) -> str:
    return normalize_scope(value)


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    logical_id: str
    revision_hash: str
    record_type: Literal["rule", "vision", "decision", "workflow"]
    lifecycle: Literal["candidate", "trial", "active", "superseded", "retracted", "retired"]
    authority: Literal["recorded", "provisional", "unreviewed", "stated", "learned"]
    scope: str
    domain: str = ""
    source: str
    # Existing sync placement is independent of lifecycle and authority.
    sync_eligible: bool = False
    topic: str = ""
    strength: Literal["unknown", "default", "hard"] = "unknown"
    value: str = ""
    annotation_valid: bool = False
    critical: bool = False
    sensitive: bool = False
    text: str = ""
    question: str = ""

    def metadata(self) -> dict[str, Any]:
        return self.model_dump(exclude={"text", "question", "value", "topic"}) | {
            "topic_hash": digest(self.topic) if self.topic else None,
            "value_hash": digest(self.value) if self.value else None,
        }


def _rows(conn: sqlite3.Connection, table: str, max_records: int) -> list[dict[str, Any]]:
    # Table names are fixed internal constants, never caller input.
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY id LIMIT ?", (max_records + 1,))
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor]
    if len(rows) > max_records:
        raise ValueError("shadow source limit reached")
    return rows


def project_records(conn: sqlite3.Connection, *, max_records: int = 10000) -> list[Record]:
    # Nested read savepoint: one consistent snapshot without committing or
    # rolling back any transaction belonging to the live caller.
    conn.execute("SAVEPOINT varyz_shadow_read")
    try:
        return _project_records(conn, max_records=max_records)
    finally:
        conn.execute("RELEASE SAVEPOINT varyz_shadow_read")


def _project_records(conn: sqlite3.Connection, *, max_records: int) -> list[Record]:
    """Replay existing ledger lineage; do not invent owner confirmation from exposure."""
    records: list[Record] = []
    for table, retract_table, target, category in (
        ("owner_principles", "owner_principle_retractions", "principle_id", "rule"),
        ("owner_knowledge", "owner_knowledge_retractions", "knowledge_id", "decision"),
        ("owner_workflows", "owner_workflow_retractions", "workflow_id", "workflow"),
    ):
        rows = _rows(conn, table, max_records)
        parents = {row["id"]: row.get("supersedes_id") for row in rows}
        superseded = set(parents.values()) - {None}
        retracted = {row[0] for row in conn.execute(f"SELECT {target} FROM {retract_table}")}
        for row in rows:
            identity = row["id"]
            root, seen = identity, set()
            while parents.get(root):
                if root in seen:
                    raise ValueError("shadow source has cyclic lineage")
                seen.add(root)
                root = parents[root]
            provenance = json.loads(row.get("provenance_json") or "{}")
            if not isinstance(provenance, dict):
                provenance = {}
            trial = provenance.get("trial")
            ended = provenance.get("trial_ended")
            ended = ended if isinstance(ended, dict) else {}
            authority = "recorded"
            lifecycle = "active"
            if row.get("status") == "candidate":
                lifecycle, authority = "candidate", "unreviewed"
            elif trial or ended.get("how") == "promoted":
                lifecycle, authority = "trial", "provisional"
            if category == "workflow":
                authority = "stated" if row["basis"] == "stated" else "learned"
            if identity in superseded:
                lifecycle = "superseded"
            if identity in retracted:
                lifecycle = "retracted"
            if row.get("source") == "standards_seed":
                lifecycle = "retired"
            annotation = provenance.get("shadow_policy")
            valid = (
                isinstance(annotation, dict)
                and annotation.get("strength") in {"hard", "default"}
                and isinstance(annotation.get("topic"), str) and bool(annotation["topic"])
                and isinstance(annotation.get("value"), str) and bool(annotation["value"])
            )
            text = row.get("statement") or row.get("answer") or row.get("name") or ""
            question = row.get("question", "")
            sensitive = bool(_SENSITIVE.search(text + " " + question))
            records.append(Record(
                id=identity, logical_id=f"{category}:{root}", revision_hash=digest(row),
                record_type=row.get("kind", category), lifecycle=lifecycle,
                authority=authority, scope=scope_key(row.get("scope")),
                domain=row.get("domain", ""), source=row["source"],
                sync_eligible=lifecycle == "active" and authority in {"recorded", "stated"},
                topic=annotation["topic"] if valid else "",
                value=annotation["value"] if valid else "",
                strength=annotation["strength"] if valid else "unknown",
                annotation_valid=bool(valid), sensitive=sensitive,
                critical=(bool(_RESTRICT.search(text))
                          or (valid and annotation["strength"] == "hard")),
                text=text, question=question,
            ))
    return records


def packet_ids(packet: dict[str, Any]) -> list[str]:
    keys = ("writing_rules", "message_rules", "how_owner_works", "project_vision",
            "project_rules", "standing_rules",
            "owner_workflows", "project_decisions", "general_preferences")
    ids = {str(item["id"]) for key in keys for item in packet.get(key, []) if "id" in item}
    checklist = packet.get("definition_of_done", [])
    if isinstance(checklist, dict):
        checklist = [item for items in checklist.values() for item in items]
    ids.update(str(item["id"]) for item in checklist if "id" in item)
    return sorted(ids)


def compile_packet(
    records: list[Record], task: str, project: str | None, domains: set[str], *, limit: int = 12
) -> dict[str, Any]:
    """B: working-project scope, pinned restrictions, typed conflicts, bounded defaults."""
    project_scope = scope_key(project) if project else None
    allowed = [r for r in records if r.lifecycle in {"active", "trial"}
               and r.scope in {"*", project_scope}
               and (not r.domain or r.domain in domains)]
    quarantined = [r.id for r in allowed if
                   (r.authority == "provisional" and r.sensitive
                    and not _safe_trial_restriction(r.text))
                   or (r.record_type == "decision" and r.sensitive)]
    allowed = [r for r in allowed if r.id not in quarantined]
    groups: dict[str, list[Record]] = defaultdict(list)
    for record in allowed:
        if record.annotation_valid:
            groups[record.topic].append(record)
    conflicts, overridden = [], set()
    for group in groups.values():
        local = [r for r in group if r.scope == project_scope]
        if local and all(r.strength == "default" for r in group):
            overridden.update(r.id for r in group if r.scope == "*")
            group = local
        if len({r.value for r in group}) > 1:
            conflicts.append(sorted(r.id for r in group))
    conflicted = {identity for group in conflicts for identity in group}
    terms = _terms(task)
    ranked = sorted((r for r in allowed if r.id not in overridden | conflicted),
                    key=lambda r: (-len(terms & _terms(r.text + " " + r.question)),
                                   r.scope == "*", r.id))
    pinned = [r for r in ranked if r.critical and r.record_type in {"rule", "vision"}]
    checklists = [r for r in ranked if r.domain and r.record_type in {"rule", "vision"}]
    visions = [r for r in ranked if r.record_type == "vision" and r.scope != "*"]
    base = {r.id for r in pinned + checklists + visions}
    standing = [r for r in ranked if r.record_type in {"rule", "vision"}
                and r.id not in base][:max(0, limit - len(base))]
    decisions = [r for r in ranked if r.record_type == "decision"
                 and terms & _terms(r.question + " " + r.text)][:6]
    workflows = [r for r in ranked if r.record_type == "workflow"
                 and (r.domain in domains or terms & _terms(r.text))][:3]
    selected = sorted({r.id for r in
                       pinned + checklists + visions + standing + decisions + workflows})
    by_id = {r.id: r for r in records}
    return {
        "ids": selected, "pinned": sorted(r.id for r in pinned),
        "trials": sorted(i for i in selected if by_id[i].lifecycle == "trial"),
        "conflicts": conflicts, "quarantined": sorted(quarantined),
        "overridden_defaults": sorted(overridden),
        "unannotated": sorted(r.id for r in allowed if not r.annotation_valid),
        "estimated_text_chars": sum(len(by_id[i].text) + len(by_id[i].question) for i in selected),
    }


def compile_answer(
    records: list[Record], question: str, project: str | None, *, owner_answers_only: bool
) -> dict[str, Any]:
    """An exact-match control only. Permission requests always require fresh review.

    Never call retrieval/model tiers or reuse unbounded task permissions. Unknown
    questions are outside this shadow answer policy's coverage, not errors.
    """
    if owner_answers_only or _SENSITIVE.search(question):
        return {"disposition": "requires_owner", "ids": [], "answer_hash": None}
    scope = scope_key(project) if project else None
    matches = [r for r in records if r.record_type == "decision" and r.lifecycle == "active"
               and r.scope in {"*", scope} and not r.sensitive
               and normalize_question(r.question) == normalize_question(question)]
    local = [r for r in matches if r.scope == scope]
    matches = local or matches
    if not matches:
        return {"disposition": "outside_coverage", "ids": [], "answer_hash": None}
    if len({r.text for r in matches}) > 1:
        return {"disposition": "conflict", "ids": sorted(r.id for r in matches),
                "answer_hash": None}
    return {"disposition": "exact_recorded", "ids": sorted(r.id for r in matches),
            "answer_hash": digest(matches[0].text)}
