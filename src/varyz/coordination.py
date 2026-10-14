"""Explicit task ownership across local and hosted MCP connections.

The host acknowledges the same selection on each connected surface. This module
never contacts another backend, copies task text, or expands an access grant.
"""

from __future__ import annotations

from typing import Any

from varyz.scopes import normalize_scope

WORKFLOW_TOOLS = frozenset({
    "start_task", "ask", "finish_check", "owner_questions", "answer_owner_question",
    "remember_owner_rule", "remember_owner_workflow", "away", "resolve", "advise",
    "record_workflow_nudge", "answer_workflow_review",
})

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "conversation_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "action": {"type": "string", "enum": ["status", "select"]},
        "backend": {"type": "string", "enum": ["local", "cloud"]},
        "expected_revision": {"type": "integer", "minimum": 0},
        "reason": {"type": "string", "enum": [
            "initial", "transport_unavailable", "explicit_handoff",
        ]},
    },
    "required": ["project", "conversation_id"],
    "additionalProperties": False,
}

ROUTE_DESCRIPTION = (
    "Coordinate local and hosted Varyz for one task. Prefer a working authorized local "
    "connection; otherwise use an already authorized hosted connection. Select the same "
    "backend on both surfaces using the same project and conversation_id, and wait for "
    "both acknowledgments before workflow calls. A status call is read-only. To hand off, "
    "read status and pass expected_revision and reason. Never switch because an answer "
    "is unknown, empty, withheld, denied or conflicting. Keep replies and pending IDs "
    "on their originating backend; do not repeat writes after an ambiguous timeout. "
    "This does not authenticate, upload, sync or automatically invoke another server."
)


def initialize(db: Any) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS varyz_task_routes ("
        "namespace TEXT NOT NULL, project TEXT NOT NULL, conversation_id TEXT NOT NULL, "
        "backend TEXT NOT NULL, revision INTEGER NOT NULL, "
        "PRIMARY KEY(namespace, project, conversation_id))"
    )


def task_identity(arguments: dict[str, Any]) -> tuple[str, str] | None:
    task = arguments.get("conversation_id")
    if task is None:
        return None
    if not isinstance(task, str) or not task.strip() or len(task) > 128:
        raise ValueError("conversation_id must be a nonblank string of at most 128 characters")
    project = arguments.get("project")
    if not isinstance(project, str) or not project.strip() or len(project) > 128:
        raise ValueError("project is required when coordinating a task")
    return normalize_scope(project), task


def status(db: Any, namespace: str, arguments: dict[str, Any], surface: str) -> dict[str, Any]:
    identity = task_identity(arguments)
    if identity is None:
        raise ValueError("conversation_id is required")
    project, task = identity
    row = db.execute(
        "SELECT backend,revision FROM varyz_task_routes "
        "WHERE namespace=? AND project=? AND conversation_id=?",
        (namespace, project, task),
    ).fetchone()
    return {
        "surface": surface, "project": project, "conversation_id": task,
        "workflow_backend": row["backend"] if row else surface,
        "revision": row["revision"] if row else 0,
        "selected": bool(row),
        "coordination": "host_acknowledges_both_surfaces",
    }


def route(db: Any, namespace: str, arguments: dict[str, Any], surface: str) -> dict[str, Any]:
    if set(arguments) - set(ROUTE_SCHEMA["properties"]):
        raise ValueError("Unexpected routing arguments")
    current = status(db, namespace, arguments, surface)
    action = arguments.get("action", "status")
    if action == "status":
        return current
    if action != "select" or arguments.get("backend") not in {"local", "cloud"}:
        raise ValueError("Select requires backend local or cloud")
    backend = arguments["backend"]
    expected = arguments.get("expected_revision", 0)
    if type(expected) is not int or expected < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    reason = arguments.get("reason", "initial")
    if reason not in {"initial", "transport_unavailable", "explicit_handoff"}:
        raise ValueError("An unknown, denied or withheld answer is not a handoff reason")
    # Retries of a confirmed selection are harmless, including a lost ack.
    if current["selected"] and current["workflow_backend"] == backend:
        return {**current, "status": "selected"}
    if expected != current["revision"] or (current["selected"] and reason == "initial"):
        return {**current, "status": "conflict", "hint": "Read both routes before handing off"}
    key = (namespace, current["project"], current["conversation_id"])
    if current["selected"]:
        result = db.execute(
            "UPDATE varyz_task_routes SET backend=?,revision=revision+1 "
            "WHERE namespace=? AND project=? AND conversation_id=? AND revision=?",
            (backend, *key, expected),
        )
    else:
        result = db.execute(
            "INSERT INTO varyz_task_routes VALUES(?,?,?,?,1) "
            "ON CONFLICT(namespace,project,conversation_id) DO NOTHING",
            (*key, backend),
        )
    fresh = status(db, namespace, arguments, surface)
    return {**fresh, "status": "selected" if result.rowcount == 1 else "conflict"}


def guard(
    db: Any, namespace: str, arguments: dict[str, Any], surface: str, tool: str,
) -> dict[str, Any] | None:
    # A handoff cannot orphan an already asked question. Each server still
    # validates its own pending identifier and the owner's actual reply.
    if tool == "answer_owner_question":
        return None
    if tool not in WORKFLOW_TOOLS or arguments.get("conversation_id") is None:
        return None
    current = status(db, namespace, {
        **arguments, "project": arguments.get("project") or "*",
    }, surface)
    if current["workflow_backend"] == surface:
        return None
    return {
        "status": "delegated", "routing": current,
        "hint": "Use the selected backend for this task's workflow; this call did no work",
        "definition_of_done": {}, "standing_rules": [], "owner_questions": [],
    }
