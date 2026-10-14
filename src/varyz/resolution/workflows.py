"""How the owner guides an agent through a kind of work: the steps, in their order.

A rule says how something is done ("self-host the fonts"). A workflow says in
which order the owner settles things with an agent: for a website, the colour,
then the logo, then what goes on the page; for a product, the vision, then the
design, then monetization. An agent that knows the order asks about the colour
before it builds a page around a colour nobody chose.

Varyz does not run the steps. It hands the order to the agent with the task, marks
the steps this project already has a recorded decision for, and names the next
one. When the task starts at a later step while an earlier one is still open, the
agent is told to do what was asked but to say once that the owner usually settles
the earlier step first: the owner sometimes starts with the wrong thing, and a
workflow that only mirrored what they did would teach agents the slip.

Two sources, one ledger. A 'stated' workflow is the owner's own order, recorded
from their words. A 'learned' one is an order found repeated across their
sessions (see ``workflow_miner``); it reaches agents at once, marked as learned,
never replaces a stated one, and once withdrawn is never learned back.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.resolution.knowledge import (
    GLOBAL_SCOPE,
    _datetime,
    _iso,
    _next_monotonic_seq,
    _terms,
    _write_transaction,
    fold_stored_scopes,
    normalize_scope,
    stored_scope,
)
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore, normalize_domain

BASES = ("stated", "learned")
MAX_NAME_CHARS = 80
MAX_STEPS = 20
MAX_STEP_CHARS = 200
MAX_HOW_CHARS = 500
MAX_RATIONALE_CHARS = 500
SETTLED_EXCERPT_CHARS = 90

_CURRENT = """workflow.id NOT IN (SELECT supersedes_id FROM owner_workflows
                                  WHERE supersedes_id IS NOT NULL)
              AND workflow.id NOT IN (SELECT workflow_id FROM owner_workflow_retractions)"""


def normalize_name(name: str) -> str:
    """Identity of a workflow's name: case, spacing and closing punctuation ignored."""
    value = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    return value.rstrip(".!;:").rstrip()


@dataclass(frozen=True)
class Step:
    step: str
    how: str = ""
    # An optional step keeps its place in the order but is never raised as open:
    # the owner chose that when asked about a step they kept skipping.
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {"step": self.step}
        if self.how:
            item["how"] = self.how
        if self.optional:
            item["optional"] = True
        return item


def parse_steps(steps: Iterable[Any]) -> tuple[Step, ...]:
    """Steps from strings ('colour', 'logo: three sketches side by side') or dicts."""
    parsed: list[Step] = []
    for item in steps:
        optional = False
        if isinstance(item, Step):
            step, how, optional = item.step, item.how, item.optional
        elif isinstance(item, Mapping):
            step, how = str(item.get("step") or ""), str(item.get("how") or "")
            optional = item.get("optional") is True
        elif isinstance(item, str):
            step, _, how = item.partition(": ")
        else:
            raise ValueError("a step is text ('colour' or 'logo: how you guide it')")
        step = " ".join(step.split()).strip(" -:")
        how = " ".join(how.split())
        if not step:
            continue
        if len(step) > MAX_STEP_CHARS:
            raise ValueError(f"a step is at most {MAX_STEP_CHARS} characters")
        if len(how) > MAX_HOW_CHARS:
            raise ValueError(f"how a step is guided is at most {MAX_HOW_CHARS} characters")
        parsed.append(Step(step, how, optional))
    if len(parsed) < 2:
        raise ValueError("a workflow needs at least two steps, in the order you take them")
    if len(parsed) > MAX_STEPS:
        raise ValueError(f"a workflow has at most {MAX_STEPS} steps")
    names = [normalize_name(item.step) for item in parsed]
    if len(set(names)) != len(names):
        raise ValueError("each step appears once in a workflow")
    return tuple(parsed)


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    scope: str
    domain: str
    steps: tuple[Step, ...]
    rationale: str
    basis: str
    source: str
    provenance: Mapping[str, Any]
    caller: Caller
    device_id: str
    monotonic_seq: int
    supersedes_id: str | None
    created_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return normalize_name(self.name), self.scope

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scope": self.scope,
            "domain": self.domain,
            "steps": [step.to_dict() for step in self.steps],
            "rationale": self.rationale,
            "basis": self.basis,
            "source": self.source,
            "provenance": dict(self.provenance),
            "caller": self.caller.model_dump(mode="json"),
            "device_id": self.device_id,
            "monotonic_seq": self.monotonic_seq,
            "supersedes_id": self.supersedes_id,
            "created_at": _iso(self.created_at),
        }

    def origin(self) -> str:
        """Where the order came from, as an agent is told it."""
        if self.basis == "stated":
            return "the owner's own order"
        units = self.provenance.get("units")
        unit = self.provenance.get("unit", "sessions")
        return f"learned from {units} {unit}" if units else "learned from past sessions"


class SQLiteWorkflowStore:
    """At most one current workflow per name and scope; the ledger keeps the rest."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Any = utc_now) -> None:
        self._conn = conn
        fold_stored_scopes(conn)
        self._clock = clock
        self._lock = threading.RLock()

    # -- writes ---------------------------------------------------------------

    def record(
        self,
        *,
        name: str,
        steps: Iterable[Any],
        caller: Caller,
        device_id: str,
        source: str,
        scope: str | None = GLOBAL_SCOPE,
        domain: str | None = "",
        rationale: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> str:
        """The owner's own order. It replaces whatever was current for this name."""
        name, scope, domain, rationale, source = self._validated(
            name, scope, domain, rationale, source
        )
        parsed = parse_steps(steps)
        with _write_transaction(self._conn, self._lock):
            current = self._current_for(name, scope)
            if (
                current is not None
                and current.basis == "stated"
                and current.steps == parsed
                and current.domain == domain
            ):
                return current.id
            return self._insert(
                name=name, scope=scope, domain=domain, steps=parsed, rationale=rationale,
                basis="stated", source=source, provenance=provenance or {},
                caller=caller, device_id=device_id,
                supersedes_id=current.id if current is not None else None,
            )

    def learn(
        self,
        *,
        name: str,
        steps: Iterable[Any],
        caller: Caller,
        device_id: str,
        source: str,
        scope: str | None = GLOBAL_SCOPE,
        domain: str | None = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> str | None:
        """An order found in the owner's sessions; ``None`` when nothing changes.

        The owner's stated order is never replaced, the same learned order is not
        written twice, and an order the owner withdrew is never learned back.
        A learned order whose step notes changed (say, one more session in which a
        step had to be gone back to) is written again, superseding the last one.
        """
        name, scope, domain, _rationale, source = self._validated(
            name, scope, domain, "", source
        )
        parsed = parse_steps(steps)
        order = _order(parsed)
        with _write_transaction(self._conn, self._lock):
            current = self._current_for(name, scope)
            if current is not None and current.basis == "stated":
                return None
            if current is not None and current.steps == parsed:
                return None
            if order in self._withdrawn_orders(name, scope):
                return None
            return self._insert(
                name=name, scope=scope, domain=domain, steps=parsed, rationale="",
                basis="learned", source=source, provenance=provenance or {},
                caller=caller, device_id=device_id,
                supersedes_id=current.id if current is not None else None,
            )

    def retract(
        self, workflow_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        """Withdraw a workflow; the original stays for audit and is never relearned."""
        retraction_id = new_id()
        with _write_transaction(self._conn, self._lock):
            self._current_row(workflow_id)
            seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """INSERT INTO owner_workflow_retractions
                   (id, workflow_id, reason, caller_agent_id, caller_display_name,
                    caller_purpose, caller_capability_id, caller_device_id,
                    caller_transport, device_id, monotonic_seq, retracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    retraction_id, workflow_id, reason.strip(), caller.agent_id,
                    caller.display_name, caller.purpose, caller.capability_id,
                    caller.device_id, caller.transport, device_id, seq,
                    _iso(self._clock()),
                ),
            )
        return retraction_id

    # -- reads ----------------------------------------------------------------

    def current(self) -> tuple[Workflow, ...]:
        rows = self._conn.execute(
            f"""SELECT workflow.* FROM owner_workflows AS workflow WHERE {_CURRENT}
                ORDER BY workflow.created_at DESC, workflow.monotonic_seq DESC"""
        ).fetchall()
        return tuple(self._workflow(row) for row in rows)

    def domains(self) -> tuple[str, ...]:
        """Every kind of work the owner's current workflows are filed under, sorted."""
        return tuple(sorted({workflow.domain for workflow in self.current() if workflow.domain}))

    def get(self, workflow_id: str) -> Workflow | None:
        row = self._conn.execute(
            "SELECT * FROM owner_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return self._workflow(row) if row is not None else None

    def applicable(
        self, text: str, *, project: str | None = None, domains: Collection[str] = ()
    ) -> tuple[Workflow, ...]:
        """The workflows a task falls under, one per name, the closest scope first.

        A workflow tied to a kind of work ('website') applies to that work or when
        the task names it. A free-standing one ('product': vision, design,
        monetization) also applies when the task names one of its steps: "work out
        monetization" is a step of building the product.
        """
        wanted = {normalize_domain(domain) for domain in domains}
        project_scope = normalize_scope(project) if project else None
        lowered = unicodedata.normalize("NFKC", text).casefold()
        task_terms = _terms(text)
        chosen: dict[str, tuple[int, Workflow]] = {}
        for workflow in self.current():
            specific = workflow.scope != GLOBAL_SCOPE
            if specific and workflow.scope != project_scope and not re.search(
                rf"(?<!\w){re.escape(workflow.scope)}(?!\w)", lowered
            ):
                continue
            named = _covers(task_terms, _terms(workflow.name))
            if (workflow.domain and workflow.domain in wanted) or named:
                rank = 0
            elif not workflow.domain and any(
                _covers(task_terms, _terms(step.step)) for step in workflow.steps
            ):
                rank = 1
            else:
                continue
            key = normalize_name(workflow.name)
            ordering = rank * 2 + (0 if specific else 1)
            kept = chosen.get(key)
            if kept is None or ordering < kept[0]:
                chosen[key] = (ordering, workflow)
        return tuple(item[1] for item in sorted(chosen.values(), key=lambda item: item[0]))

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _validated(
        name: str, scope: str | None, domain: str | None, rationale: str, source: str
    ) -> tuple[str, str, str, str, str]:
        name = " ".join(name.split()).strip(" -:")
        rationale = " ".join((rationale or "").split())
        source = source.strip()
        if not name or not source:
            raise ValueError("a workflow needs a name and a source")
        if len(name) > MAX_NAME_CHARS:
            raise ValueError(f"a workflow's name is at most {MAX_NAME_CHARS} characters")
        if len(rationale) > MAX_RATIONALE_CHARS:
            raise ValueError(f"a workflow's reason is at most {MAX_RATIONALE_CHARS} characters")
        return name, normalize_scope(scope), normalize_domain(domain), rationale, source

    def _current_for(self, name: str, scope: str) -> Workflow | None:
        row = self._conn.execute(
            f"""SELECT workflow.* FROM owner_workflows AS workflow
                WHERE workflow.normalized_name = ? AND varyz_scope(workflow.scope) = ?
                  AND {_CURRENT}
                ORDER BY workflow.created_at DESC, workflow.monotonic_seq DESC LIMIT 1""",
            (normalize_name(name), scope),
        ).fetchone()
        return self._workflow(row) if row is not None else None

    def _withdrawn_orders(self, name: str, scope: str) -> set[tuple[str, ...]]:
        rows = self._conn.execute(
            """SELECT workflow.steps_json FROM owner_workflows AS workflow
               JOIN owner_workflow_retractions AS retraction
                 ON retraction.workflow_id = workflow.id
               WHERE workflow.normalized_name = ? AND varyz_scope(workflow.scope) = ?""",
            (normalize_name(name), scope),
        ).fetchall()
        return {_order(_steps_from_json(row[0])) for row in rows}

    def _insert(
        self,
        *,
        name: str,
        scope: str,
        domain: str,
        steps: tuple[Step, ...],
        rationale: str,
        basis: str,
        source: str,
        provenance: Mapping[str, Any],
        caller: Caller,
        device_id: str,
        supersedes_id: str | None,
    ) -> str:
        workflow_id = new_id()
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO owner_workflows
               (id, name, normalized_name, scope, domain, steps_json, rationale, basis,
                source, provenance_json, caller_agent_id, caller_display_name,
                caller_purpose, caller_capability_id, caller_device_id, caller_transport,
                device_id, monotonic_seq, supersedes_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                workflow_id, name, normalize_name(name), scope, domain,
                json.dumps([step.to_dict() for step in steps], ensure_ascii=False),
                rationale, basis, source,
                json.dumps(dict(provenance), ensure_ascii=False, sort_keys=True),
                caller.agent_id, caller.display_name, caller.purpose,
                caller.capability_id, caller.device_id, caller.transport, device_id,
                seq, supersedes_id, _iso(self._clock()),
            ),
        )
        return workflow_id

    def _current_row(self, workflow_id: str) -> Workflow:
        row = self._conn.execute(
            f"""SELECT workflow.* FROM owner_workflows AS workflow
                WHERE workflow.id = ? AND {_CURRENT}""",
            (workflow_id,),
        ).fetchone()
        if row is not None:
            return self._workflow(row)
        if self.get(workflow_id) is None:
            raise KeyError(f"unknown workflow: {workflow_id}")
        raise ValueError(f"workflow {workflow_id} was already replaced or withdrawn")

    @staticmethod
    def _workflow(row: sqlite3.Row) -> Workflow:
        return Workflow(
            id=row["id"],
            name=row["name"],
            scope=stored_scope(row["scope"]),
            domain=row["domain"],
            steps=_steps_from_json(row["steps_json"]),
            rationale=row["rationale"],
            basis=row["basis"],
            source=row["source"],
            provenance=json.loads(row["provenance_json"]),
            caller=Caller(
                agent_id=row["caller_agent_id"],
                display_name=row["caller_display_name"],
                purpose=row["caller_purpose"],
                capability_id=row["caller_capability_id"],
                device_id=row["caller_device_id"],
                transport=row["caller_transport"],
            ),
            device_id=row["device_id"],
            monotonic_seq=int(row["monotonic_seq"]),
            supersedes_id=row["supersedes_id"],
            created_at=_datetime(row["created_at"]),
        )


def _steps_from_json(value: str) -> tuple[Step, ...]:
    return tuple(
        Step(str(item.get("step", "")), str(item.get("how", "")), item.get("optional") is True)
        for item in json.loads(value)
    )


def _order(steps: Sequence[Step]) -> tuple[str, ...]:
    return tuple(normalize_name(step.step) for step in steps)


def _covers(have: set[str], wanted: set[str]) -> bool:
    """Most of a short label's salient terms are present: 'monetization' in a task."""
    if not wanted:
        return False
    return len(have & wanted) >= max(1, math.ceil(len(wanted) / 2))


def _settled_by(step: Step, evidence: Sequence[tuple[str, set[str]]]) -> str | None:
    """A recorded project decision or rule that already covers this step, if any.

    Stricter than task matching: a step is marked settled only when every salient
    term of a one- or two-word step is there ("colour" in "which brand colours ...").
    """
    wanted = _terms(step.step)
    if not wanted:
        return None
    need = len(wanted) if len(wanted) <= 2 else math.ceil(len(wanted) * 2 / 3)
    for label, terms in evidence:
        if len(wanted & terms) >= need:
            return label
    return None


def _clip(text: str, limit: int = SETTLED_EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# How big a task is. A small change to a later step (a colour tweak on a page) is
# not the moment to raise an open earlier step; new work on it is. The call is
# made by a local classifier (`varyz.resolution.task_size`); the verb lexicon
# below is its last fallback, used when neither the local model nor the local
# embedding is there. On the authored held-out set it scored 60.9% against the
# model's 98.6% (reports/TASK_SIZE_CLASSIFIER_2026-09-25.md). Words that say so
# outright ('small', 'minor') decide; otherwise the first verb that names a size
# does. A task that names no size counts as new work: the nudge is one sentence
# and never blocks, so a missed small change costs less than a missed start on
# the wrong step.
_SMALL_OUTRIGHT = re.compile(
    r"\b(?:small|minor|tiny|little|quick|slight(?:ly)?|typos?|nit)\b"
)
_SIZED = re.compile(
    r"\b(?P<small>fix(?:es|ed|ing)?|tweak\w*|adjust\w*|bump\w*|polish\w*|nudg\w*|"
    r"renam\w*|align\w*|touch(?:\s|-)?up|correct(?:s|ed|ing)?|patch\w*|tidy|"
    r"clean(?:\s|-)?up|swap\w*|change the|update the|move the)\b"
    r"|\b(?P<new>build\w*|creat\w*|make|mak(?:es|ing)|design\w*|start\w*|new|"
    r"redo\w*|re-do\w*|rebuild\w*|redesign\w*|rework\w*|rewrit\w*|launch\w*|"
    r"set(?:\s|-)?up|draft\w*|write|writing|plan\w*|overhaul\w*|from scratch|add an?)\b"
)
# A task that goes back over a step already built.
_REDO = re.compile(
    r"\b(?:redo\w*|re-do\w*|rework\w*|rebuild\w*|redesign\w*|rewrit\w*|remak\w*|"
    r"start(?:ing)? over|from scratch|again|overhaul\w*|another pass)\b"
)


def _without_steps(task: str, steps: Sequence[Step]) -> str:
    """The task with the workflow's own step names taken out: 'design' is a step
    here, so "tweak the design" must be sized by 'tweak' alone."""
    text = " " + unicodedata.normalize("NFKC", task).casefold() + " "
    for step in sorted(steps, key=lambda item: -len(item.step)):
        name = re.escape(normalize_name(step.step))
        text = re.sub(rf"(?<!\w){name}\w*", " ", text)
    return text


def task_size(task: str, steps: Sequence[Step] = ()) -> str:
    """'small' for a change to work already there, 'new' for new work on a step."""
    text = _without_steps(task, steps)
    if _SMALL_OUTRIGHT.search(text):
        return "small"
    first = _SIZED.search(text)
    if first is not None and first.group("small"):
        return "small"
    return "new"


@dataclass(frozen=True)
class Sizing:
    """How big a task is, and what said so: 'model', 'embedding' or 'lexicon'."""

    size: str
    by: str


# Sizes a task against the workflow's steps. Any callable of this shape will do;
# `varyz.resolution.task_size.sizer_from_config` builds the configured chain.
TaskSizer = Callable[[str, Sequence[Step]], Sizing]


def lexicon_sizer(task: str, steps: Sequence[Step]) -> Sizing:
    return Sizing(task_size(task, steps), "lexicon")


def is_redo(task: str, steps: Sequence[Step] = ()) -> bool:
    """The task goes back over work already done ('redo the page', 'again')."""
    return _REDO.search(_without_steps(task, steps)) is not None


# After this many skips of one step within one week, with no work redone after
# any skip since the step was last reviewed, the owner is asked once whether the
# step still belongs in the order. Skips older than the week expire: an
# occasional skip is not a reason to reopen the order, so the step stays.
REVIEW_AFTER_SKIPS = 2
REVIEW_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class OpenSkip:
    """A skip in this project not yet followed by rework."""

    id: str
    step: str       # normalized
    task_step: str  # normalized


@dataclass(frozen=True)
class NudgeHistory:
    """What the owner answered when agents raised this workflow's open steps.

    Counts are by normalized step name over the workflow's versions (its name and
    scope), since the last time the owner reviewed that step. ``skipped`` counts
    only the last ``REVIEW_WINDOW``; ``reworked`` counts every skip since the last
    review that led to rework, however old.
    """

    skipped: Mapping[str, int] = field(default_factory=dict)
    reworked: Mapping[str, int] = field(default_factory=dict)
    reviewed: frozenset[str] = frozenset()  # steps reviewed on this very version
    open_skips: tuple[OpenSkip, ...] = ()


def guide(
    workflow: Workflow,
    task: str,
    *,
    project: str | None,
    evidence: Sequence[str] = (),
    history: NudgeHistory | None = None,
    sizer: TaskSizer | None = None,
) -> dict[str, Any]:
    """What an agent is told about one workflow for this task.

    ``evidence`` is what this project already recorded, one line each: its
    decisions ("decided: Which colours? → navy") and rules ("vision: ..."). A step
    a line covers is shown as settled, with that line, so the agent can see why;
    the rest are open, in the owner's order. ``history`` is what the owner
    answered when earlier nudges raised this workflow's steps. ``sizer`` says
    whether the task is new work or a small change; it is asked only when the
    answer matters (the task is at a later step with earlier ones open), so a
    local model is never woken for a task it could not change. A sizer with an
    ``is_redo`` method also decides whether the task redoes its step.
    """
    history = history or NudgeHistory()
    indexed = [(line, _terms(line)) for line in evidence]
    task_terms = _terms(task)
    steps: list[dict[str, Any]] = []
    # The step the task is at: the one whose name it covers most completely
    # ('hosting' over 'page content' for "host the landing page").
    best: tuple[int, tuple[float, int]] | None = None
    for index, step in enumerate(workflow.steps):
        settled = _settled_by(step, indexed)
        item: dict[str, Any] = {"step": step.step}
        if step.how:
            item["how"] = step.how
        if step.optional:
            item["optional"] = True
        if settled is not None:
            item["settled"] = _clip(settled)
        steps.append(item)
        wanted = _terms(step.step)
        shared = len(task_terms & wanted)
        if wanted and _covers(task_terms, wanted):
            fit = (shared / len(wanted), shared)
            if best is None or fit > best[1]:
                best = (index, fit)
    at = best[0] if best is not None else None
    open_before = [
        item["step"]
        for item in steps[: at if at is not None else 0]
        if "settled" not in item and not item.get("optional")
    ]
    upcoming = next(
        (item["step"] for item in steps if "settled" not in item and not item.get("optional")),
        None,
    )
    sizing = (
        (sizer or lexicon_sizer)(task, workflow.steps)
        if at is not None and open_before
        else None
    )
    nudge = sizing is not None and sizing.size == "new"
    where = f" for {project}" if project else ""
    sentences: list[str] = []
    if nudge:
        assert at is not None
        earlier = _join(open_before)
        sentences.append(
            f"This task starts new work on the '{steps[at]['step']}' step, but the owner "
            f"usually settles {earlier} before it, and nothing is recorded for "
            f"{'that' if len(open_before) == 1 else 'those'}{where} yet. Do what the owner "
            f"asked, but say once that they usually settle {earlier} first and offer to "
            "do that first: starting here may be a slip. Do not block on it."
        )
        costly = [
            f"'{name}' ({history.reworked[normalize_name(name)]}×)"
            for name in open_before
            if history.reworked.get(normalize_name(name))
        ]
        if costly:
            sentences.append(
                "Say why: skipping " + ", ".join(costly) + " before led to redoing work "
                "once it was settled."
            )
        sentences.append(
            "When the owner answers, record it with `record_workflow_nudge` "
            f"(workflow_id '{workflow.id}', steps {open_before}, answer 'took' if they "
            "settle it first, 'skipped' if they go on)."
        )
    elif at is not None and open_before:
        sentences.append(
            f"A small change on the '{steps[at]['step']}' step: just do it. The owner "
            f"usually settles {_join(open_before)} first, but do not raise that for a "
            "small change."
        )
    elif upcoming is not None:
        sentences.append(
            f"Next in the owner's order: '{upcoming}'. Settle each step with the owner "
            "(call `ask`) before building on it, and take them in this order."
        )
    else:
        sentences.append(f"Every step already has a recorded decision{where}; build on those.")

    rework: dict[str, Any] | None = None
    # A sizer that also judges rework (the configured chain does) decides it;
    # otherwise the verb list does.
    redo = getattr(sizer, "is_redo", None) or is_redo
    here = normalize_name(steps[at]["step"]) if at is not None else None
    settled_now = {normalize_name(item["step"]) for item in steps if "settled" in item}
    reworkable = [
        skip for skip in history.open_skips
        if skip.task_step == here and skip.step in settled_now
    ]
    # Asked only when there is a skip it could count against: a model call (and
    # a cold load) for every task at a step would buy nothing.
    if at is not None and reworkable and redo(task, workflow.steps):
        skip = reworkable[0]
        step_name = next(
            item["step"] for item in steps if normalize_name(item["step"]) == skip.step
        )
        rework = {"skip_id": skip.id, "step": step_name, "task_step": steps[at]["step"]}
        sentences.append(
            f"Earlier in this project the owner skipped '{step_name}' and went "
            f"straight to '{steps[at]['step']}'; '{step_name}' is settled now and "
            f"this task redoes '{steps[at]['step']}'. Record that with "
            f"`record_workflow_nudge` (workflow_id '{workflow.id}', "
            f"skip_id '{skip.id}', answer 'rework'): it is what makes the step "
            "worth raising next time."
        )

    review: dict[str, Any] | None = None
    for item in steps:
        name = normalize_name(item["step"])
        skips = history.skipped.get(name, 0)
        if (
            not item.get("optional")
            and skips >= REVIEW_AFTER_SKIPS
            and not history.reworked.get(name)
            and name not in history.reviewed
        ):
            review = {
                "step": item["step"],
                "skips": skips,
                "question": (
                    f"You have skipped '{item['step']}' in your {workflow.name} workflow "
                    f"{skips} times this week, and no work had to be redone after any "
                    "skip of it. Keep it in the order, make it optional (agents stop "
                    "raising it), or drop it?"
                ),
            }
            sentences.append(
                "Ask the owner this once, with your question tool, offering keep / "
                f"optional / drop: \"{review['question']}\" Record the reply with "
                f"`answer_workflow_review` (workflow_id '{workflow.id}', step "
                f"'{item['step']}'). Never change the workflow without their answer."
            )
            break

    return {
        "id": workflow.id,
        "name": workflow.name,
        "scope": "all projects" if workflow.scope == GLOBAL_SCOPE else workflow.scope,
        "basis": workflow.basis,
        "origin": workflow.origin(),
        **({"why": workflow.rationale} if workflow.rationale else {}),
        "steps": steps,
        "task_step": steps[at]["step"] if at is not None else None,
        "work": sizing.size if sizing else None,
        "sized_by": sizing.by if sizing else None,
        "open_before_task_step": open_before,
        "nudge": nudge,
        "next_step": upcoming,
        **({"rework": rework} if rework else {}),
        **({"review": review} if review else {}),
        "guidance": " ".join(sentences),
    }


def _join(items: Sequence[str]) -> str:
    quoted = [f"'{item}'" for item in items]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def owner_kinds(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The kinds of work the owner filed confirmed rules or workflows under, sorted.

    ``detect_domains`` takes them as ``extra``, so a kind the owner named
    themselves ('gardening') is detected when a task names it.
    """
    rules = SQLitePrincipleStore(conn).rule_domains()
    return tuple(sorted({*rules, *SQLiteWorkflowStore(conn).domains()}))


__all__ = [
    "BASES",
    "REVIEW_AFTER_SKIPS",
    "REVIEW_WINDOW",
    "Sizing",
    "TaskSizer",
    "NudgeHistory",
    "OpenSkip",
    "SQLiteWorkflowStore",
    "Step",
    "Workflow",
    "guide",
    "is_redo",
    "lexicon_sizer",
    "normalize_name",
    "owner_kinds",
    "parse_steps",
    "task_size",
]
