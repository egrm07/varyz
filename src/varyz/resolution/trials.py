"""Trial rules: strong mined rules go into effect before the owner has read them.

A reading of the owner's turns queues a few hundred candidates, and about one in
eight is wrong. Asking the owner to read all of them is the chore that keeps them
unread, so a candidate with strong evidence behind it skips the queue: it goes
into effect on trial, and agents are told it is not yet the owner's word.

Strong evidence is what a mistaken reading rarely produces (``assess``): the owner
corrected an agent over it, or said it in two separate conversations, or in two
projects, or said it would hold from now on ("always", "from now on", "every
time": the owner's own word that it is a rule, read live as they say it).
Visions, decisions and workflows always wait for the owner, as does a rule that
loosens what an agent may do with private data, money, publishing or deleting,
and one close to a rule already in effect.

A trial rule ends one of four ways:

* the owner corrects an agent that followed it: ``remember_owner_rule`` with
  ``replaces`` supersedes it, as with any rule;
* enough task exposures make it eligible for review; silence never confirms it;
* the owner keeps or drops it in a spot-check: `varyz rules review` shows a
  random sample of trial rules beside the candidates;
* the owner undoes the trial lane (`varyz rules trials --undo-since`).

The spot-check is the measurement: what share of sampled trial rules the owner
keeps. Once ``min_audited`` were checked and fewer than ``min_kept`` of them were
kept, no new rule goes on trial until the owner raises the bar or resumes it; the
``trials.off`` sentinel stops it at once (`varyz rules trials --off`).
"""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from varyz.domain import utc_now
from varyz.resolution.knowledge import _datetime, _iso, _terms
from varyz.resolution.models import Caller
from varyz.resolution.principles import GLOBAL_SCOPE, Principle, SQLitePrincipleStore
from varyz.resolution.rule_miner import _GENERAL

# The readings that read the owner's turns with a model (profile_reader.SOURCES).
# Rules matched by words alone (`--lexical`, instruction files) queued sentence
# fragments and vendored text before, so they always wait for the owner.
MODEL_SOURCES = ("model:profile", "cloud:profile", "agent:profile")
# Slots that may go on trial. A vision or a line never to cross is how the owner
# sees a project; a decision becomes an answer `ask` gives; a workflow, the
# owner's order of work. A wrong one of those costs more than asking.
TRIAL_SLOTS = ("rule", "done")
# Why a trial rule was withdrawn: kept apart from the owner's own retractions.
TRIAL_DROPPED = "dropped in a spot-check of trial rules"
TRIAL_UNDONE = "trial rules undone by the owner"
# How close (shared words over all words) a candidate may come to a rule already
# in effect before the owner is asked: it may restate it or contradict it.
OVERLAP_LIMIT = 0.5

# What an agent may do with private data, money, publishing and deleting: a
# wrong rule here that *allows* something is costly; one that forbids it only
# makes the agent ask. Such a rule goes on trial only when it restricts.
_SENSITIVE = re.compile(
    r"\b(privacy|private|personal data|secret|password|credential|token|api key|"
    r"cloud|upload|send|share|email|publish|post|deploy|release|push|pay|payment|"
    r"spend|money|price|billing|purchase|delete|remove|drop|wipe|force|production)\w*",
    re.IGNORECASE,
)
_RESTRICTS = re.compile(
    r"\b(never|don'?t|do not|no|not|without asking|ask (me |the owner )?(first|before)|"
    r"only (after|with|when)|unless|must not|avoid)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TrialSettings:
    enabled: bool = True
    promote_tasks: int = 3
    promote_days: int = 7
    spot_check: int = 10
    min_kept: float = 0.9
    # Fewer checked than this says too little to pause on.
    min_audited: int = 5

    @classmethod
    def from_config(cls, cfg: Any) -> TrialSettings:
        workflows = getattr(cfg, "workflows", None)
        if workflows is None:
            return cls()
        return cls(
            enabled=bool(getattr(workflows, "trial_rules", True)),
            promote_tasks=int(getattr(workflows, "trial_promote_tasks", 3)),
            promote_days=int(getattr(workflows, "trial_promote_days", 7)),
            spot_check=int(getattr(workflows, "trial_spot_check", 10)),
            min_kept=float(getattr(workflows, "trial_min_kept", 0.9)),
        )


@dataclass
class Verdict:
    """Which lane a candidate belongs in, and why."""

    trial: bool
    signals: list[str] = field(default_factory=list)
    holds_back: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"trial": self.trial, "signals": self.signals, "held_back": self.holds_back}


def off_path(db_path: Path | str) -> Path:
    """The sentinel that stops new trial rules: beside the archive, under rules/."""
    return Path(db_path).parent / "rules" / "trials.off"


def system_caller(device_id: str) -> Caller:
    """Who ends a trial when its tasks passed: Varyz itself, on the owner's device."""
    return Caller(
        agent_id="varyz:trials", display_name="Varyz trial rules",
        purpose="owner_knowledge", device_id=device_id, transport="local",
    )


def _quotes(principle: Principle) -> list[dict[str, Any]]:
    quotes = principle.provenance.get("quotes")
    return [quote for quote in quotes if isinstance(quote, dict)] \
        if isinstance(quotes, list) else []


def _slot(principle: Principle) -> str:
    slot = principle.provenance.get("slot")
    if isinstance(slot, str):
        return slot
    if principle.kind == "vision":
        return "vision"
    return "done" if principle.domain else "rule"


def evidence(principle: Principle) -> dict[str, int]:
    """How much stands behind a candidate: conversations, projects, corrections."""
    quotes = _quotes(principle)
    provenance = principle.provenance
    threads = provenance.get("threads")
    if not isinstance(threads, int):
        threads = len({str(quote.get("thread") or quote.get("entry_id")) for quote in quotes})
    lifted = provenance.get("lifted_from")
    projects = len(lifted) if isinstance(lifted, list) and lifted else len(
        {str(quote["project"]) for quote in quotes if quote.get("project")}
    )
    corrections = provenance.get("corrections")
    return {
        "conversations": threads,
        "projects": projects,
        "corrections": len(corrections) if isinstance(corrections, list) else 0,
        "messages": int(provenance.get("messages") or len(quotes)),
        # Quotes that say it holds from now on, not about the moment.
        "standing": sum(
            1 for quote in quotes
            if not quote.get("moment") and _GENERAL.search(str(quote.get("quote") or ""))
        ),
    }


def assess(candidate: Principle, in_effect: Iterable[Principle] = ()) -> Verdict:
    """Whether a candidate may go into effect on trial, by the evidence behind it."""
    verdict = Verdict(trial=False)
    if candidate.source not in MODEL_SOURCES:
        verdict.holds_back.append("not read by a model")
    slot = _slot(candidate)
    if slot not in TRIAL_SLOTS:
        verdict.holds_back.append(f"a {slot}: the owner's to keep")
    if any(quote.get("moment") for quote in _quotes(candidate)):
        verdict.holds_back.append("said partly about the moment")
    statement = candidate.statement
    if _SENSITIVE.search(statement) and not _RESTRICTS.search(statement):
        verdict.holds_back.append("loosens what an agent may do with something sensitive")
    terms = _terms(statement)
    for rule in in_effect:
        if rule.scope not in {GLOBAL_SCOPE, candidate.scope}:
            continue
        other = _terms(rule.statement)
        if terms and other and len(terms & other) / len(terms | other) >= OVERLAP_LIMIT:
            verdict.holds_back.append(f"close to a rule in effect: {rule.statement[:80]}")
            break
    found = evidence(candidate)
    if found["corrections"]:
        verdict.signals.append("corrected an agent")
    if found["conversations"] >= 2:
        verdict.signals.append(f"said in {found['conversations']} conversations")
    if found["projects"] >= 2:
        verdict.signals.append(f"said in {found['projects']} projects")
    if found["standing"]:
        verdict.signals.append("said to hold from now on")
    if not verdict.signals:
        verdict.holds_back.append("one conversation and no correction")
    verdict.trial = bool(verdict.signals) and not verdict.holds_back
    return verdict


def task_key(conversation_id: str | None, task: str) -> str:
    """What makes two briefings one task: the conversation, else the task's words."""
    if conversation_id:
        return f"conversation:{conversation_id}"
    return "task:" + hashlib.sha256(" ".join(task.split()).casefold().encode()).hexdigest()[:24]


class TrialRules:
    """The trial lane over the owner's rule ledger."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        store: SQLitePrincipleStore | None = None,
        *,
        settings: TrialSettings | None = None,
        off: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.conn = conn
        self.store = store or SQLitePrincipleStore(conn, clock=clock)
        self.settings = settings or TrialSettings()
        self.off = off
        self.clock = clock

    # -- going on trial --------------------------------------------------------

    def paused(self) -> str | None:
        """Why no new rule goes on trial now, or ``None`` when they do."""
        if not self.settings.enabled:
            return "turned off in config ([workflows] trial_rules = false)"
        if self.off is not None and self.off.exists():
            return "stopped by the owner (`varyz rules trials --on` resumes)"
        checked = self._outcomes()
        audited = checked["kept"] + checked["dropped"]
        if audited >= self.settings.min_audited:
            kept = checked["kept"] / audited
            if kept < self.settings.min_kept:
                return (f"the spot-check kept {kept:.0%} of {audited} trial rules, under "
                        f"{self.settings.min_kept:.0%}")
        return None

    def consider(self, candidate_id: str, *, caller: Caller, device_id: str) -> str | None:
        """Put a just-queued candidate on trial when its evidence holds; the trial id."""
        if self.paused() is not None:
            return None
        candidate = self.store.get(candidate_id)
        if candidate is None or candidate.status != "candidate":
            return None
        verdict = assess(candidate, self.store.current("confirmed"))
        if not verdict.trial:
            return None
        try:
            return self.store.start_trial(
                candidate_id, caller=caller, device_id=device_id, signals=verdict.signals,
            )
        except ValueError:  # superseded or withdrawn meanwhile
            return None

    def scan(self, *, apply: bool, caller: Caller, device_id: str) -> dict[str, Any]:
        """Sort every open candidate into its lane; with ``apply``, start the trials."""
        why_paused = self.paused()
        lanes: dict[str, list[dict[str, Any]]] = {"trial": [], "review": []}
        started = 0
        for candidate in self.store.current("candidate"):
            verdict = assess(candidate, self.store.current("confirmed"))
            lane = "trial" if verdict.trial else "review"
            lanes[lane].append({"id": candidate.id, "statement": candidate.statement,
                                "scope": candidate.scope, **verdict.to_dict()})
            if lane == "trial" and apply and why_paused is None:
                try:
                    self.store.start_trial(candidate.id, caller=caller, device_id=device_id,
                                           signals=verdict.signals)
                    started += 1
                except ValueError:
                    continue
        return {"paused": why_paused, "started": started, **lanes}

    # -- in effect -------------------------------------------------------------

    def trials(self) -> tuple[Principle, ...]:
        return tuple(rule for rule in self.store.current("confirmed") if rule.trial)

    def record_uses(
        self, ids: Collection[str], key: str, *, device_id: str
    ) -> list[str]:
        """Record exposure only. A briefing is neither compliance nor owner approval."""
        on_trial = {rule.id: rule for rule in self.trials()}
        now = _iso(self.clock())
        wanted = [rule_id for rule_id in dict.fromkeys(ids) if rule_id in on_trial]
        if not wanted:
            return []
        with self.conn:
            self.conn.executemany(
                """INSERT OR IGNORE INTO owner_principle_uses (principle_id, task_key, shown_at)
                   VALUES (?, ?, ?)""",
                [(rule_id, key[:200], now) for rule_id in wanted],
            )
        return self.promote_due(device_id=device_id, among=wanted)

    def uses(self, principle_id: str) -> int:
        return int(self.conn.execute(
            "SELECT count(*) FROM owner_principle_uses WHERE principle_id = ?", (principle_id,)
        ).fetchone()[0])

    def promote_due(
        self, *, device_id: str, among: Collection[str] | None = None
    ) -> list[str]:
        """Compatibility entry point: only an explicit keep/correction ends a trial.

        Task exposure and elapsed time remain review signals, never confirmation.
        Existing historical promotion records are retained for audit, not rewritten.
        """
        return []

    # -- the owner's view ------------------------------------------------------

    def spot_check(self, seed: str = "") -> tuple[Principle, ...]:
        """A random sample of the rules on trial for the owner to keep or drop."""
        rules = sorted(self.trials(), key=lambda rule: rule.id)
        size = min(self.settings.spot_check, len(rules))
        return tuple(random.Random(seed).sample(rules, size)) if size else ()

    def undo(self, since: datetime, *, caller: Caller, device_id: str) -> int:
        """Withdraw every rule still on trial that went into effect at ``since`` or later."""
        undone = 0
        for rule in self.trials():
            started = _datetime(str(rule.provenance["trial"].get("since")))
            if started < since:
                continue
            try:
                self.store.retract(rule.id, caller=caller, device_id=device_id,
                                   reason=TRIAL_UNDONE)
                undone += 1
            except ValueError:
                continue
        return undone

    def _outcomes(self) -> dict[str, int]:
        """How every trial so far ended, from the ledger."""
        counts = {"on_trial": 0, "promoted": 0, "kept": 0, "dropped": 0, "corrected": 0,
                  "withdrawn": 0, "undone": 0}
        rows = self.conn.execute(
            """SELECT trial.id,
                      later.id AS later_id,
                      json_extract(later.provenance_json, '$.trial_ended.how') AS how,
                      json_extract(later.provenance_json, '$.corrects') AS corrects,
                      retraction.reason AS reason
               FROM owner_principles AS trial
               LEFT JOIN owner_principles AS later ON later.supersedes_id = trial.id
               LEFT JOIN owner_principle_retractions AS retraction
                      ON retraction.principle_id = trial.id
               WHERE trial.status = 'confirmed'
                 AND json_extract(trial.provenance_json, '$.trial') IS NOT NULL"""
        ).fetchall()
        for row in rows:
            if row["reason"] is not None:
                key = {TRIAL_DROPPED: "dropped", TRIAL_UNDONE: "undone"}.get(
                    row["reason"], "withdrawn")
            elif row["later_id"] is None:
                key = "on_trial"
            elif row["how"] in {"promoted", "kept"}:
                key = str(row["how"])
            else:
                # Replaced by a different wording: the owner corrected it.
                key = "corrected"
            counts[key] += 1
        return counts

    def status(self) -> dict[str, Any]:
        outcomes = self._outcomes()
        audited = outcomes["kept"] + outcomes["dropped"]
        return {
            "paused": self.paused(),
            "settings": {
                "promote_tasks": self.settings.promote_tasks,
                "promote_days": self.settings.promote_days,
                "spot_check": self.settings.spot_check,
                "min_kept": self.settings.min_kept,
            },
            **outcomes,
            "spot_checked": audited,
            "kept_share": round(outcomes["kept"] / audited, 3) if audited else None,
            "rules": [
                {"id": rule.id, "statement": rule.statement, "scope": rule.scope,
                 "since": str(rule.provenance["trial"].get("since"))[:10],
                 "signals": list(rule.provenance["trial"].get("signals") or []),
                 "tasks": self.uses(rule.id)}
                for rule in self.trials()
            ],
        }


__all__ = [
    "MODEL_SOURCES",
    "TRIAL_DROPPED",
    "TRIAL_UNDONE",
    "TrialRules",
    "TrialSettings",
    "Verdict",
    "assess",
    "evidence",
    "off_path",
    "system_caller",
    "task_key",
]
