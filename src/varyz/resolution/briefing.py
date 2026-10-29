"""The first and last thing an agent does on a task: take the briefing, check the work.

``start_task`` is what the agent is handed before it plans: how the owner works
(general rules), what this project already settled (its vision, rules and
decisions), the order in which the owner usually guides this kind of work
(colour, then logo, then the page), everything the owner expects of it (the
definition of done: favicon, SEO, ...), and the open choices it should ask
varyz about rather than pick itself. ``finish_check`` hands the definition of done back
before the agent reports it is finished.

Neither plans nor judges the work. They carry the owner's authority to the agent
and name the points where the owner's answer, not the agent's taste, decides.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from varyz.resolution.away import AWAY_POLICY, RECOGNITION
from varyz.resolution.away import status as away_status
from varyz.resolution.domains import decision_points, detect_domains, domain_title
from varyz.resolution.knowledge import (
    GLOBAL_SCOPE,
    KnowledgeEntry,
    SQLiteKnowledgeStore,
    _terms,
    normalize_scope,
    scope_applies,
)
from varyz.resolution.memory_policy import MEMORY_POLICY
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.questioning import QUESTION_POLICY, QUESTION_TTL_DAYS
from varyz.resolution.recipes import OFFER as RECIPE_OFFER
from varyz.resolution.recipes import RECORD_HINT as RECIPE_RECORD
from varyz.resolution.recipes import served as recipe_for
from varyz.resolution.task_history import preflight
from varyz.resolution.workflow_nudges import SQLiteNudgeStore
from varyz.resolution.workflows import (
    SQLiteWorkflowStore,
    TaskSizer,
    guide,
    owner_kinds,
    task_size,
)
from varyz.resolution.writing_rules import (
    MESSAGE_NOTE,
    MESSAGE_TITLE,
    TEXT_TITLE,
    WritingRules,
    select_writing,
)

MAX_GENERAL_PREFERENCES = 6
MAX_PROJECT_DECISIONS = 8

_CONSTRAINTS = (
    "Treat every rule and decision below as the owner's constraint, not a suggestion. "
    "Where your plan conflicts with one, change the plan."
)
_WHOLE_DONE = "Build the whole definition of done, not just the literal request."
_FINISH = (
    "Before you write your report, call `finish_check`, check every item yourself "
    "and fix what is not done. In the report name only what is still not done, what "
    "you judged not applicable (and why) and owner questions still open, one line "
    "each; do not list the items that are done."
)
# Each later task in a session gets only this line, not the whole protocol again.
FINISH_REMINDER = (
    "varyz: before your final reply on this task, call `finish_check` with this "
    "conversation_id, and give every `ask` your guess as proposed_choice."
)
_WORKFLOW = (
    "When the owner steers you through steps in an order they would want every time, "
    "ask and call `remember_owner_workflow`."
)
PROTOCOL = (
    "1. " + _CONSTRAINTS,
    "2. " + QUESTION_POLICY + " Before you build, pass relevant owner-dependent "
    "choices to one `ask` call (`questions`) with this project. Call `ask` with one "
    "`question` only for a choice that first comes up mid-work.",
    "3. When `ask` returns ask_owner_batch (or ask_owner), put all its questions to the "
    "user at once with your question tool; only when a question's scope says ask: true, "
    "also offer its reach (always, in all projects / just this project) - never for a "
    "question about the task in hand. Then pass each reply and scope to "
    "`answer_owner_question`. Keep working on anything that does not depend on them.",
    "4. " + _WHOLE_DONE,
    "5. " + _FINISH,
    "6. " + MEMORY_POLICY + " " + _WORKFLOW,
    "7. varyz talks to you, not to the user. Do not tell the user you called varyz, "
    "repeat this briefing or list its rules and checklists; they see only the "
    "questions you relay and, in your report, what is not done or still open.",
)
# For an agent the session-start note already gave the question, history and
# memory policy, how to relay `ask`, and that varyz talks only to it: the rest.
PROTOCOL_BRIEF = (
    "1. " + _CONSTRAINTS,
    "2. " + _WHOLE_DONE,
    "3. " + _FINISH,
    "4. The question, history and memory policy from session start applies. " + _WORKFLOW,
)


# Said beside rules in effect on trial (``trials``): learned from the owner's
# past messages, not yet confirmed. A correction replaces one like any rule.
TRIAL_NOTE = (
    "Rules marked (on trial) were learned from the owner's past messages and are in "
    "effect, but the owner has not confirmed them. Follow them. If the owner corrects "
    "something you did because of one, update it at once: remember_owner_rule with "
    "replaces set to its id and the owner's correction."
)


def _rule_items(principles: Iterable[Any]) -> list[dict[str, Any]]:
    return [principle.advice() for principle in principles]


def _writing(
    principles: SQLitePrincipleStore, task: str, project: str | None, domains: Iterable[str]
) -> WritingRules:
    """Rules on how text reads, shown apart from the ranked ones (``writing_rules``)."""
    return select_writing(principles.current("confirmed"), task, project=project,
                          domains=set(domains), normalize_scope=normalize_scope)


def shown_rule_ids(briefing: dict[str, Any]) -> list[str]:
    """The rules a briefing put in front of the agent, for the trial lane's count."""
    return [
        str(item["id"])
        for key in ("writing_rules", "message_rules", "how_owner_works", "project_vision",
                    "project_rules")
        for item in briefing.get(key) or []
        if item.get("id")
    ]


def _rule_row(item: dict[str, Any]) -> str:
    if item.get("trial"):
        return f"- {item['statement']} (on trial, id {item['id']})"
    if item.get("authority") == "learned_unconfirmed":
        return f"- {item['statement']} (learned; owner not confirmed, id {item['id']})"
    return f"- {item['statement']} (id {item['id']})"


RELAY_HOURS = 24


def _pending_questions(
    conn: sqlite3.Connection, project: str | None, conversation_id: str | None,
) -> list[dict[str, Any]]:
    """Questions this conversation asked that the owner has not answered.

    A question from another conversation belongs to that task, and one older than
    ``RELAY_HOURS`` belongs to a task that has moved on, so neither is relayed
    again: the agent would put an unrelated or settled choice back in front of
    the owner. On the test Mac a five-day-old question kept coming back, so the
    relay window is a day, shorter than the week (``QUESTION_TTL_DAYS``) the
    question stays answerable in ``varyz questions``.
    """
    if not conversation_id:
        return []
    since = datetime.now(UTC) - timedelta(hours=min(RELAY_HOURS, QUESTION_TTL_DAYS * 24))
    try:
        rows = conn.execute(
            """SELECT id, question, project FROM resolution_escalations
               WHERE status = 'pending' AND conversation_id = ? AND created_at >= ?
               ORDER BY created_at, id""",
            (conversation_id, since.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:  # before migration 0037
        return []
    scope = normalize_scope(project) if project else None
    return [
        {"escalation_id": row[0], "question": row[1]}
        for row in rows
        if row[2] is None or (scope is not None and normalize_scope(row[2]) == scope)
    ]


def _decision(entry: KnowledgeEntry) -> dict[str, Any]:
    return {
        "question": entry.question,
        "answer": entry.answer,
        "scope": "all projects" if entry.scope == GLOBAL_SCOPE else entry.scope,
        "since": entry.stated_at.isoformat()[:10],
    }


def _related_decisions(
    entries: list[KnowledgeEntry], task: str, project: str | None
) -> list[KnowledgeEntry]:
    """This project's settled choices that bear on the task, closest first.

    Every choice the project ever settled is not context for every task: a release
    question means nothing to a hook fix. A choice must share a word with the task
    itself, two once the task says more than three things. The project's name and
    the kind of work do not count: nearly every choice in a Python project says
    "Python" or names the project.
    """
    wanted = _terms(task) - _terms(project or "")
    need = 1 if len(wanted) <= 3 else 2
    scored: dict[tuple[str, str], tuple[int, KnowledgeEntry]] = {}
    for entry in entries:
        shared = len(wanted & _terms(entry.question + " " + entry.answer))
        key = (entry.question.casefold(), entry.answer.casefold())
        if shared >= need and key not in scored:
            scored[key] = (shared, entry)
    ranked = sorted(scored.values(), key=lambda item: -item[0])
    return [entry for _shared, entry in ranked][:MAX_PROJECT_DECISIONS]


def _answered_by(question: str, entries: list[KnowledgeEntry]) -> KnowledgeEntry | None:
    """A recorded decision that covers most of a decision point's salient terms."""
    wanted = _terms(question)
    if not wanted:
        return None
    need = max(2, math.ceil(len(wanted) / 2))
    best: tuple[int, KnowledgeEntry] | None = None
    for entry in entries:
        shared = len(wanted & _terms(entry.question + " " + entry.answer))
        if shared >= need and (best is None or shared > best[0]):
            best = (shared, entry)
    return best[1] if best else None


def _workflow_guides(
    conn: sqlite3.Connection,
    task: str,
    *,
    project: str | None,
    domains: tuple[str, ...],
    decisions: list[KnowledgeEntry],
    principles: SQLitePrincipleStore,
    sizer: TaskSizer | None = None,
) -> list[dict[str, Any]]:
    """The owner's usual order for this work, with the steps this project settled."""
    store = SQLiteWorkflowStore(conn)
    workflows = store.applicable(task, project=project, domains=domains)
    if not workflows:
        return []
    scope = normalize_scope(project) if project else None
    evidence = [f"decided: {entry.question} → {entry.answer}" for entry in decisions]
    if scope is not None:
        evidence.extend(
            f"{principle.kind}: {principle.statement}"
            for principle in principles.current()
            if principle.scope == scope
        )
    names = {workflow.name.casefold() for workflow in store.current()}
    nudges = SQLiteNudgeStore(conn)
    guides = []
    for workflow in workflows:
        try:
            history = nudges.history(workflow, project)
        except sqlite3.OperationalError:  # a database not yet migrated to 0041
            history = None
        item = guide(
            workflow, task, project=project, evidence=evidence, history=history,
            sizer=sizer,
        )
        for step in item["steps"]:
            if step["step"].casefold() in names and step["step"].casefold() != (
                workflow.name.casefold()
            ):
                step["has_own_workflow"] = True
        guides.append(item)
    return guides


def start_task(
    conn: sqlite3.Connection,
    task: str,
    *,
    project: str | None,
    cwd: Path | None = None,
    limit: int = 12,
    sizer: TaskSizer | None = None,
    conversation_id: str | None = None,
    cfg: Any = None,
    signals_cfg: Any = None,
) -> dict[str, Any]:
    """The owner's briefing for a task. ``sizer`` sizes it for workflow nudges
    (``task_size.sizer_from_config``); without one, the verb lexicon does.
    ``signals_cfg`` (default ``cfg``) holds the owner's memory-test setting."""
    task = " ".join(task.split())
    if not task:
        raise ValueError("task is required: what the user asked you to do")
    from varyz.coordination import guard

    delegated = guard(conn, "local", {"project": project, "conversation_id": conversation_id},
                      "local", "start_task")
    if delegated is not None:
        return {**delegated, "project": project, "conversation_id": conversation_id,
                "kinds_of_work": []}
    principles = SQLitePrincipleStore(conn)
    domains = detect_domains(task, cwd, extra=owner_kinds(conn))
    kinds = " ".join(domain_title(name) for name in domains)
    writing = _writing(principles, task, project, domains)
    chosen, withheld = principles.applicable(task, project=project, limit=limit,
                                             exclude=writing.ids, context=kinds)
    checklist = principles.checklist(domains, project=project, text=task)
    # The owner's memory test: a few low-stakes rules wait until finish_check.
    from varyz.resolution.rule_signals import held_back

    hidden = held_back([item.principle for item in chosen]
                       + [rule for rules in checklist.values() for rule in rules],
                       conversation_id, signals_cfg or cfg)
    if hidden:
        chosen = tuple(item for item in chosen if item.principle.id not in hidden)
        checklist = {name: tuple(rule for rule in rules if rule.id not in hidden)
                     for name, rules in checklist.items()}
    general = [item.principle for item in chosen if item.principle.scope == GLOBAL_SCOPE]
    specific = [item.principle for item in chosen if item.principle.scope != GLOBAL_SCOPE]

    entries = [
        entry for entry in SQLiteKnowledgeStore(conn).current()
        if scope_applies(entry.scope, project, task)
    ]
    project_decisions = [entry for entry in entries if entry.scope != GLOBAL_SCOPE]
    task_terms = _terms(task + " " + kinds)
    general_scored = sorted(
        (
            (len(task_terms & _terms(entry.question + " " + entry.answer)), entry)
            for entry in entries
            if entry.scope == GLOBAL_SCOPE
        ),
        key=lambda item: -item[0],
    )
    general_preferences = [entry for score, entry in general_scored if score][
        :MAX_GENERAL_PREFERENCES
    ]

    open_points: list[dict[str, str]] = []
    decided: list[dict[str, Any]] = []
    for point in decision_points(domains):
        known = _answered_by(point["question"], entries)
        if known is None:
            open_points.append(point)
        else:
            decided.append({**point, "answer": known.answer, "from": known.question})

    pending = _pending_questions(conn, project, conversation_id)
    workflows = _workflow_guides(
        conn, task, project=project, domains=domains,
        decisions=project_decisions, principles=principles, sizer=sizer,
    )
    from varyz.resolution.continuation import brief as continuation_brief
    from varyz.resolution.rule_questions import pick as rule_question

    away = away_status(conn, project, conversation_id)
    return {
        "task": task,
        "continuation": continuation_brief(
            conn, task, project, conversation_id=conversation_id, cfg=cfg, max_chars=3500,
        ),
        "history_check": preflight(task, project),
        "project": project,
        "conversation_id": conversation_id,
        "away_mode": away,
        "kinds_of_work": [
            {"domain": name, "title": domain_title(name)} for name in domains
        ],
        "writing_rules": _rule_items(writing.text),
        "message_rules": _rule_items(writing.messages),
        "message_rules_note": MESSAGE_NOTE if writing.messages else None,
        "how_owner_works": _rule_items([p for p in general if p.kind == "rule"]),
        "project_vision": _rule_items([p for p in specific if p.kind == "vision"])
        + _rule_items([p for p in general if p.kind == "vision"]),
        "project_rules": _rule_items([p for p in specific if p.kind == "rule"]),
        "more_rules_in_scope": withheld,
        "trial_rules": TRIAL_NOTE if any(p.trial for p in (*general, *specific)) else None,
        "owner_workflows": workflows,
        "task_recipe": recipe_for(conn, task, project),
        "project_decisions": [_decision(entry) for entry in _related_decisions(
            project_decisions, task, project)],
        "general_preferences": [_decision(entry) for entry in general_preferences],
        "definition_of_done": {
            name: [
                {"id": item.id, "item": item.statement,
                 "scope": "all projects" if item.scope == GLOBAL_SCOPE else item.scope}
                for item in items
            ]
            for name, items in checklist.items()
        },
        "ask_varyz_about": open_points,
        "already_decided": decided,
        "pending_owner_questions": pending,
        # One rule the reading was unsure of, asked here because it bears on this task.
        "rule_question": None if away["active"] else rule_question(
            conn, task, project=project, conversation_id=conversation_id,
            now=datetime.now(UTC)),
        "protocol": [*PROTOCOL, RECOGNITION, (
            "Away mode overrides immediate relay and optional workflow/memory questions. "
            + AWAY_POLICY if away["active"] else
            "Check away state before relaying questions if the owner activates it later."
        )],
    }


def finish_check(
    conn: sqlite3.Connection,
    task: str,
    *,
    project: str | None,
    cwd: Path | None = None,
    limit: int = 12,
    sizer: TaskSizer | None = None,
    conversation_id: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
    artifact: str | None = None,
    cfg: Any = None,
    progress_max_chars: int = 4000,
) -> dict[str, Any]:
    task = " ".join(task.split())
    if not task:
        raise ValueError("task is required: what you were asked to do")
    from varyz.coordination import guard

    delegated = guard(conn, "local", {"project": project, "conversation_id": conversation_id},
                      "local", "finish_check")
    if delegated is not None:
        return delegated
    principles = SQLitePrincipleStore(conn)
    domains = detect_domains(task, cwd, extra=owner_kinds(conn))
    checklist = principles.checklist(domains, project=project, text=task)
    writing = _writing(principles, task, project, domains)
    chosen, _ = principles.applicable(
        task, project=project, limit=limit, exclude=writing.ids,
        context=" ".join(domain_title(name) for name in domains))
    items: list[dict[str, Any]] = []
    for name, rules in checklist.items():
        items.extend({"domain": name, "id": rule.id, "item": rule.statement} for rule in rules)
    # The report itself is text for the owner: the writing rules hold for it too.
    standing = [
        {"id": rule.id, "rule": rule.statement}
        for rule in (*writing.text, *writing.messages)
    ] + [
        {"id": item.principle.id, "rule": item.principle.statement}
        for item in chosen
        if item.principle.kind == "rule"
    ]
    decisions = [
        entry for entry in SQLiteKnowledgeStore(conn).current()
        if entry.scope != GLOBAL_SCOPE and scope_applies(entry.scope, project, task)
    ]
    guides = _workflow_guides(
        conn, task, project=project, domains=domains,
        decisions=decisions, principles=principles, sizer=sizer,
    )
    # Only new work at a later step is worth naming; a small change is not.
    steps_open = [
        {
            "workflow": item["name"],
            "workflow_id": item["id"],
            "task_step": item["task_step"],
            "open_before_it": item["open_before_task_step"],
            "record": "record_workflow_nudge with the owner's answer: 'took' or 'skipped'",
        }
        for item in guides
        if item["nudge"]
    ]
    reviews = [
        {"workflow": item["name"], "workflow_id": item["id"], **item["review"]}
        for item in guides
        if item.get("review")
    ]
    reworks = [
        {"workflow": item["name"], "workflow_id": item["id"], **item["rework"]}
        for item in guides
        if item.get("rework")
    ]
    from varyz.resolution.rule_questions import open_question
    from varyz.resolution.verification import verify

    verification = verify(items + [
        {"id": rule["id"], "item": rule["rule"], "domain": "standing"} for rule in standing
    ], cwd=cwd, evidence=evidence, artifact=artifact)
    from varyz.resolution.task_completion import current as task_completion

    completion = task_completion(conn, project, conversation_id, cfg=cfg,
                                 max_chars=progress_max_chars)
    # A recipe that was served asks for its outcome; new work without one asks
    # once for the recipe, so the next agent need not work it out again.
    recipe = recipe_for(conn, task, project)
    recipe_check = (
        {"recipe_id": recipe["id"], "name": recipe["name"], "record": RECIPE_RECORD}
        if recipe is not None
        else {"offer": RECIPE_OFFER} if task_size(task) != "small" else None
    )
    return {
        "task": task,
        "verification": verification,
        "task_completion": completion,
        "project": project,
        "conversation_id": conversation_id,
        "away_mode": away_status(conn, project, conversation_id),
        "away_guidance": (
            AWAY_POLICY + " Leave deferred questions for the return review, not a question UI. "
            "After reporting a completed task call away(action='complete', scope='task'). "
            "Do not end away mode merely because one turn finishes or work is blocked."
            if away_status(conn, project, conversation_id)["active"] else None
        ),
        "kinds_of_work": list(domains),
        "definition_of_done": items,
        "standing_rules": standing,
        "workflow_steps_open": steps_open,
        "workflow_reviews_due": reviews,
        "workflow_reworks": reworks,
        "task_recipe": recipe_check,
        "open_owner_questions": _pending_questions(conn, project, conversation_id),
        # The rule start_task asked about, while the owner has not answered it.
        "rule_question": None if away_status(conn, project, conversation_id)["active"]
        else open_question(conn, conversation_id),
        "how": (
            "Check every item yourself before you write your report and fix anything "
            "not done now. The user does not see this checklist: in the report name "
            "only outstanding work, exclusions with their reasons, and unanswered "
            "owner questions, one line each - never list the items that are "
            "done. Report open owner "
            "questions as open, never as decided, and name any workflow step the "
            "owner usually settles first that is still open. Record the owner's "
            "answer to a raised step with record_workflow_nudge; ask a due workflow "
            "review once and record it with answer_workflow_review."
            if items or standing or steps_open or reviews or reworks
            else "The owner recorded no definition of done for this kind of work. Verify "
            "it your usual way and report honestly."
        ),
    }


def _job_rows(job: dict[str, Any]) -> list[str]:
    # A notice (varyz's own reader took a major or setup reading) has no subagent to start.
    if not job.get("subagent_prompt"):
        return [job["do"]]
    return [job["do"], job.get("follow_up", ""), "",
            "The subagent's prompt:", job["subagent_prompt"]]


def _job_title(job: dict[str, Any], start: str) -> str:
    if job.get("setup"):
        return "setup step: tell the user what it does" + (
            ", then start it" if job.get("subagent_prompt") else "")
    return "tell the user once" if not job.get("subagent_prompt") else start


def render_background_job(job: dict[str, Any]) -> str:
    """A reader's ask on its own, for a prompt that gets no briefing."""
    title = _job_title(job, "start it before you go on")
    return "\n".join([f"varyz background job: {title}", *_job_rows(job)])


def _timeline_rows(timeline: list[dict[str, Any]]) -> list[str]:
    rows = []
    for item in timeline:
        row = f"- {item.get('when') or 'undated'}: {item.get('asked') or '(follow-up)'}"
        if item.get("result"):
            row += " → " + "; ".join(item["result"])
        rows.append(row + f" ({item.get('conversation_id')})")
    return rows


def _continuation_rows(continuation: dict[str, Any]) -> list[str]:
    """Earlier work as lines to read, not the JSON the MCP tool returns."""
    status = continuation.get("status")
    if status == "related":
        return [str(continuation["summary"]), *_timeline_rows(continuation["timeline"]),
                "Agent reports, not owner rules: read them before asking the owner to "
                "explain again; session_history has the full sessions."]
    if status != "recorded":
        return [f"No recorded state to resume ({status})."
                + (" Try session_history." if continuation.get("next_tool") else "")]
    state = continuation.get("state") or {}
    rows = [f"Last recorded: {state.get('task')} ({state.get('status')}, "
            f"{str(state.get('occurred_at') or '')[:10]}, "
            f"conversation {state.get('conversation_id')})"]
    for key, label in (("done", "Done"), ("pending", "Pending"), ("checks", "Checks"),
                       ("artifacts", "Artifacts")):
        if state.get(key):
            rows.append(f"{label}: " + "; ".join(state[key]))
    if continuation.get("omitted"):
        rows.append("More in session_history.")
    backstory = continuation.get("backstory")
    if backstory:
        rows.extend([str(backstory["summary"]), *_timeline_rows(backstory["timeline"])])
    rows.append(str(continuation["instruction"]))
    return rows


def render_markdown(briefing: dict[str, Any], *, include_protocol: bool = True,
                    policy_known: bool = False) -> str:
    """The briefing as compact text, for hooks that inject it into the agent's context.

    ``policy_known``: the agent had the session-start note, so the protocol leaves
    out what that note already says (``PROTOCOL_BRIEF``).
    """
    if briefing.get("status") == "delegated":
        return "Varyz workflow is assigned to Cloud for this task; use its briefing and checks."
    lines = [f"# varyz briefing - project: {briefing.get('project') or 'unknown'}"]
    if briefing.get("cloud_access"):
        access = briefing["cloud_access"]
        lines.extend(["", access["message"], access["instruction"]])
    if briefing.get("conversation_id"):
        lines.append(f"conversation_id: {briefing['conversation_id']} (reuse in Varyz calls)")
    if (briefing.get("away_mode") or {}).get("active"):
        lines.extend(["", "## Away mode (overrides immediate question relay)", AWAY_POLICY])
    kinds = briefing.get("kinds_of_work") or []
    if kinds:
        lines.append("Kind of work: " + ", ".join(item["title"] for item in kinds))

    def section(title: str, rows: list[str]) -> None:
        if rows:
            lines.append("")
            lines.append(f"## {title}")
            lines.extend(rows)

    history = briefing.get("history_check")
    continuation = briefing.get("continuation")
    if continuation:
        section("Earlier sessions on this topic" if continuation.get("status") == "related"
                else "Recorded work to continue", _continuation_rows(continuation))
    if history and history.get("status") != "optional":
        # The policy itself came with session start or the protocol; this says
        # only where this task's history stands. Most tasks do not refer to
        # earlier work and get no section at all.
        section("History before clarification", [
            f"Status: {history['status']}. {history['coverage']}",
            *([f"Search once, with a small limit: `session_history` (project "
               f"{history.get('project')}, latest true) for earlier agent work, or "
               f"`recall` (limit 3) with: {history.get('query')}. If it is slow or finds "
               "nothing useful, go on with the task."]
              if history["status"] == "required" else []),
        ])

    job = briefing.get("background_job")
    if job:
        # First: it is started before the task is planned.
        section(f"Background job: {_job_title(job, 'start it before you plan')}",
                _job_rows(job))

    section(TEXT_TITLE, [_rule_row(item) for item in briefing.get("writing_rules") or []])
    section(MESSAGE_TITLE, ([str(briefing["message_rules_note"])]
                            if briefing.get("message_rules_note") else [])
            + [_rule_row(item) for item in briefing.get("message_rules") or []])
    section(
        "How the owner works (always)",
        [_rule_row(item) for item in briefing.get("how_owner_works", [])],
    )
    section(
        "This project's vision",
        [_rule_row(item) for item in briefing.get("project_vision", [])],
    )
    section(
        "This project's rules",
        [_rule_row(item) for item in briefing.get("project_rules", [])],
    )
    if briefing.get("trial_rules"):
        lines.extend(["", str(briefing["trial_rules"])])
    for item in briefing.get("owner_workflows", []):
        rows = [f"*{item['origin']}, {item['scope']}*"]
        if item.get("why"):
            rows.append(f"Why: {item['why']}")
        for number, step in enumerate(item["steps"], 1):
            row = f"{number}. {step['step']}"
            if step.get("optional"):
                row += " (optional)"
            if step.get("how"):
                row += f" - {step['how']}"
            if step.get("settled"):
                row += f" ✓ settled ({step['settled']})"
            if step["step"] == item.get("task_step"):
                row += " ← this task"
            if step.get("has_own_workflow"):
                row += " (has its own workflow)"
            rows.append(row)
        rows.append(f"→ {item['guidance']}")
        section(f"How the owner guides this work: {item['name']} (their usual order)", rows)
    recipe = briefing.get("task_recipe")
    if recipe:
        worked = recipe.get("worked_before")
        rows = [f"*{recipe['scope']}" + (f", worked {worked} times before" if worked else "")
                + "*"]
        rows.extend(f"{number}. {step}" for number, step in enumerate(recipe["steps"], 1))
        rows.append(f"Check: {recipe['check']}")
        if recipe.get("pitfalls"):
            rows.append(f"Watch out: {recipe['pitfalls']}")
        rows.append(f"→ {recipe['guidance']}")
        section(f"Recipe from a past task: {recipe['name']} (id {recipe['id']})", rows)
    section(
        "Already decided for this project",
        [f"- {item['question']} → {item['answer']}"
         for item in briefing.get("project_decisions", [])],
    )
    section(
        "The owner's general preferences that may apply",
        [f"- {item['question']} → {item['answer']}"
         for item in briefing.get("general_preferences", [])],
    )
    done = briefing.get("definition_of_done") or {}
    for name, items in done.items():
        title = domain_title(name)
        section(
            f"Definition of done: {title}",
            [f"- [ ] {item['item']}" for item in items],
        )
    section(
        "Possible choices: check relevance and existing answers before `ask`",
        [f"- {item['question']}" for item in briefing.get("ask_varyz_about", [])],
    )
    section(
        "Owner questions still open",
        [f"- {item['question']}" for item in briefing.get("pending_owner_questions", [])],
    )
    asked = briefing.get("rule_question")
    if asked:
        section("One rule to check with the owner", [
            f"- {asked['question']} ({asked['scope']}; id {asked['id']})",
            str(asked["how"]),
        ])
    if include_protocol:
        protocol = list(briefing.get("protocol", []))
        if policy_known:
            # What start_task adds after PROTOCOL: recognition (in the session-start
            # note too) and the away line (not).
            protocol = [*PROTOCOL_BRIEF,
                        *(item for item in protocol[len(PROTOCOL):] if item != RECOGNITION)]
        section("How to work with varyz", protocol)
    return "\n".join(lines)


__all__ = [
    "FINISH_REMINDER", "PROTOCOL", "finish_check", "render_background_job", "render_markdown",
    "start_task",
]
