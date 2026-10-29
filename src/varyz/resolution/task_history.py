"""Bounded, extractive task history preflight; never a new owner decision."""

from __future__ import annotations

import re
from typing import Any

from varyz.resolution.questioning import HISTORY_POLICY

_SCAFFOLD_TEXT = """
a an the to for in on of and or with my me our your you it this that please
can could would should add make build create implement update change fix section
project task want need help about were was has have been we discussed
и в во на с со к ко по о об от до за из у а но или же ли не это эту этот эти
мне мой моя мои нам наш наша наше наши ты вы он она они его её их что как
пожалуйста можешь можно нужно надо сделай сделать добавь добавить исправь исправить
обнови обновить проект задача помоги хочу
"""
_SCAFFOLD = frozenset(_SCAFFOLD_TEXT.split())

# A task needs the owner's history only when it points back at earlier work: a
# past discussion, decision, session or promise. Everything else ("evaluate this
# job posting", "write this function") is answered from the request and the
# repository; a history search there costs the agent time and the owner's machine.
_PAST_WORK = re.compile(
    r"\b(?:continue|resume|pick (?:it |this |that )?up|carry on|left off|"
    r"last (?:time|session|week|night)|earlier (?:session|chat|discussion|plan)|"
    r"previously|previous (?:session|chat|discussion|conversation|attempt|task)|"
    r"as (?:we|i|you) (?:said|agreed|decided|discussed|planned)|"
    r"we (?:discussed|agreed|decided|talked|planned)|(?:you|we) promised|"
    r"(?:do you|you) remember|what did (?:we|i|you)|why did (?:we|i|you)|"
    r"the other (?:session|chat|agent|thread)|(?:in|from) chatgpt)\b"
    r"|продолж|раньше|ранее|прошл|предыдущ|как мы|обсуждали|договорил|мы решили|"
    r"помнишь|вспомни|напомни|в тот раз|на ч[её]м мы|где мы останов|друг(?:ой|ом) "
    r"(?:сессии|чате)",
    re.IGNORECASE,
)


def task_query(task: str, project: str | None) -> str:
    """Literal topic terms without request scaffolding or repeated project names."""
    ignored = _SCAFFOLD | set(re.findall(r"\w+", (project or "").casefold()))
    words = dict.fromkeys(re.findall(r"[\w'-]+", task[:8000].casefold()))
    return " ".join(word for word in words if len(word) > 1 and word not in ignored)[:1000]


def refers_to_past_work(task: str) -> bool:
    return bool(_PAST_WORK.search(task[:8000]))


def preflight(task: str, project: str | None) -> dict[str, Any]:
    """'required' only when the task points back at earlier work. 'optional'
    briefings leave the history section out, so agents do not search by reflex."""
    result: dict[str, Any] = {
        "status": "required",
        "project": project,
        "query": task_query(task, project),
        "evidence": [],
        "instruction": HISTORY_POLICY,
        "coverage": "The task refers to earlier work; its history has not been "
                    "retrieved yet.",
    }
    if task and not refers_to_past_work(task):
        result.update(status="optional", coverage=(
            "The task does not refer to earlier work: no history search is needed. "
            "Search only if you would otherwise ask the owner to clarify scope."))
    return result


def matched_excerpt(text: str, query: str, limit: int = 900) -> str:
    """Keep the matching passage when the source is a long conversation chunk."""
    if len(text) <= limit:
        return text
    terms = re.findall(r"[\w'-]+", query)
    match = re.search(r"\b(?:" + "|".join(map(re.escape, terms)) + r")\b", text, re.I)
    start = max(0, match.start() - 200) if match else 0
    return ("…" if start else "") + text[start:start + limit - 2] + "…"


def cloud_history(archive: Any, account: str, task: str, project: str) -> dict[str, Any]:
    """Search only the caller's approved snapshot; retain provenance and source links."""
    result = preflight(task, project)
    if not result["query"]:
        return {**result, "status": "topic_required"}
    found = archive.search(account, result["query"], project=project, limit=3)
    if found.get("paused"):
        return {**result, "status": "paused", "coverage": "Archive reading is paused."}
    evidence = []
    for hit in found.get("results", []):
        try:
            source = archive.fetch(account, hit["id"], project=project)
        except KeyError:  # Revocation between search and fetch must not disclose a stale hit.
            continue
        evidence.append({
            "id": source["id"], "title": source["title"], "url": source["url"],
            "text": matched_excerpt(hit["snippet"], result["query"]),
            "metadata": source["metadata"],
            "provenance": source["metadata"]["provenance"],
            "synced_at": source["metadata"]["synced_at"],
            "excerpt_only": True,
        })
    return {
        **result, "status": "checked", "evidence": evidence,
        "coverage": "Bounded search of approved synced project records only; "
                    "no matches does not mean no earlier discussion exists.",
        "next_action": "Read relevant excerpts and fetch useful source IDs for full context "
                       "before choosing scope or asking a clarification.",
    }


def local_history(conn: Any, cfg: Any, caller: Any, task: str, project: str | None,
                  cwd: Any, remaining_chars: int) -> dict[str, Any]:
    """No network/model startup. Disclose only project evidence covered by the grant."""
    import json

    from varyz.cloud.archive_sync import entry_project
    from varyz.db.dao import is_entry_excluded, row_to_entry
    from varyz.domain import SearchHit
    from varyz.resolution.availability import is_paused
    from varyz.resolution.knowledge import normalize_scope
    from varyz.retrieval.capsule import to_capsules
    from varyz.retrieval.search import fts_escape
    from varyz.trust import capability_limits

    result = preflight(task, project)
    if is_paused(cfg):
        return {**result, "status": "paused"}
    if caller.capability_id is None:
        return {**result, "status": "access_required"}
    grant = conn.execute(
        "SELECT resource_types_json, destinations_json FROM agent_capabilities WHERE id=?",
        (caller.capability_id,),
    ).fetchone()
    max_items, max_chars = capability_limits(conn, caller)
    budget = min(max_chars, remaining_chars, 2700)
    if (grant is None or "summary" not in json.loads(grant[0])
            or "agent" not in json.loads(grant[1]) or max_items <= 0 or budget < 100):
        return {**result, "status": "access_required"}
    if not project or not result["query"]:
        return {**result, "status": "topic_required"}
    scope = normalize_scope(project)
    projects = {scope: {"root": str(cwd) if cwd else None}}
    rows = conn.execute(
        """SELECT e.*, snippet(entries_fts, 1, '', '', '…', 64) AS snip,
                  bm25(entries_fts) AS rank
           FROM entries_fts JOIN entries e ON e.rowid=entries_fts.rowid
           WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts) LIMIT 200""",
        (fts_escape(result["query"]),),
    ).fetchall()
    hits = []
    withheld = 0
    for row in rows:
        if row["source"] in cfg.graph.excluded_sources:
            continue
        if row["source"] not in {"ai_conversations", "documents", "git", "github"}:
            continue
        if entry_project(row, projects) != scope:
            continue
        entry = row_to_entry(row)
        if is_entry_excluded(conn, entry.source, entry.source_account, entry.external_id):
            continue
        capsules, hidden = to_capsules(
            [SearchHit(entry=entry, score=-row["rank"], chunk=row["snip"])],
            max_chars=min(900, budget),
        )
        withheld += hidden
        if capsules:
            item = capsules[0].as_dict()
            size = len(json.dumps(item, ensure_ascii=False))
            if size > budget:
                break
            hits.append(item)
            budget -= size
        if len(hits) >= min(3, max_items) or budget < 100:
            break
    return {
        **result, "status": "withheld" if withheld else "checked",
        "evidence": [] if withheld else hits, "withheld_sensitive": withheld,
        "coverage": "Bounded search of permitted local project records; not a complete history.",
    }
