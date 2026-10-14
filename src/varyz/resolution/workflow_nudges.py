"""What the owner answers when an agent raises a workflow step they usually settle first.

The owner is right about the task in front of them: an agent that points out an
open earlier step does what it was asked anyway. What varyz keeps is the answer.
'took' means they settled the earlier step first; 'skipped' means they went on.
A skip that is followed, once the skipped step is settled, by the skipped-to step
being redone is recorded as 'rework' against that skip: the evidence that the
step was worth raising, which the next nudge quotes.

Skips with no rework after them are the other signal: a step the owner keeps
passing over at no cost may not belong in the order. Varyz never removes it on
its own. After ``REVIEW_AFTER_SKIPS`` such skips within ``REVIEW_WINDOW`` (a
week) it asks the owner once whether to keep the step, make it optional (still
in the order, never raised as open) or drop it; 'optional' and 'drop' record the
owner's changed order as a new version of the workflow. Skips older than the
window expire, so a step skipped now and then stays in the order.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.resolution.knowledge import (
    _iso,
    _next_monotonic_seq,
    _write_transaction,
    normalize_scope,
)
from varyz.resolution.models import Caller
from varyz.resolution.workflows import (
    REVIEW_AFTER_SKIPS,
    REVIEW_WINDOW,
    NudgeHistory,
    OpenSkip,
    SQLiteWorkflowStore,
    Workflow,
    normalize_name,
)

ANSWERS = ("took", "skipped", "rework")
DECISIONS = ("keep", "optional", "drop")


class SQLiteNudgeStore:
    """The owner's answers to workflow nudges, and their reviews of skipped steps."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Any = utc_now) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.RLock()
        self._workflows = SQLiteWorkflowStore(conn, clock=clock)

    # -- writes ---------------------------------------------------------------

    def record_answer(
        self,
        workflow_id: str,
        *,
        steps: Sequence[str],
        answer: str,
        caller: Caller,
        device_id: str,
        task_step: str = "",
        project: str | None = None,
    ) -> list[str]:
        """One row per earlier step the owner took or skipped; their ids."""
        if answer not in ("took", "skipped"):
            raise ValueError("answer is 'took' or 'skipped' ('rework' names a skip_id)")
        with _write_transaction(self._conn, self._lock):
            workflow = self._workflow(workflow_id)
            names = self._steps_of(workflow, steps)
            return [
                self._insert(
                    workflow_id=workflow.id, step=name, task_step=task_step,
                    project=project, answer=answer, skip_id=None,
                    caller=caller, device_id=device_id,
                )
                for name in names
            ]

    def record_rework(
        self, skip_id: str, *, caller: Caller, device_id: str
    ) -> str:
        """The skipped-to step was redone after the skipped step was settled."""
        with _write_transaction(self._conn, self._lock):
            row = self._conn.execute(
                "SELECT * FROM owner_workflow_nudges WHERE id = ?", (skip_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown skip: {skip_id}")
            if row["answer"] != "skipped":
                raise ValueError(f"{skip_id} is not a skip")
            done = self._conn.execute(
                "SELECT id FROM owner_workflow_nudges WHERE skip_id = ?", (skip_id,)
            ).fetchone()
            if done is not None:
                return str(done[0])
            return self._insert(
                workflow_id=row["workflow_id"], step=row["step"],
                task_step=row["task_step"], project=row["project"] or None,
                answer="rework", skip_id=skip_id, caller=caller, device_id=device_id,
            )

    def review(
        self,
        workflow_id: str,
        *,
        step: str,
        decision: str,
        caller: Caller,
        device_id: str,
        source: str,
    ) -> dict[str, Any]:
        """The owner's answer about a step they keep skipping; the order they keep."""
        if decision not in DECISIONS:
            raise ValueError("decision is 'keep', 'optional' or 'drop'")
        with _write_transaction(self._conn, self._lock):
            workflow = self._workflow(workflow_id)
            (name,) = self._steps_of(workflow, [step])
            wanted = normalize_name(name)
            done = self._conn.execute(
                """SELECT decision, result_workflow_id FROM owner_workflow_reviews
                   WHERE workflow_id = ? AND normalized_step = ?""",
                (workflow.id, wanted),
            ).fetchone()
            if done is not None:
                return {
                    "decision": done["decision"],
                    "workflow_id": done["result_workflow_id"] or workflow.id,
                    "already_answered": True,
                }
            # The owner answers about the order agents are following now; a
            # replaced version's steps must not overwrite what came after it.
            workflow = self._workflows._current_row(workflow.id)
            result_id: str | None = None
            if decision != "keep":
                if decision == "drop":
                    kept = [s for s in workflow.steps if normalize_name(s.step) != wanted]
                else:
                    kept = [
                        replace(s, optional=True) if normalize_name(s.step) == wanted else s
                        for s in workflow.steps
                    ]
                if len(kept) < 2:
                    raise ValueError(
                        "a workflow needs at least two steps; withdraw it with "
                        "`varyz workflows retract` instead"
                    )
                result_id = self._workflows.record(
                    name=workflow.name, steps=kept, caller=caller, device_id=device_id,
                    source=source, scope=workflow.scope, domain=workflow.domain,
                    rationale=workflow.rationale,
                    provenance={
                        **dict(workflow.provenance),
                        "reviewed": {"step": name, "decision": decision,
                                     "from": workflow.id},
                    },
                )
            seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """INSERT INTO owner_workflow_reviews
                   (id, workflow_id, step, normalized_step, decision, result_workflow_id,
                    caller_agent_id, caller_display_name, caller_purpose,
                    caller_capability_id, caller_device_id, caller_transport,
                    device_id, monotonic_seq, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id(), workflow.id, name, wanted, decision, result_id,
                    caller.agent_id, caller.display_name, caller.purpose,
                    caller.capability_id, caller.device_id, caller.transport,
                    device_id, seq, _iso(self._clock()),
                ),
            )
            return {"decision": decision, "workflow_id": result_id or workflow.id,
                    "already_answered": False}

    # -- reads ----------------------------------------------------------------

    def history(self, workflow: Workflow, project: str | None = None) -> NudgeHistory:
        """Answers over every version of this workflow (its name and scope)."""
        rows = self._lineage_rows(workflow)
        last_review: dict[str, str] = {}
        reviewed: set[str] = set()
        for review in self._conn.execute(
            """SELECT review.workflow_id, review.normalized_step, review.reviewed_at
               FROM owner_workflow_reviews AS review
               JOIN owner_workflows AS workflow ON workflow.id = review.workflow_id
               WHERE workflow.normalized_name = ? AND varyz_scope(workflow.scope) = ?""",
            (normalize_name(workflow.name), workflow.scope),
        ):
            step = review["normalized_step"]
            last_review[step] = max(last_review.get(step, ""), review["reviewed_at"])
            if review["workflow_id"] == workflow.id:
                reviewed.add(step)
        reworked_skips = {row["skip_id"] for row in rows if row["answer"] == "rework"}
        week_start = _iso(self._clock() - REVIEW_WINDOW)
        skipped: Counter[str] = Counter()
        reworked: Counter[str] = Counter()
        open_skips: list[OpenSkip] = []
        scope = normalize_scope(project) if project else ""
        for row in rows:
            if row["answer"] != "skipped":
                continue
            step = row["normalized_step"]
            if row["recorded_at"] > last_review.get(step, ""):
                if row["recorded_at"] >= week_start:
                    skipped[step] += 1
                if row["id"] in reworked_skips:
                    reworked[step] += 1
            if scope and row["project"] == scope and row["id"] not in reworked_skips:
                open_skips.append(OpenSkip(
                    row["id"], step, normalize_name(row["task_step"] or "")
                ))
        return NudgeHistory(
            skipped=dict(skipped), reworked=dict(reworked),
            reviewed=frozenset(reviewed), open_skips=tuple(open_skips),
        )

    def counts(self) -> list[dict[str, Any]]:
        """Per current workflow step: how often it was raised, taken, skipped, redone."""
        out: list[dict[str, Any]] = []
        for workflow in self._workflows.current():
            tally: dict[str, Counter[str]] = {}
            for row in self._lineage_rows(workflow):
                tally.setdefault(row["normalized_step"], Counter())[row["answer"]] += 1
            history = self.history(workflow)
            for step in workflow.steps:
                name = normalize_name(step.step)
                seen = tally.get(name, Counter())
                if not seen and not step.optional:
                    continue
                out.append({
                    "workflow_id": workflow.id,
                    "workflow": workflow.name,
                    "scope": workflow.scope,
                    "step": step.step,
                    "optional": step.optional,
                    "took": seen["took"],
                    "skipped": seen["skipped"],
                    "rework": seen["rework"],
                    "skips_this_week": history.skipped.get(name, 0),
                    "reviewed": name in history.reviewed,
                    "review_due": (
                        not step.optional
                        and history.skipped.get(name, 0) >= REVIEW_AFTER_SKIPS
                        and not history.reworked.get(name)
                        and name not in history.reviewed
                    ),
                })
        return out

    # -- internals ------------------------------------------------------------

    def _workflow(self, workflow_id: str) -> Workflow:
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            raise KeyError(f"unknown workflow: {workflow_id}")
        return workflow

    @staticmethod
    def _steps_of(workflow: Workflow, steps: Iterable[str]) -> list[str]:
        """The workflow's own spelling of each named step; unknown names refused."""
        by_name = {normalize_name(step.step): step.step for step in workflow.steps}
        names: list[str] = []
        for step in steps:
            name = by_name.get(normalize_name(str(step)))
            if name is None:
                raise ValueError(
                    f"'{step}' is not a step of the {workflow.name} workflow "
                    f"({', '.join(by_name.values())})"
                )
            if name not in names:
                names.append(name)
        if not names:
            raise ValueError("name at least one step of the workflow")
        return names

    def _lineage_rows(self, workflow: Workflow) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT nudge.* FROM owner_workflow_nudges AS nudge
               JOIN owner_workflows AS workflow ON workflow.id = nudge.workflow_id
               WHERE workflow.normalized_name = ? AND varyz_scope(workflow.scope) = ?
               ORDER BY nudge.recorded_at, nudge.monotonic_seq""",
            (normalize_name(workflow.name), workflow.scope),
        ).fetchall()

    def _insert(
        self,
        *,
        workflow_id: str,
        step: str,
        task_step: str,
        project: str | None,
        answer: str,
        skip_id: str | None,
        caller: Caller,
        device_id: str,
    ) -> str:
        nudge_id = new_id()
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO owner_workflow_nudges
               (id, workflow_id, step, normalized_step, task_step, project, answer,
                skip_id, caller_agent_id, caller_display_name, caller_purpose,
                caller_capability_id, caller_device_id, caller_transport, device_id,
                monotonic_seq, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                nudge_id, workflow_id, step, normalize_name(step),
                " ".join(task_step.split()), normalize_scope(project) if project else "",
                answer, skip_id, caller.agent_id, caller.display_name, caller.purpose,
                caller.capability_id, caller.device_id, caller.transport, device_id,
                seq, _iso(self._clock()),
            ),
        )
        return nudge_id


__all__ = ["ANSWERS", "DECISIONS", "SQLiteNudgeStore"]
