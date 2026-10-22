"""Task recipes: how a task the agent had to work out gets done next time.

A workflow is the order the owner settles things in. A recipe is narrower: the
steps that got one task done after the agent worked out how (a failed first try,
a search, a fix to its first approach), how to tell it worked, and the trap that
cost the time. The agent writes it at finish_check, once the task is done and
verified; the next start_task whose task names it gets it back, so the next agent
starts where this one finished instead of working it out again.

A recipe is small on purpose: at most eight steps and 1,600 characters, about
400 tokens, so it costs a briefing little. Each use appends an outcome. 'changed'
comes with a better recipe that supersedes the old one; a recipe whose last two
uses failed is no longer served until it is replaced.

The agent writes these, not a model and not a miner, so learning one costs no AI
call. The lexical miner that looked for repeated orders in past sessions found
none in 5,876 turns (2026-10-22) and is off by default.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
from varyz.resolution.workflows import Step, normalize_name

OUTCOMES = ("worked", "changed", "failed")
MIN_STEPS = 2
MAX_STEPS = 8
MAX_NAME_CHARS = 80
MAX_STEP_CHARS = 200  # the step and how it is done, together
MAX_CHECK_CHARS = 240
MAX_PITFALLS_CHARS = 240
MAX_NOTE_CHARS = 240
# The whole recipe as an agent reads it: about 400 tokens.
MAX_TOTAL_CHARS = 1600
FAILURES_TO_STOP = 2

_CURRENT = """recipe.id NOT IN (SELECT supersedes_id FROM task_recipes
                                WHERE supersedes_id IS NOT NULL)
              AND recipe.id NOT IN (SELECT recipe_id FROM task_recipe_retractions)"""


@dataclass(frozen=True)
class Recipe:
    id: str
    name: str
    scope: str
    steps: tuple[Step, ...]
    check: str
    pitfalls: str
    source: str
    provenance: dict[str, Any]
    caller: Caller
    device_id: str
    monotonic_seq: int
    supersedes_id: str | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scope": self.scope,
            "steps": [step.to_dict() for step in self.steps],
            "check": self.check,
            "pitfalls": self.pitfalls,
            "supersedes_id": self.supersedes_id,
            "created_at": _iso(self.created_at),
        }


def parse_recipe_steps(steps: Iterable[Any]) -> tuple[Step, ...]:
    """Steps from strings ('build: uv build --wheel') or {"step", "how"} dicts."""
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Iterable):
        raise ValueError("steps is a list: the steps that got the task done, in order")
    parsed: list[Step] = []
    for item in steps:
        if isinstance(item, Mapping):
            step, how = str(item.get("step") or ""), str(item.get("how") or "")
        elif isinstance(item, str):
            step, _, how = item.partition(": ")
        else:
            raise ValueError("a step is text ('build: uv build --wheel') or {step, how}")
        step = " ".join(step.split()).strip(" -:")
        how = " ".join(how.split())
        if not step:
            continue
        if len(step) + len(how) > MAX_STEP_CHARS:
            raise ValueError(
                f"a step and how it is done are at most {MAX_STEP_CHARS} characters together"
            )
        parsed.append(Step(step, how))
    if len(parsed) < MIN_STEPS:
        raise ValueError("a recipe needs at least two steps, in the order that worked")
    if len(parsed) > MAX_STEPS:
        raise ValueError(
            f"a recipe has at most {MAX_STEPS} steps: keep the ones the next agent "
            "would get wrong, not every command"
        )
    return tuple(parsed)


def render(recipe: Recipe | Mapping[str, Any]) -> str:
    """The recipe as an agent reads it; its length is what the size cap counts."""
    item = recipe.to_dict() if isinstance(recipe, Recipe) else recipe
    lines = [str(item["name"])]
    for number, step in enumerate(item["steps"], 1):
        how = step.get("how") if isinstance(step, Mapping) else ""
        text = step["step"] if isinstance(step, Mapping) else str(step)
        lines.append(f"{number}. {text}" + (f": {how}" if how else ""))
    lines.append(f"Check: {item['check']}")
    if item.get("pitfalls"):
        lines.append(f"Watch out: {item['pitfalls']}")
    return "\n".join(lines)


def _clean(value: str | None, limit: int, what: str) -> str:
    text = " ".join((value or "").split())
    if len(text) > limit:
        raise ValueError(f"{what} is at most {limit} characters")
    return text


def _scan(*texts: str) -> None:
    """A recipe is written by an agent mid-task: it must not carry a credential."""
    from varyz.privacy.secrets_scan import redact_tree

    _safe, findings = redact_tree(list(texts))
    if findings:
        raise ValueError(
            "the recipe contains what looks like a credential; write the step without "
            "it (name where the secret lives, not the secret)"
        )


def validated(
    name: str, steps: Iterable[Any], check: str, pitfalls: str, scope: str | None
) -> tuple[str, tuple[Step, ...], str, str, str]:
    """A recipe's fields cleaned, within the size cap and free of credentials."""
    name = " ".join(str(name).split()).strip(" -:")
    if not name:
        raise ValueError("a recipe needs a name: the task it does ('release varyz')")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"a recipe's name is at most {MAX_NAME_CHARS} characters")
    check = _clean(check, MAX_CHECK_CHARS, "check")
    if not check:
        raise ValueError("check is required: how the next agent tells the task worked")
    pitfalls = _clean(pitfalls, MAX_PITFALLS_CHARS, "pitfalls")
    parsed = parse_recipe_steps(steps)
    size = len(render({
        "name": name, "steps": [step.to_dict() for step in parsed],
        "check": check, "pitfalls": pitfalls,
    }))
    if size > MAX_TOTAL_CHARS:
        raise ValueError(
            f"the recipe is {size} characters; keep it under {MAX_TOTAL_CHARS} "
            "(about 400 tokens) by cutting steps any agent would do anyway"
        )
    _scan(name, check, pitfalls, *(f"{step.step} {step.how}" for step in parsed))
    return name, parsed, check, pitfalls, normalize_scope(scope)


def best_match(recipes: Iterable[Any], text: str, project: str | None) -> Any | None:
    """The recipe a task names, the project's own before a general one.

    Each recipe has ``name`` and ``scope``. It applies when the task carries most
    of its name's terms (all of them for a name of one or two terms) and its
    scope is general, the project, or a project the task names.
    """
    project_scope = normalize_scope(project) if project else None
    lowered = unicodedata.normalize("NFKC", text).casefold()
    task_terms = _terms(text)
    best: tuple[tuple[int, float, int], Any] | None = None
    for recipe in recipes:
        scope = normalize_scope(recipe.scope)
        specific = scope != GLOBAL_SCOPE
        if specific and scope != project_scope and not re.search(
            rf"(?<!\w){re.escape(scope)}(?!\w)", lowered
        ):
            continue
        wanted = _terms(recipe.name)
        if not wanted:
            continue
        shared = len(task_terms & wanted)
        need = len(wanted) if len(wanted) <= 2 else math.ceil(len(wanted) * 0.6)
        if shared < need:
            continue
        rank = (1 if specific else 0, shared / len(wanted), shared)
        if best is None or rank > best[0]:
            best = (rank, recipe)
    return best[1] if best else None


class SQLiteRecipeStore:
    """At most one current recipe per name and scope; the ledger keeps the rest."""

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
        check: str,
        caller: Caller,
        device_id: str,
        source: str,
        pitfalls: str = "",
        scope: str | None = GLOBAL_SCOPE,
        replaces: str | None = None,
        conversation_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> str:
        """A new recipe, or a better one for ``replaces`` (which is marked 'changed')."""
        if not source.strip():
            raise ValueError("a recipe needs a source")
        name, parsed, check, pitfalls, scope = validated(name, steps, check, pitfalls, scope)
        with _write_transaction(self._conn, self._lock):
            old = self._current_row(replaces) if replaces else self._current_for(name, scope)
            if (
                old is not None and old.steps == parsed and old.check == check
                and old.pitfalls == pitfalls and old.name == name and old.scope == scope
            ):
                return old.id
            recipe_id = self._insert(
                name=name, scope=scope, steps=parsed, check=check, pitfalls=pitfalls,
                source=source.strip(), provenance=provenance or {}, caller=caller,
                device_id=device_id, supersedes_id=old.id if old is not None else None,
            )
            if replaces and old is not None:
                self._outcome(old.id, "changed", "", conversation_id, caller, device_id)
        return recipe_id

    def outcome(
        self,
        recipe_id: str,
        outcome: str,
        *,
        caller: Caller,
        device_id: str,
        note: str = "",
        conversation_id: str | None = None,
    ) -> str:
        """How a served recipe did: 'worked', 'changed' or 'failed'."""
        if outcome not in OUTCOMES:
            raise ValueError("outcome is 'worked', 'changed' or 'failed'")
        note = _clean(note, MAX_NOTE_CHARS, "note")
        if note:
            _scan(note)
        with _write_transaction(self._conn, self._lock):
            if self.get(recipe_id) is None:
                raise KeyError(f"unknown recipe: {recipe_id}")
            return self._outcome(recipe_id, outcome, note, conversation_id, caller, device_id)

    def retract(
        self, recipe_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        retraction_id = new_id()
        with _write_transaction(self._conn, self._lock):
            if self._current_row(recipe_id) is None:
                raise ValueError(f"recipe {recipe_id} was already replaced or withdrawn")
            seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """INSERT INTO task_recipe_retractions
                   (id, recipe_id, reason, caller_agent_id, caller_display_name,
                    caller_purpose, caller_capability_id, caller_device_id,
                    caller_transport, device_id, monotonic_seq, retracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    retraction_id, recipe_id, reason.strip(), caller.agent_id,
                    caller.display_name, caller.purpose, caller.capability_id,
                    caller.device_id, caller.transport, device_id, seq,
                    _iso(self._clock()),
                ),
            )
        return retraction_id

    # -- reads ----------------------------------------------------------------

    def current(self) -> tuple[Recipe, ...]:
        rows = self._conn.execute(
            f"""SELECT recipe.* FROM task_recipes AS recipe WHERE {_CURRENT}
                ORDER BY recipe.created_at DESC, recipe.monotonic_seq DESC"""
        ).fetchall()
        return tuple(self._recipe(row) for row in rows)

    def get(self, recipe_id: str) -> Recipe | None:
        row = self._conn.execute(
            "SELECT * FROM task_recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        return self._recipe(row) if row is not None else None

    def uses(self, recipe_id: str) -> dict[str, int]:
        """How often a recipe and the ones it replaced were followed, by outcome."""
        ids = [recipe_id]
        seen = {recipe_id}
        while len(ids) < 100:
            row = self._conn.execute(
                "SELECT supersedes_id FROM task_recipes WHERE id = ?", (ids[-1],)
            ).fetchone()
            if row is None or not row[0] or row[0] in seen:
                break
            ids.append(row[0])
            seen.add(row[0])
        counts = dict.fromkeys(OUTCOMES, 0)
        marks = ",".join("?" * len(ids))
        for outcome, count in self._conn.execute(
            f"""SELECT outcome, COUNT(*) FROM task_recipe_outcomes
                WHERE recipe_id IN ({marks}) GROUP BY outcome""",
            ids,
        ):
            counts[outcome] = int(count)
        return counts

    def applicable(self, text: str, *, project: str | None = None) -> Recipe | None:
        """The one recipe this task names, the project's own before a general one.

        A recipe applies when the task carries most of its name's terms (all of
        them for a name of one or two terms), and not when its last uses failed.
        """
        usable = [recipe for recipe in self.current() if not self._failing(recipe.id)]
        return best_match(usable, text, project)

    # -- internals ------------------------------------------------------------

    def _failing(self, recipe_id: str) -> bool:
        rows = self._conn.execute(
            """SELECT outcome FROM task_recipe_outcomes WHERE recipe_id = ?
               ORDER BY recorded_at DESC, monotonic_seq DESC LIMIT ?""",
            (recipe_id, FAILURES_TO_STOP),
        ).fetchall()
        return len(rows) == FAILURES_TO_STOP and all(row[0] == "failed" for row in rows)

    def _current_for(self, name: str, scope: str) -> Recipe | None:
        row = self._conn.execute(
            f"""SELECT recipe.* FROM task_recipes AS recipe
                WHERE recipe.normalized_name = ? AND varyz_scope(recipe.scope) = ?
                  AND {_CURRENT}
                ORDER BY recipe.created_at DESC, recipe.monotonic_seq DESC LIMIT 1""",
            (normalize_name(name), scope),
        ).fetchone()
        return self._recipe(row) if row is not None else None

    def _current_row(self, recipe_id: str | None) -> Recipe | None:
        row = self._conn.execute(
            f"SELECT recipe.* FROM task_recipes AS recipe WHERE recipe.id = ? AND {_CURRENT}",
            (recipe_id,),
        ).fetchone()
        if row is not None:
            return self._recipe(row)
        if self.get(str(recipe_id)) is None:
            raise KeyError(f"unknown recipe: {recipe_id}")
        return None

    def _insert(
        self,
        *,
        name: str,
        scope: str,
        steps: tuple[Step, ...],
        check: str,
        pitfalls: str,
        source: str,
        provenance: Mapping[str, Any],
        caller: Caller,
        device_id: str,
        supersedes_id: str | None,
    ) -> str:
        recipe_id = new_id()
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO task_recipes
               (id, name, normalized_name, scope, steps_json, check_text, pitfalls,
                source, provenance_json, caller_agent_id, caller_display_name,
                caller_purpose, caller_capability_id, caller_device_id, caller_transport,
                device_id, monotonic_seq, supersedes_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recipe_id, name, normalize_name(name), scope,
                json.dumps([step.to_dict() for step in steps], ensure_ascii=False),
                check, pitfalls, source,
                json.dumps(dict(provenance), ensure_ascii=False, sort_keys=True),
                caller.agent_id, caller.display_name, caller.purpose,
                caller.capability_id, caller.device_id, caller.transport, device_id,
                seq, supersedes_id, _iso(self._clock()),
            ),
        )
        return recipe_id

    def _outcome(
        self, recipe_id: str, outcome: str, note: str, conversation_id: str | None,
        caller: Caller, device_id: str,
    ) -> str:
        outcome_id = new_id()
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO task_recipe_outcomes
               (id, recipe_id, outcome, note, conversation_id, caller_agent_id,
                caller_display_name, caller_purpose, caller_capability_id,
                caller_device_id, caller_transport, device_id, monotonic_seq,
                recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                outcome_id, recipe_id, outcome, note, conversation_id, caller.agent_id,
                caller.display_name, caller.purpose, caller.capability_id,
                caller.device_id, caller.transport, device_id, seq, _iso(self._clock()),
            ),
        )
        return outcome_id

    @staticmethod
    def _recipe(row: sqlite3.Row) -> Recipe:
        return Recipe(
            id=row["id"],
            name=row["name"],
            scope=stored_scope(row["scope"]),
            steps=tuple(
                Step(str(item.get("step", "")), str(item.get("how", "")))
                for item in json.loads(row["steps_json"])
            ),
            check=row["check_text"],
            pitfalls=row["pitfalls"],
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


RECORD_HINT = (
    "At finish_check record how it went with record_recipe: recipe_id and outcome "
    "'worked', 'failed' (with a short note), or 'changed' (pass the corrected steps "
    "and replaces=recipe_id)."
)

OFFER = (
    "If this task took working out (a failed first attempt, a search for how, a fix "
    "to your first approach) and it is now done and verified, call record_recipe "
    "once: the task's name, at most 8 short steps that got it done, the check that "
    "proved it worked, and the trap that cost time. Leave out steps any agent "
    "would take anyway. Skip it for routine work."
)


def _brief(
    recipe_id: str, name: str, scope: str, steps: Iterable[Any], check: str,
    pitfalls: str, worked: int,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": recipe_id,
        "name": name,
        "scope": "all projects" if normalize_scope(scope) == GLOBAL_SCOPE else scope,
        "steps": [
            (step.step + (f": {step.how}" if step.how else "")) if isinstance(step, Step)
            else str(step["step"]) + (f": {step['how']}" if step.get("how") else "")
            for step in steps
        ],
        "check": check,
    }
    if pitfalls:
        item["pitfalls"] = pitfalls
    if worked:
        item["worked_before"] = worked
    item["guidance"] = (
        "A past agent worked this task out; follow these steps unless the task "
        "differs. " + RECORD_HINT
    )
    return item


def hosted(
    records: Iterable[Mapping[str, Any]], task: str, project: str | None
) -> dict[str, Any] | None:
    """The recipe for a hosted (Cloud) start_task, from synced recipe records."""
    from types import SimpleNamespace

    candidates = [
        SimpleNamespace(id=record["id"], **record["payload"])
        for record in records
        if record.get("kind") == "recipe" and isinstance(record.get("payload"), Mapping)
    ]
    match = best_match(candidates, task, project)
    if match is None:
        return None
    return _brief(
        match.id, match.name, match.scope, match.steps, match.check,
        getattr(match, "pitfalls", ""), 0,
    )


def served(conn: sqlite3.Connection, task: str, project: str | None) -> dict[str, Any] | None:
    """The recipe for start_task, compact; ``None`` when there is none or no table."""
    try:
        store = SQLiteRecipeStore(conn)
        recipe = store.applicable(task, project=project)
        if recipe is None:
            return None
        uses = store.uses(recipe.id)
    except sqlite3.OperationalError:  # a database not yet migrated to 0051
        return None
    return _brief(
        recipe.id, recipe.name, recipe.scope, recipe.steps, recipe.check,
        recipe.pitfalls, uses["worked"],
    )
