"""How the owner builds things: the standing rules an agent is advised with.

Varyz does not plan or judge work; the coding agent does. What varyz holds is the
owner's authority on *how* it should be done — "dry-run first", "no commits unless
asked", a project's vision and non-goals — and it hands the applicable part of that
to the agent before the agent plans, so the agent builds it the owner's way.

Only confirmed rules reach an agent. A rule mined from the owner's own words is a
candidate until the owner confirms it, with one exception: a candidate with strong
evidence behind it goes into effect on trial (``varyz.resolution.trials``), a
confirmed row marked ``trial`` that agents are told is not yet the owner's word,
until enough tasks pass without a correction or the owner keeps or drops it in a
spot-check. The ledger is append-only: confirming supersedes a candidate (or a
trial rule), and rejecting or withdrawing appends a retraction.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.resolution.knowledge import (
    GLOBAL_SCOPE,
    _datetime,
    _iso,
    _next_monotonic_seq,
    _write_transaction,
    fold_stored_scopes,
    normalize_scope,
    stored_scope,
)
from varyz.resolution.models import Caller

KINDS = ("rule", "vision")
# The reason a reader's unreviewed candidate is withdrawn when a newer reading
# replaces it (profile_reader.REPLACED).
REPLACED_READING = "replaced by a newer reading of your turns"
# The reason a project's open candidate is withdrawn when the same rule, learned
# in another project too, is queued once for every project (``_lift``).
LIFTED = "learned in more than one project; asked once for all projects"
# What a rule applies to: how the owner works with agents on any task, their
# taste in everything they build, or one project.
APPLIES_TO = ("work", "products", "project")
MAX_STATEMENT_CHARS = 500
MAX_RATIONALE_CHARS = 500

# Setup seeded 63 starter rules until 2026-09-29. They were never the owner's own,
# and Varyz ships no rules of its own, so they no longer apply: measured on
# 2026-10-02, 62 of the 64 rules briefing the owner's agents were these, and agents
# followed them over what they had learned from the owner. The rows stay for audit.
STARTER_SOURCE = "standards_seed"
_CURRENT = f"""principle.source != '{STARTER_SOURCE}'
              AND principle.id NOT IN (SELECT supersedes_id FROM owner_principles
                                       WHERE supersedes_id IS NOT NULL)
              AND principle.id NOT IN (SELECT principle_id
                                       FROM owner_principle_retractions)"""


def normalize_statement(statement: str) -> str:
    """Identity of a rule's wording: case, spacing and closing punctuation ignored."""
    normalized = unicodedata.normalize("NFKC", statement).casefold()
    normalized = " ".join(normalized.split())
    return normalized.rstrip(".!;:").rstrip()


def near_statement(statement: str) -> str:
    """Identity of a rule's meaning as typed: ``normalize_statement`` with only its
    letters and digits kept, so "Never use em-dashes" and "never use em dashes!"
    are one rule."""
    return re.sub(r"[\W_]+", "", normalize_statement(statement))


def normalize_domain(domain: str | None) -> str:
    """'' for a rule that applies to any task, else a kind of work ('website')."""
    value = unicodedata.normalize("NFKC", domain or "").casefold().strip()
    value = re.sub(r"[\s_]+", "-", value)
    if value in {"", "*", "any", "all", "general"}:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise ValueError("a domain is a short slug such as 'website' or 'python-tool'")
    return value


@dataclass(frozen=True)
class Principle:
    id: str
    statement: str
    kind: str
    scope: str
    rationale: str
    status: str
    source: str
    provenance: Mapping[str, Any]
    caller: Caller
    device_id: str
    monotonic_seq: int
    supersedes_id: str | None
    created_at: datetime
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provenance"] = dict(self.provenance)
        value["caller"] = self.caller.model_dump(mode="json")
        value["created_at"] = _iso(self.created_at)
        return value

    def advice(self) -> dict[str, Any]:
        """What an agent is shown: the rule and its authority, not the audit trail."""
        value: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "statement": self.statement,
            "scope": "all projects" if self.scope == GLOBAL_SCOPE else self.scope,
            "why": self.rationale or None,
            "source": self.source,
            "since": _iso(self.created_at)[:10],
            "authority": "learned_trial" if self.trial else "learned_unconfirmed"
            if self.provenance.get("trial_ended", {}).get("how") == "promoted"
            else "owner_confirmed",
        }
        if self.domain:
            value["domain"] = self.domain
        if self.trial:
            value["trial"] = True
        return value

    @property
    def trial(self) -> bool:
        """In effect on trial: mined, not yet kept by the owner (``trials``)."""
        return self.status == "confirmed" and isinstance(self.provenance.get("trial"), dict)


@dataclass(frozen=True)
class Applicable:
    principle: Principle
    matched_terms: tuple[str, ...]


class SQLitePrincipleStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Any = utc_now) -> None:
        self._conn = conn
        fold_stored_scopes(conn)
        self._clock = clock
        self._lock = threading.RLock()

    # -- writes ---------------------------------------------------------------

    def add(
        self,
        *,
        statement: str,
        caller: Caller,
        device_id: str,
        source: str,
        kind: str = "rule",
        scope: str | None = GLOBAL_SCOPE,
        rationale: str = "",
        provenance: Mapping[str, Any] | None = None,
        supersedes_id: str | None = None,
        domain: str | None = "",
    ) -> str:
        """Record a confirmed rule; an identical current one is returned, not doubled.

        ``domain`` makes the rule part of one kind of work's definition of done
        ('website': favicon, SEO, ...), handed over only when the task is that work.
        """
        statement, kind, scope, rationale, source = self._validated(
            statement, kind, scope, rationale, source
        )
        domain = normalize_domain(domain)
        with _write_transaction(self._conn, self._lock):
            existing = self._conn.execute(
                f"""SELECT principle.id FROM owner_principles AS principle
                    WHERE principle.normalized_statement = ? AND varyz_scope(principle.scope) = ?
                      AND principle.domain = ?
                      AND principle.status = 'confirmed' AND {_CURRENT}""",
                (normalize_statement(statement), scope, domain),
            ).fetchone()
            if existing is not None and supersedes_id is None:
                return str(existing[0])
            return self._insert(
                statement=statement, kind=kind, scope=scope, rationale=rationale,
                status="confirmed", source=source, provenance=provenance or {},
                caller=caller, device_id=device_id, supersedes_id=supersedes_id,
                domain=domain,
            )

    def propose(
        self,
        *,
        statement: str,
        caller: Caller,
        device_id: str,
        source: str,
        kind: str = "rule",
        scope: str | None = GLOBAL_SCOPE,
        rationale: str = "",
        provenance: Mapping[str, Any] | None = None,
        domain: str | None = "",
    ) -> str | None:
        """Queue a candidate for the owner; ``None`` when this wording was seen before.

        Any earlier row counts — confirmed, rejected or withdrawn — so re-mining the
        same history never asks the owner the same thing twice. Two rows do not
        count: a starter rule, since the owner saying it is new, and what was
        withdrawn as ``REPLACED_READING`` (with the candidate a withdrawn rule was
        kept from): set aside for a newer reading, which may queue its wording again.

        A rule learned in a second project is no project's own: it is lifted to a
        candidate for every project (``_lift``), still for the owner to keep.
        """
        statement, kind, scope, rationale, source = self._validated(
            statement, kind, scope, rationale, source
        )
        domain = normalize_domain(domain)
        with _write_transaction(self._conn, self._lock):
            lifted = self._lift(
                statement=statement, kind=kind, scope=scope, rationale=rationale,
                source=source, provenance=provenance or {}, caller=caller,
                device_id=device_id, domain=domain,
            )
            if lifted is not None:
                return lifted or None
            seen = self._conn.execute(
                """WITH RECURSIVE set_aside(id) AS (
                       SELECT principle_id FROM owner_principle_retractions WHERE reason = ?
                       UNION
                       SELECT principle.supersedes_id FROM owner_principles AS principle
                       JOIN set_aside ON principle.id = set_aside.id
                       WHERE principle.supersedes_id IS NOT NULL)
                   SELECT 1 FROM owner_principles AS principle
                   WHERE principle.normalized_statement = ?
                     AND principle.source != ?
                     AND principle.id NOT IN (SELECT id FROM set_aside)
                   LIMIT 1""",
                (REPLACED_READING, normalize_statement(statement), STARTER_SOURCE),
            ).fetchone()
            if seen is not None:
                return None
            return self._insert(
                statement=statement, kind=kind, scope=scope, rationale=rationale,
                status="candidate", source=source, provenance=provenance or {},
                caller=caller, device_id=device_id, supersedes_id=None, domain=domain,
            )

    def confirm(
        self,
        candidate_id: str,
        *,
        caller: Caller,
        device_id: str,
        statement: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
        rationale: str | None = None,
        domain: str | None = None,
        applies_to: str | None = None,
    ) -> str:
        """The owner accepts a candidate, optionally reworded or rescoped.

        ``applies_to`` says what a rule for every project is about: 'work' (how the
        owner works with agents) or 'products' (their taste in what they build).
        A rule in effect on trial is kept the same way, from a spot-check.
        """
        if applies_to is not None and applies_to not in APPLIES_TO:
            raise ValueError(f"applies_to must be one of {', '.join(APPLIES_TO)}")
        with _write_transaction(self._conn, self._lock):
            candidate = self._current_row(candidate_id)
            if candidate.trial:
                provenance = _end_trial(candidate, "kept", self._clock())
            elif candidate.status != "candidate":
                raise ValueError(f"{candidate_id} is not a pending candidate")
            else:
                provenance = {**candidate.provenance, "candidate_source": candidate.source}
            if applies_to is not None:
                provenance["applies_to"] = applies_to
            new_statement, new_kind, new_scope, new_rationale, source = self._validated(
                statement if statement is not None else candidate.statement,
                kind if kind is not None else candidate.kind,
                scope if scope is not None else candidate.scope,
                rationale if rationale is not None else candidate.rationale,
                candidate.source,
            )
            return self._insert(
                statement=new_statement, kind=new_kind, scope=new_scope,
                rationale=new_rationale, status="confirmed", source=source,
                provenance=provenance, caller=caller, device_id=device_id,
                supersedes_id=candidate_id,
                domain=normalize_domain(domain if domain is not None else candidate.domain),
            )

    def start_trial(
        self,
        candidate_id: str,
        *,
        caller: Caller,
        device_id: str,
        signals: Collection[str],
    ) -> str:
        """Put a candidate into effect on trial: a confirmed row that supersedes it.

        ``signals`` are the evidence that let it skip the review queue
        (``trials.assess``); they stay in the row for the owner's spot-check.
        """
        with _write_transaction(self._conn, self._lock):
            candidate = self._current_row(candidate_id)
            if candidate.status != "candidate":
                raise ValueError(f"{candidate_id} is not a pending candidate")
            provenance = {
                **candidate.provenance,
                "candidate_source": candidate.source,
                "trial": {"since": _iso(self._clock()), "signals": sorted(signals)},
            }
            return self._insert(
                statement=candidate.statement, kind=candidate.kind, scope=candidate.scope,
                rationale=candidate.rationale, status="confirmed", source=candidate.source,
                provenance=provenance, caller=caller, device_id=device_id,
                supersedes_id=candidate_id, domain=candidate.domain,
            )

    def end_trial(
        self, trial_id: str, *, caller: Caller, device_id: str, how: str,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        """Confirm a trial rule for good, as it is: ``how`` says why ('promoted')."""
        with _write_transaction(self._conn, self._lock):
            trial = self._current_row(trial_id)
            if not trial.trial:
                raise ValueError(f"{trial_id} is not a rule on trial")
            return self._insert(
                statement=trial.statement, kind=trial.kind, scope=trial.scope,
                rationale=trial.rationale, status="confirmed", source=trial.source,
                provenance=_end_trial(trial, how, self._clock(), detail),
                caller=caller, device_id=device_id, supersedes_id=trial_id,
                domain=trial.domain,
            )

    def retract(
        self, principle_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        """Withdraw a rule or reject a candidate; the original row stays for audit."""
        with _write_transaction(self._conn, self._lock):
            return self._retract(
                principle_id, caller=caller, device_id=device_id, reason=reason
            )

    def _retract(
        self, principle_id: str, *, caller: Caller, device_id: str, reason: str
    ) -> str:
        retraction_id = new_id()
        self._current_row(principle_id)
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO owner_principle_retractions
               (id, principle_id, reason, caller_agent_id, caller_display_name,
                caller_purpose, caller_capability_id, caller_device_id,
                caller_transport, device_id, monotonic_seq, retracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                retraction_id, principle_id, reason.strip(), caller.agent_id,
                caller.display_name, caller.purpose, caller.capability_id,
                caller.device_id, caller.transport, device_id, seq,
                _iso(self._clock()),
            ),
        )
        return retraction_id

    def _lift(
        self,
        *,
        statement: str,
        kind: str,
        scope: str,
        rationale: str,
        source: str,
        provenance: Mapping[str, Any],
        caller: Caller,
        device_id: str,
        domain: str,
    ) -> str | None:
        """Lift a rule learned in another project too to every project.

        Measured on 2026-10-02, about a third of the owner's corrections were
        learned but filed under the one project they were said in ("never use
        em-dashes" under ``website``), so agents in every other project repeated
        them. When the same or nearly the same wording (``near_statement``) is a
        current candidate or rule of a different project, a candidate for all
        projects is queued: it replaces the other project's open candidate, and
        sits beside a kept one, which stays until the owner keeps the wider one.
        Returns the new id, ``""`` when the rule already holds everywhere, or
        ``None`` when there is nothing to lift. A vision is one project's and
        never lifted; nothing is confirmed here.
        """
        if kind == "vision" or scope == GLOBAL_SCOPE:
            return None
        key = near_statement(statement)
        if not key:
            return None
        elsewhere: list[Principle] = []
        for row in self._conn.execute(
            f"""SELECT principle.* FROM owner_principles AS principle
                WHERE principle.kind = 'rule' AND {_CURRENT}"""
        ):
            other = self._principle(row)
            if near_statement(other.statement) != key or other.domain != domain:
                continue
            if other.scope == GLOBAL_SCOPE:
                return ""
            if other.scope != scope:
                elsewhere.append(other)
        if not elsewhere:
            return None
        projects = sorted({scope, *(other.scope for other in elsewhere)})
        open_candidates = [other for other in elsewhere if other.status == "candidate"]
        replaced = open_candidates[0] if open_candidates else None
        lifted_provenance = {**provenance, "lifted_from": projects}
        if lifted_provenance.get("applies_to") in (None, "project"):
            lifted_provenance["applies_to"] = "work"
        lifted_id = self._insert(
            statement=statement, kind=kind, scope=GLOBAL_SCOPE,
            rationale=rationale or (replaced.rationale if replaced else ""),
            status="candidate", source=source, provenance=lifted_provenance,
            caller=caller, device_id=device_id,
            supersedes_id=replaced.id if replaced else None, domain=domain,
        )
        for other in open_candidates[1:]:
            self._retract(other.id, caller=caller, device_id=device_id, reason=LIFTED)
        return lifted_id

    # -- reads ----------------------------------------------------------------

    def withdrawn(self) -> tuple[dict[str, Any], ...]:
        """Withdrawn rules and dropped suggestions, with their reason and date.

        Superseded versions and suggestions converted into other record types
        are not withdrawn rules.
        """
        rows = self._conn.execute(
            """SELECT principle.*, retraction.reason, retraction.retracted_at
               FROM owner_principles AS principle
               JOIN owner_principle_retractions AS retraction
                 ON retraction.principle_id = principle.id
               WHERE principle.source != ?
                 AND principle.id NOT IN (SELECT supersedes_id FROM owner_principles
                                          WHERE supersedes_id IS NOT NULL)
                 AND retraction.reason NOT IN ('kept as a workflow', 'kept as a decision')
               ORDER BY retraction.retracted_at DESC""",
            (STARTER_SOURCE,),
        ).fetchall()
        return tuple({**self._principle(row).to_dict(), "status": "withdrawn",
                      "withdrawn_reason": row["reason"],
                      "withdrawn_at": row["retracted_at"]} for row in rows)

    def current(self, status: str = "confirmed") -> tuple[Principle, ...]:
        rows = self._conn.execute(
            f"""SELECT principle.* FROM owner_principles AS principle
                WHERE principle.status = ? AND {_CURRENT}
                ORDER BY principle.created_at DESC, principle.monotonic_seq DESC""",
            (status,),
        ).fetchall()
        return tuple(self._principle(row) for row in rows)

    def rule_domains(self) -> tuple[str, ...]:
        """Every kind of work the owner's confirmed rules are filed under, sorted:
        the owner's own kinds ('gardening') reach ``detect_domains`` this way."""
        return tuple(sorted({p.domain for p in self.current("confirmed") if p.domain}))

    def get(self, principle_id: str) -> Principle | None:
        row = self._conn.execute(
            "SELECT * FROM owner_principles WHERE id = ?", (principle_id,)
        ).fetchone()
        return self._principle(row) if row is not None else None

    def applicable(
        self,
        text: str,
        *,
        project: str | None = None,
        limit: int = 12,
        domains: Collection[str] = (),
    ) -> tuple[tuple[Applicable, ...], int]:
        """Confirmed rules that bear on a task, and how many were left out.

        Scope is a hard filter: a rule for another project never applies. Within
        scope, rules sharing terms with the task come first; standing rules with
        no shared term still fill the remaining places, because "dry-run first"
        governs a task that never says "dry-run". Relevance orders; it does not
        judge — the agent weighs each rule against its own plan.

        A domain rule ('website': add a favicon) applies only when the task is that
        kind of work; ``domains`` names the kinds detected for this task. At least
        half of ``limit`` goes to rules for every project when there are that many.
        A rule saved twice in near-identical words is shown once, and a rule for
        every project that is about another of the owner's projects stays out
        unless the task names that project (``rule_selection``).
        """
        from varyz.resolution.rule_selection import select

        current = self.current("confirmed")
        selection = select(
            current,
            text,
            project=project,
            domains={normalize_domain(domain) for domain in domains},
            limit=limit,
            known_projects={p.scope for p in current},
            normalize_scope=normalize_scope,
        )
        chosen = tuple(Applicable(rule, matched) for rule, matched in selection.chosen)
        return chosen, selection.withheld

    def checklist(
        self, domains: Collection[str], *, project: str | None = None, text: str = ""
    ) -> dict[str, tuple[Principle, ...]]:
        """Every in-scope rule for each kind of work: its definition of done.

        Unlike :meth:`applicable` nothing is ranked away: a checklist that drops
        the favicon because the task never said "favicon" is the failure it exists
        to prevent. A project's own item comes before the general one.
        """
        wanted = [normalize_domain(domain) for domain in domains]
        project_scope = normalize_scope(project) if project else None
        lowered = unicodedata.normalize("NFKC", text).casefold()
        grouped: dict[str, list[Principle]] = {domain: [] for domain in wanted if domain}
        # Oldest first, so a checklist reads in the order it was written down.
        for principle in reversed(self.current("confirmed")):
            if principle.domain not in grouped:
                continue
            if principle.scope != GLOBAL_SCOPE and principle.scope != project_scope and not (
                re.search(rf"(?<!\w){re.escape(principle.scope)}(?!\w)", lowered)
            ):
                continue
            grouped[principle.domain].append(principle)
        return {
            domain: tuple(sorted(items, key=lambda item: item.scope == GLOBAL_SCOPE))
            for domain, items in grouped.items()
            if items
        }

    def stats(self) -> dict[str, Any]:
        """Aggregate counts only: how many rules, and how often suggestions were kept.

        A candidate put on trial was not kept by the owner, so it is not counted
        as accepted; ``trials.TrialRules.status`` measures the trial lane."""
        confirmed = len(self.current("confirmed"))
        candidates = len(self.current("candidate"))
        accepted = self._conn.execute(
            """SELECT count(*) FROM owner_principles AS kept
               JOIN owner_principles AS suggested ON suggested.id = kept.supersedes_id
               WHERE kept.status = 'confirmed' AND suggested.status = 'candidate'
                 AND json_extract(kept.provenance_json, '$.trial') IS NULL"""
        ).fetchone()[0]
        rejected = self._conn.execute(
            """SELECT count(*) FROM owner_principle_retractions AS retraction
               JOIN owner_principles AS suggested ON suggested.id = retraction.principle_id
               WHERE suggested.status = 'candidate' AND retraction.reason NOT IN (?, ?)""",
            (REPLACED_READING, LIFTED),
        ).fetchone()[0]
        by_source: dict[str, dict[str, int]] = {}
        for status, source, count in self._conn.execute(
            f"""SELECT principle.status, principle.source, count(*)
                FROM owner_principles AS principle WHERE {_CURRENT}
                GROUP BY principle.status, principle.source"""
        ):
            family = str(source).split(":", 1)[0]
            bucket = by_source.setdefault(family, {"confirmed": 0, "candidate": 0})
            bucket[status] = bucket.get(status, 0) + int(count)
        reviewed = accepted + rejected
        return {
            "confirmed": confirmed,
            "candidates": candidates,
            "accepted": int(accepted),
            "rejected": int(rejected),
            "acceptance_rate": round(accepted / reviewed, 3) if reviewed else None,
            "by_source": by_source,
        }

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _validated(
        statement: str, kind: str, scope: str | None, rationale: str, source: str
    ) -> tuple[str, str, str, str, str]:
        statement = " ".join(statement.split())
        rationale = " ".join((rationale or "").split())
        source = source.strip()
        if not statement or not source:
            raise ValueError("a rule needs a statement and a source")
        if len(statement) > MAX_STATEMENT_CHARS:
            raise ValueError(f"a rule is at most {MAX_STATEMENT_CHARS} characters")
        if len(rationale) > MAX_RATIONALE_CHARS:
            raise ValueError(f"a rule's reason is at most {MAX_RATIONALE_CHARS} characters")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        normalized_scope = normalize_scope(scope)
        if kind == "vision" and normalized_scope == GLOBAL_SCOPE:
            raise ValueError("a vision belongs to one project; pass its name as scope")
        return statement, kind, normalized_scope, rationale, source

    def _insert(
        self,
        *,
        statement: str,
        kind: str,
        scope: str,
        rationale: str,
        status: str,
        source: str,
        provenance: Mapping[str, Any],
        caller: Caller,
        device_id: str,
        supersedes_id: str | None,
        domain: str = "",
    ) -> str:
        if supersedes_id is not None:
            self._current_row(supersedes_id)
        principle_id = new_id()
        seq = _next_monotonic_seq(self._conn, device_id)
        self._conn.execute(
            """INSERT INTO owner_principles
               (id, statement, normalized_statement, kind, scope, rationale, status,
                source, provenance_json, caller_agent_id, caller_display_name,
                caller_purpose, caller_capability_id, caller_device_id,
                caller_transport, device_id, monotonic_seq, supersedes_id, created_at,
                domain)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                principle_id, statement, normalize_statement(statement), kind, scope,
                rationale, status, source,
                json.dumps(dict(provenance), ensure_ascii=False, sort_keys=True),
                caller.agent_id, caller.display_name, caller.purpose,
                caller.capability_id, caller.device_id, caller.transport, device_id,
                seq, supersedes_id, _iso(self._clock()), domain,
            ),
        )
        return principle_id

    def _current_row(self, principle_id: str) -> Principle:
        row = self._conn.execute(
            f"""SELECT principle.* FROM owner_principles AS principle
                WHERE principle.id = ? AND {_CURRENT}""",
            (principle_id,),
        ).fetchone()
        if row is not None:
            return self._principle(row)
        if self.get(principle_id) is None:
            raise KeyError(f"unknown rule: {principle_id}")
        raise ValueError(f"rule {principle_id} was already superseded or withdrawn")

    @staticmethod
    def _principle(row: sqlite3.Row) -> Principle:
        return Principle(
            id=row["id"],
            statement=row["statement"],
            kind=row["kind"],
            scope=stored_scope(row["scope"]),
            rationale=row["rationale"],
            status=row["status"],
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
            domain=_row_domain(row),
        )


def _end_trial(
    trial: Principle, how: str, now: datetime, detail: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A trial rule's provenance once it holds for good: the trial kept as history."""
    provenance = {key: value for key, value in trial.provenance.items() if key != "trial"}
    provenance["trial_ended"] = {
        **dict(trial.provenance["trial"]), "how": how, "ended": _iso(now), **(detail or {}),
    }
    return provenance


def _row_domain(row: sqlite3.Row) -> str:
    try:
        return str(row["domain"] or "")
    except (IndexError, KeyError):  # read before migration 0037
        return ""


__all__ = [
    "GLOBAL_SCOPE",
    "KINDS",
    "Applicable",
    "Principle",
    "SQLitePrincipleStore",
    "near_statement",
    "normalize_domain",
    "normalize_scope",
    "normalize_statement",
]
