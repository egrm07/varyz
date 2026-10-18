"""The current task's reported work, distinct from reusable completion standards."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from varyz.session_progress import HistoryQuery, history, pause_path


def current(
    conn: sqlite3.Connection, project: str | None, conversation_id: str | None, *, cfg: Any = None,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Read only the latest live checkpoint of this exact task, through privacy gates.

    A mined conversation, another task, or a withheld latest checkpoint must not
    become an executable checklist. Checkpoints remain agent reports, not proof.
    """
    if not project or not conversation_id:
        return {"status": "task_scope_required", "continue_work": False}
    if cfg is not None and pause_path(cfg.data_dir).exists():
        return {"status": "paused", "continue_work": False}
    page = history(conn, HistoryQuery(
        project=project, conversation_id=conversation_id, origin="agent_report", latest=True,
        limit=1,
    ), max_chars=max_chars, cfg=cfg)
    if not page["reports"]:
        status = ("withheld" if page["withheld_sensitive"] else
                  "budget_exceeded" if page["truncated"] else "not_recorded")
        return {"status": status,
                "continue_work": False}
    report = page["reports"][0]
    pending = report.get("pending", [])
    working = report["status"] == "working" and bool(pending)
    result = {
        "status": report["status"],
        "task": report["task"],
        "pending": pending,
        "reference": report["reference"],
        "agent": report["agent"],
        "continue_work": working,
        "ready_to_report": report["status"] != "working",
        "independently_verified": False,
        "how": (
            "This task is still recorded as working. Continue authorised unfinished work; "
            "a successful standards check alone does not finish the task. If all remaining "
            "work requires owner input, permission or an external change, record a blocked "
            "checkpoint naming the blockers and give a concise handoff. Do not bypass "
            "permissions, poll unchanged blockers or claim completion."
            if working else
            "The task is recorded as working without a remaining checklist. Verify its "
            "current state and record the unfinished work or an honest closed/blocked "
            "status before claiming completion."
            if report["status"] == "working" else
            "This is the agent's current-state report, not independently verified completion. "
            "A blocked task may need a handoff; do not restart it automatically or bypass "
            "its blockers."
        ),
    }
    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
        return {"status": "budget_exceeded", "continue_work": False}
    return result
