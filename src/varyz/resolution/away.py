"""Away state and deferred reviews. Intent and technical judgment belong to the agent."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from varyz.domain import iso, utc_now
from varyz.resolution.knowledge import OWNER_ID, normalize_scope

RECOGNITION = (
    "The working agent (you), never a local classifier, recognises explicit owner "
    "instructions such as '/away', 'keep working while I am away', or 'do not interrupt; "
    "I will review later'. Call away(action='on') with their actual words. Default to "
    "scope='task', using the conversation_id from this briefing (or create a unique "
    "task id once and reuse it). Pass it to ask, start_task and finish_check. Use "
    "project/all scope only if explicitly requested. 'I am busy', silence, quoted "
    "examples and requests to implement away mode are not activation. On 'I am back' "
    "or '/away off', call away(action='off') for the activated scope and review the "
    "queue. A status question does not end away mode. End task-scoped away mode with "
    "away(action='complete') only when that task is finished or abandoned."
)

AWAY_POLICY = (
    "When away_mode.active is true: defer owner questions, scope questions, workflow reviews "
    "and optional memory confirmations until the owner returns; do not invoke a "
    "question UI or repeat reminders. Keep doing useful work in the authorised task. "
    "Use recorded constraints first. Exercise professional judgment on implementation "
    "details already delegated by the task or the owner's expressed trust: language, "
    "libraries, algorithms, tests and internal architecture need not become owner "
    "questions merely because they matter. Prefer the existing stack. Do not infer "
    "delegation just from describing someone as nontechnical. For a delegated choice "
    "use ask(decision_kind='delegated') to check known constraints without creating "
    "a new owner question; a missing preference is yours to decide, not an outage or "
    "a withheld answer to bypass. For owner-specific unknowns, use a coherent, easily "
    "replaceable provisional choice and attach proposed_choice and reason to ask. "
    "Never save provisional choices as owner answers or treat silence as approval. "
    "If a missing goal or permission would make downstream work wasteful, mark the "
    "question blocking or permission, pause that branch and work on independent "
    "parts. Do not build multiple speculative versions, poll for answers, broaden "
    "the task or keep busy after useful work is exhausted. Existing authorisation "
    "still applies; away mode grants no extra spending, publishing, destructive "
    "actions or private-data access. Before an upload, push, install, network request "
    "or sandbox escalation, check both existing owner authorisation and whether the "
    "client will require an interactive approval. Reuse explicit approval already "
    "given for that action and destination; do not ask again. Do not invoke an "
    "operation that requires an interactive approval while away, even as the final "
    "step. Queue missing owner permission with ask(decision_kind='permission'); "
    "record client approval blockers separately from owner consent. Prepare the "
    "artifact and exact blocked action, then continue all independent implementation, "
    "offline checks and review. Use an authorised noninteractive alternative when "
    "available; never weaken client security or broaden access. When useful work "
    "is exhausted, leave a handoff with completed work and prepared blockers, keep "
    "away active and the task incomplete, and request approvals together only after "
    "the owner explicitly returns. Do not end the whole task at the first blocked "
    "branch, poll for approval or claim that away mode keeps a stopped client running. "
    "Leave a concise handoff when useful work is "
    "done. On return, review blockers first, group related preferences with their "
    "drafts, and accept 'you decide' without requiring an opinion on everything."
)


def _project(project: str | None) -> str:
    return normalize_scope(project) if project else ""


def _key(scope: str, project: str | None, conversation_id: str | None) -> tuple[str, str, str]:
    if scope not in {"task", "project", "all"}:
        raise ValueError("scope must be task, project or all")
    if scope != "all" and (not isinstance(project, str) or not project.strip()):
        raise ValueError("project is required for task or project scope")
    if scope == "task" and (
        not isinstance(conversation_id, str) or not conversation_id.strip()
        or len(conversation_id) > 200
    ):
        raise ValueError("conversation_id is required for task scope; reuse the task's id")
    return scope, _project(project) if scope != "all" else "", (
        conversation_id or "" if scope == "task" else ""
    )


def status(
    conn: sqlite3.Connection, project: str | None, conversation_id: str | None = None,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """The narrowest live setting wins; a task can return while other agents stay away."""
    now = now or utc_now()
    keys = [("all", "", "")]
    if project:
        keys.insert(0, ("project", _project(project), ""))
        if conversation_id:
            keys.insert(0, ("task", _project(project), conversation_id))
    for key in keys:
        try:
            row = conn.execute(
                "SELECT * FROM away_modes WHERE scope=? AND project=? AND conversation_id=?",
                key,
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: away_modes" in str(exc):
                break  # read-only hooks can run before the first migrated MCP call
            raise
        if row is None or (row["expires_at"] and row["expires_at"] <= iso(now)):
            continue
        return {
            "active": bool(row["active"]), "scope": row["scope"],
            "project": project, "conversation_id": conversation_id,
            "expires_at": row["expires_at"],
            "how": AWAY_POLICY if row["active"] else "Owner is available in this scope.",
        }
    return {"active": False, "project": project, "conversation_id": conversation_id}


def set_mode(
    conn: sqlite3.Connection, *, active: bool, scope: str, project: str | None,
    conversation_id: str | None, instruction: str, duration_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist an explicit instruction, without interpreting its wording locally."""
    key = _key(scope, project, conversation_id)
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        raise ValueError("instruction must contain the owner's words (1-2000 characters)")
    if duration_minutes is not None and (
        type(duration_minutes) is not int or not 1 <= duration_minutes <= 10080
    ):
        raise ValueError("duration_minutes must be between 1 and 10080")
    now = now or utc_now()
    expires = iso(now + timedelta(minutes=duration_minutes)) if duration_minutes else None
    with conn:
        # A new explicit wider instruction supersedes earlier narrower settings.
        if scope == "all":
            conn.execute("DELETE FROM away_modes")
        elif scope == "project":
            conn.execute("DELETE FROM away_modes WHERE project=?", (_project(project),))
        conn.execute(
            """INSERT INTO away_modes VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, project, conversation_id) DO UPDATE SET
               active=excluded.active, instruction=excluded.instruction,
               updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
            (*key, int(active), instruction.strip(), iso(now), expires),
        )
        if active:
            # Questions raised before the owner left belong in the return review too.
            for row in conn.execute(
                "SELECT id, project, conversation_id FROM resolution_escalations "
                "WHERE status='pending' AND recipient_id=?", (OWNER_ID,),
            ).fetchall():
                if scope != "all" and _project(row["project"]) != _project(project):
                    continue
                if scope == "task" and row["conversation_id"] != conversation_id:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO away_reviews "
                    "(escalation_id, project, conversation_id) VALUES (?, ?, ?)",
                    (row["id"], _project(row["project"]), row["conversation_id"] or ""),
                )
    return status(conn, project, conversation_id, now=now)


def review_note(
    conn: sqlite3.Connection, escalation_id: str, project: str | None,
    conversation_id: str | None, *, kind: str = "preference",
    proposed_choice: str = "", reason: str = "",
) -> dict[str, Any]:
    """Keep agent assumptions beside a queued question, never in confirmed knowledge."""
    if kind not in {"preference", "blocking", "permission"}:
        raise ValueError("invalid review kind")
    with conn:
        conn.execute(
            """INSERT INTO away_reviews VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(escalation_id, project, conversation_id) DO UPDATE SET
               kind=CASE WHEN excluded.kind='preference' THEN away_reviews.kind
                         ELSE excluded.kind END,
               proposed_choice=COALESCE(NULLIF(excluded.proposed_choice, ''),
                                        away_reviews.proposed_choice),
               reason=COALESCE(NULLIF(excluded.reason, ''), away_reviews.reason)""",
            (escalation_id, _project(project), conversation_id or "", kind,
             proposed_choice, reason),
        )
        row = conn.execute(
            "SELECT * FROM away_reviews WHERE escalation_id=? AND project=? AND conversation_id=?",
            (escalation_id, _project(project), conversation_id or ""),
        ).fetchone()
    return dict(row)


def review(
    conn: sqlite3.Connection, project: str | None, conversation_id: str | None = None,
    *, scope: str = "task", limit: int = 20,
) -> dict[str, Any]:
    """A bounded return review; answering or dismissing removes an item naturally."""
    clauses = ["e.status='pending'", "COALESCE(e.recipient_id, ?) = ?"]
    params: list[Any] = [OWNER_ID, OWNER_ID]
    if scope != "all":
        clauses.append("r.project=?")
        params.append(_project(project))
    if scope == "task":
        clauses.append("r.conversation_id=?")
        params.append(conversation_id or "")
    query = (
        " FROM away_reviews r JOIN resolution_escalations e ON e.id=r.escalation_id WHERE "
        + " AND ".join(clauses)
    )
    count = conn.execute("SELECT COUNT(*)" + query, params).fetchone()[0]
    rows = conn.execute(
        "SELECT r.*, e.question" + query
        + " ORDER BY CASE r.kind WHEN 'permission' THEN 0 WHEN 'blocking' THEN 1 ELSE 2 END,"
        " e.created_at, e.id LIMIT ?", (*params, limit),
    ).fetchall()
    return {"pending": count, "questions": [dict(row) for row in rows]}
