"""Whether the owner's rules held in real work, and what each answer became.

Only unambiguous signals are kept, all local and append-only:

- finish_check results per rule and task (``rule_checks``). A row is written only
  when a rule's result in that task changes, so an agent calling finish_check
  five times adds one row, and a failed check that the agent then fixed shows as
  ``failed`` at attempt 1 and ``passed`` at attempt 2.
- the owner correcting a rule during a task: status ``corrected`` for the rule
  that was replaced.
- answers an agent was given in a task that then finished ready to report, with
  no later owner answer replacing them: outcome ``adopted``. A later supersession
  still appends ``overridden`` or ``corrected``; the newest outcome wins.
- the agent output the owner corrected (``correction_turns``), which never leaves
  this computer.
- the agent and model behind each check, so rules holding can be compared by model;
  a ``corrected`` row right after a ``passed`` one is a false "done".
- the memory test, on by default (``[signals] memory_test``): a small random share of
  low-stakes rules is held back from a task's briefing and revealed at its check,
  with each draw kept in ``rule_exposures``. Rules reached outside a conversation
  (advise without one) are never held back, which only understates the effect.
- whether a question needed the owner (``ask_feedback``, or derived from how it
  closed), and a rule the owner says the agent should have asked about.
- before a question reaches the owner, the agent's guess and the owner's nearest
  earlier answer (``answer_predictions``), scored against what they then said.
- how preferences move: the chain of rules each one replaced, and how long each lasted.

Recording is best effort: a signal that cannot be written never fails the
agent's task.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from varyz.contribution_schema import MODEL as MODEL_PATTERN
from varyz.domain import new_id, utc_now
from varyz.privacy.secrets_scan import redact

CHECK_STATUSES = frozenset({"passed", "failed", "not_applicable", "unchecked", "corrected"})
MAX_TURN = 4000
MAX_EVIDENCE = 400
ADOPTED_SOURCE = "task_finished"
# Model families agents report, followed by a version: claude-opus-5-5, gpt-6.1-sol.
MODEL = re.compile(MODEL_PATTERN)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection) -> Any:
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if owned:
            conn.rollback()
        raise
    else:
        if owned:
            conn.commit()


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def _rules(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    wanted = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
    if not wanted:
        return set()
    marks = ",".join("?" * len(wanted))
    return {
        row[0] for row in conn.execute(
            f"SELECT id FROM owner_principles WHERE kind='rule' AND id IN ({marks})", wanted
        )
    }


def model_name(value: Any) -> str:
    """A model id an agent reported, lowercased, or '' when it is not a known
    model family. Free text never passes: it could carry a name or a path."""
    text = str(value or "").strip().lower()
    return text if len(text) <= 64 and MODEL.fullmatch(text) else ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _append_check(
    conn: sqlite3.Connection,
    principle_id: str,
    conversation_id: str,
    *,
    status: str,
    method: str,
    verified: bool,
    evidence: str,
    project: str | None,
    now: str,
    agent: str = "",
    model: str = "",
) -> str | None:
    last = conn.execute(
        "SELECT status,attempt FROM rule_checks WHERE principle_id=? AND conversation_id=? "
        "ORDER BY checked_at DESC,rowid DESC LIMIT 1",
        (principle_id, conversation_id),
    ).fetchone()
    if last is not None and last[0] == status:
        return None  # The same result again is not a new signal.
    check_id = new_id()
    values: dict[str, Any] = {
        "id": check_id, "principle_id": principle_id, "conversation_id": conversation_id,
        "project": project, "status": status, "method": method,
        "independently_verified": int(verified),
        "attempt": (last[1] + 1) if last is not None else 1,
        "evidence": redact(evidence)[0][:MAX_EVIDENCE], "checked_at": now,
    }
    if "model" in _columns(conn, "rule_checks"):
        values.update(agent=agent[:120], model=model_name(model))
    conn.execute(
        f"INSERT INTO rule_checks ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
        tuple(values.values()),
    )
    return check_id


def record_checks(
    conn: sqlite3.Connection,
    conversation_id: str | None,
    receipts: Iterable[dict[str, Any]],
    *,
    project: str | None = None,
    agent: str = "",
    model: str = "",
    memory_test: float = 0.0,
    now: datetime | None = None,
) -> list[str]:
    """Keep finish_check's per-rule receipts. Needs the task's conversation id.

    ``memory_test`` is the owner's holdout rate when the memory test is on: each
    checked rule's arm in this task is kept beside its result."""
    if not conversation_id or not _has(conn, "rule_checks"):
        return []
    receipts = [r for r in receipts if r.get("status") in CHECK_STATUSES - {"corrected"}]
    stamp = _iso(now or utc_now())
    written: list[str] = []
    with _transaction(conn):
        known = _rules(conn, (str(r.get("rule_id") or "") for r in receipts))
        for receipt in receipts:
            if receipt.get("rule_id") not in known:
                continue  # Definition-of-done items that are not rules, or unknown ids.
            check_id = _append_check(
                conn, receipt["rule_id"], conversation_id,
                status=receipt["status"], method=str(receipt.get("method") or "none"),
                verified=bool(receipt.get("independently_verified")),
                evidence=str(receipt.get("evidence") or ""), project=project, now=stamp,
                agent=agent, model=model,
            )
            if check_id:
                written.append(check_id)
        if memory_test and _has(conn, "rule_exposures"):
            _record_arms(conn, conversation_id, known, memory_test, stamp)
    return written


# -- the memory test -----------------------------------------------------------

# Rules about these are never held back: an agent that does not see them could
# cost the owner something real.
_HIGH_STAKES = re.compile(
    r"\b(?:ask|confirm|approv\w*|permission|delet\w*|remov\w*|destroy\w*|overwrit\w*|"
    r"secret\w*|password\w*|token\w*|credential\w*|keys?|private|privacy|personal|"
    r"push\w*|publish\w*|deploy\w*|releas\w*|ship\w*|merg\w*|commit\w*|production|prod|"
    r"pay\w*|spend\w*|money|billing|cost\w*|legal|licen[cs]\w*|secur\w*|send\w*|"
    r"email\w*|post\w*|upload\w*|force|backup\w*|migrat\w*|database|data)\b",
    re.IGNORECASE,
)


def low_stakes(statement: str) -> bool:
    return not _HIGH_STAKES.search(statement or "")


def arm(principle_id: str, conversation_id: str, rate: float) -> str:
    """The rule's arm in this task: the same answer for every briefing in it."""
    digest = hashlib.sha256(f"{principle_id}\0{conversation_id}".encode()).digest()
    return "held_out" if int.from_bytes(digest[:8], "big") / 2**64 < rate else "shown"


def memory_test_rate(cfg: Any) -> float:
    signals = getattr(cfg, "signals", None)
    return float(signals.memory_test_rate) if signals and signals.memory_test else 0.0


def held_back(principles: Iterable[Any], conversation_id: str | None, cfg: Any) -> set[str]:
    """Rules this task's briefing leaves out for the memory test. Empty when the
    owner turned it off or the task has no id."""
    rate = memory_test_rate(cfg)
    if not rate or not conversation_id:
        return set()
    return {
        p.id for p in principles
        if getattr(p, "kind", "rule") == "rule" and low_stakes(p.statement)
        and arm(p.id, conversation_id, rate) == "held_out"
    }


def _record_arms(conn: sqlite3.Connection, conversation_id: str, rule_ids: Iterable[str],
                 rate: float, stamp: str) -> None:
    ids = list(rule_ids)
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    for principle_id, statement in conn.execute(
        f"SELECT id,statement FROM owner_principles WHERE id IN ({marks})", ids
    ).fetchall():
        if low_stakes(statement):
            conn.execute(
                "INSERT OR IGNORE INTO rule_exposures VALUES (?,?,?,?,?)",
                (principle_id, conversation_id, arm(principle_id, conversation_id, rate), rate,
                 stamp),
            )


# -- asking: whether it was needed, and how well the answer was predicted ------

ASK_LABELS = frozenset({"needed", "unneeded", "should_have_asked"})
# Owner replies that hand the choice back: the question did not need them.
_HANDED_BACK = re.compile(
    r"^\W*(?:(?:i\s+)?(?:don'?t|do\s+not)\s+(?:care|mind)|no\s+preference|"
    r"(?:it\s+)?(?:doesn'?t|does\s+not)\s+matter|(?:you|u)\s+(?:decide|choose|pick)|"
    r"your\s+call|up\s+to\s+you|whatever(?:\s+you\s+(?:think|want|prefer))?|either(?:\s+is|"
    r"\s+one\s+is|\s+way)?(?:\s+(?:fine|ok|okay))?|use\s+your\s+(?:own\s+)?judge?ment|"
    r"(?:you\s+)?(?:didn'?t|did\s+not|don'?t)\s+need\s+to\s+ask|no\s+need\s+to\s+ask|"
    r"any(?:\s+is|\s+one)?(?:\s+(?:fine|ok|okay))?)\W*$",
    re.IGNORECASE,
)


def ask_label(status: str, answer: str | None, feedback: str | None = None) -> str:
    """Whether a question needed the owner: their explicit word first, else what
    their reply shows. '' for a question still open."""
    if feedback in ASK_LABELS:
        return feedback
    if status == "answered":
        return "unneeded" if _HANDED_BACK.match(answer or "") else "needed"
    return "dropped" if status == "expired" else ""


def record_ask_feedback(
    conn: sqlite3.Connection,
    label: str,
    *,
    escalation_id: str | None = None,
    principle_id: str | None = None,
    conversation_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """The owner said a question was (not) needed, or the agent should have asked."""
    if label not in ASK_LABELS or not (escalation_id or principle_id):
        return None
    if not _has(conn, "ask_feedback"):
        return None
    with _transaction(conn):
        if escalation_id and not conn.execute(
            "SELECT 1 FROM resolution_escalations WHERE id=?", (escalation_id,)
        ).fetchone():
            return None
        if principle_id and not _rules(conn, [principle_id]):
            return None
        feedback_id = new_id()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO ask_feedback VALUES (?,?,?,?,?,?)",
            (feedback_id, escalation_id, principle_id, conversation_id, label,
             _iso(now or utc_now())),
        )
        return feedback_id if cursor.rowcount == 1 else None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _FILLER}


_FILLER = frozenset({
    "a", "an", "the", "to", "of", "for", "in", "on", "and", "or", "is", "it", "be", "we",
    "i", "you", "use", "with",
})


def answers_match(guess: str, answer: str) -> bool:
    """Whether a guess says what the owner said. Strict on purpose: a yes against a
    no, or a different named choice, is a miss."""
    guess_words, answer_words = _words(guess), _words(answer)
    if not guess_words or not answer_words:
        return False
    for polar in ({"yes", "no"}, {"keep", "drop"}, {"on", "off"}):
        said, guessed = answer_words & polar, guess_words & polar
        if said and guessed and said != guessed:
            return False
    small, large = sorted((guess_words, answer_words), key=len)
    return len(small & large) / len(small) >= 0.6


def guess_hit(guess: str | None, answer: str | None) -> str:
    """'hit' or 'miss' for a guess at an owner's answer; '' when there is nothing to
    score, including an owner who handed the choice back."""
    if not guess or not answer or _HANDED_BACK.match(answer):
        return ""
    return "hit" if answers_match(guess, answer) else "miss"


def _nearest_answer(conn: sqlite3.Connection, question: str,
                    exclude: str) -> tuple[str, str | None]:
    """What the owner answered to the most similar earlier question, if any is close."""
    wanted = _words(question)
    if len(wanted) < 2:
        return "", None
    best: tuple[float, str, str | None] = (0.0, "", None)
    for escalation_id, earlier, answer in conn.execute(
        "SELECT e.id,e.question,k.answer FROM resolution_escalations e "
        "JOIN owner_knowledge k ON k.id=e.answer_knowledge_id "
        "WHERE e.status='answered' AND e.id<>? ORDER BY e.answered_at DESC LIMIT 500",
        (exclude,),
    ):
        words = _words(earlier)
        overlap = len(wanted & words) / len(wanted | words) if words else 0.0
        if overlap > best[0]:
            best = (overlap, answer or "", escalation_id)
    return (best[1], best[2]) if best[0] >= 0.5 else ("", None)


def record_prediction(
    conn: sqlite3.Connection,
    escalation_id: str,
    *,
    decision_kind: str = "",
    agent: str = "",
    model: str = "",
    agent_guess: str = "",
    now: datetime | None = None,
) -> bool:
    """Before the owner sees a question: the asking agent's guess at the answer and
    the nearest one they gave before. First record wins."""
    if not _has(conn, "answer_predictions"):
        return False
    with _transaction(conn):
        row = conn.execute(
            "SELECT question,status FROM resolution_escalations WHERE id=?", (escalation_id,)
        ).fetchone()
        if row is None or row[1] != "pending":
            return False  # Only a guess made before the answer counts.
        memory_guess, source = _nearest_answer(conn, row[0], escalation_id)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO answer_predictions VALUES (?,?,?,?,?,?,?,?)",
            (escalation_id, decision_kind[:32], agent[:120], model_name(model),
             redact(agent_guess.strip())[0][:MAX_EVIDENCE], memory_guess[:MAX_EVIDENCE], source,
             _iso(now or utc_now())),
        )
        return cursor.rowcount == 1


# -- how preferences change ------------------------------------------------------

def lineage(conn: sqlite3.Connection, principle_id: str) -> dict[str, Any]:
    """How many rules came before this one, and how long the one it replaced lasted."""
    rows: list[tuple[str, str | None, str]] = []
    current: str | None = principle_id
    seen: set[str] = set()
    while current and current not in seen and len(rows) < 50:
        seen.add(current)
        row = conn.execute(
            "SELECT id,supersedes_id,created_at FROM owner_principles WHERE id=?", (current,)
        ).fetchone()
        if row is None:
            break
        rows.append((row[0], row[1], row[2]))
        current = row[1]
    lasted = 0
    if len(rows) >= 2:
        with contextlib.suppress(ValueError):
            lasted = max(0, (datetime.fromisoformat(rows[0][2])
                             - datetime.fromisoformat(rows[1][2])).days)
    return {"chain_depth": max(0, len(rows) - 1), "replaced_after_days": lasted}


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 3) if total else None


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the signals show on this computer, for the owner and for tuning Varyz."""
    out: dict[str, Any] = {}
    if _has(conn, "rule_checks"):
        rows = conn.execute(
            "SELECT principle_id,conversation_id,status,attempt,"
            + ("agent,model " if "model" in _columns(conn, "rule_checks") else "'' ,'' ")
            + "FROM rule_checks ORDER BY principle_id,conversation_id,attempt"
        ).fetchall()
        first: dict[tuple[str, str], tuple[str, str, str]] = {}
        claims = corrected = false_done = 0
        previous: dict[tuple[str, str], str] = {}
        pairs: set[tuple[str, str]] = set()
        for principle_id, conversation_id, status, _attempt, agent, model in rows:
            key = (principle_id, conversation_id)
            pairs.add(key)
            if status in {"passed", "failed"}:
                # The first result the agent actually reported, not an empty first call.
                first.setdefault(key, (status, agent, model))
            if status == "passed":
                claims += 1
            if status == "corrected":
                corrected += 1
                false_done += previous.get(key) == "passed"
            previous[key] = status
        by_model: dict[str, dict[str, int]] = {}
        for status, agent, model in first.values():
            tally = by_model.setdefault(model or agent or "unknown", {"passed": 0, "failed": 0})
            tally[status] += 1
        out["checks"] = {
            "rule_task_pairs": len(pairs),
            "passed_claims": claims,
            "owner_corrections": corrected,
            "false_done_claims": false_done,
            "first_try_by_model": {
                name: {**t, "pass_rate": _rate(t["passed"], t["passed"] + t["failed"])}
                for name, t in sorted(by_model.items())
            },
        }
        if _has(conn, "rule_exposures"):
            arms: dict[str, dict[str, int]] = {}
            for arm_name, status in conn.execute(
                "SELECT x.arm,(SELECT c.status FROM rule_checks c WHERE c.principle_id="
                "x.principle_id AND c.conversation_id=x.conversation_id AND c.status IN "
                "('passed','failed') ORDER BY c.attempt LIMIT 1) FROM rule_exposures x"
            ):
                tally = arms.setdefault(arm_name, {"passed": 0, "failed": 0})
                if status in tally:
                    tally[status] += 1
            out["memory_test"] = {
                name: {**t, "pass_rate": _rate(t["passed"], t["passed"] + t["failed"])}
                for name, t in sorted(arms.items())
            }
    if _has(conn, "resolution_escalations"):
        feedback = (dict(conn.execute(
            "SELECT escalation_id,label FROM ask_feedback WHERE escalation_id IS NOT NULL"
        ).fetchall()) if _has(conn, "ask_feedback") else {})
        labels: dict[str, int] = {}
        answers: dict[str, str] = {}
        for escalation_id, status, answer in conn.execute(
            "SELECT e.id,e.status,k.answer FROM resolution_escalations e "
            "LEFT JOIN owner_knowledge k ON k.id=e.answer_knowledge_id"
        ):
            label = ask_label(status, answer, feedback.get(escalation_id))
            if label:
                labels[label] = labels.get(label, 0) + 1
            if status == "answered":
                answers[escalation_id] = answer or ""
        if _has(conn, "ask_feedback"):
            labels["should_have_asked"] = labels.get("should_have_asked", 0) + conn.execute(
                "SELECT COUNT(*) FROM ask_feedback WHERE escalation_id IS NULL "
                "AND label='should_have_asked'").fetchone()[0]
        out["asks"] = labels
        if _has(conn, "answer_predictions"):
            agent = [0, 0]
            memory = [0, 0]
            for escalation_id, agent_guess, memory_guess in conn.execute(
                "SELECT escalation_id,agent_guess,memory_guess FROM answer_predictions"
            ):
                if escalation_id not in answers:
                    continue
                for hits, guess in ((agent, agent_guess), (memory, memory_guess)):
                    scored = guess_hit(guess, answers[escalation_id])
                    if scored:
                        hits[0] += scored == "hit"
                        hits[1] += 1
            out["predictions"] = {
                "agent": {"scored": agent[1], "hit_rate": _rate(*agent)},
                "memory": {"scored": memory[1], "hit_rate": _rate(*memory)},
            }
    if _has(conn, "owner_principles"):
        lasted = sorted(
            lineage(conn, row[0])["replaced_after_days"] for row in conn.execute(
                "SELECT id FROM owner_principles WHERE kind='rule' AND supersedes_id IS NOT NULL")
        )
        out["preference_change"] = {
            "revisions": len(lasted),
            "median_days_a_rule_lasted": lasted[len(lasted) // 2] if lasted else None,
        }
    return out


def record_correction(
    conn: sqlite3.Connection,
    corrected_id: str,
    conversation_id: str | None,
    *,
    project: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """The owner corrected this rule during a task: it did not hold there."""
    if not conversation_id or not _has(conn, "rule_checks"):
        return None
    with _transaction(conn):
        if not _rules(conn, [corrected_id]):
            return None
        # The correction is against the agent and model that did the work in this task.
        worker = conn.execute(
            "SELECT agent,model FROM rule_checks WHERE principle_id=? AND conversation_id=? "
            "ORDER BY checked_at DESC,rowid DESC LIMIT 1", (corrected_id, conversation_id),
        ).fetchone() if "model" in _columns(conn, "rule_checks") else None
        return _append_check(
            conn, corrected_id, conversation_id, status="corrected", method="owner_correction",
            verified=True, evidence="", project=project, now=_iso(now or utc_now()),
            agent=worker[0] if worker else "", model=worker[1] if worker else "",
        )


def adopt_finished(
    conn: sqlite3.Connection,
    conversation_id: str | None,
    *,
    device_id: str,
    now: datetime | None = None,
) -> list[str]:
    """Answers given in a task that finished, with no outcome yet, were adopted."""
    if not conversation_id or not _has(conn, "resolution_outcomes"):
        return []
    from varyz.resolution.outcomes import append_outcome

    moment = now or utc_now()
    with _transaction(conn):
        ledger_ids = [row[0] for row in conn.execute(
            "SELECT l.id FROM resolution_ledger l WHERE l.conversation_id=? "
            "AND l.disposition='answered' AND l.answer IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM resolution_outcomes o WHERE o.ledger_id=l.id) "
            "ORDER BY l.resolved_at",
            (conversation_id,),
        )]
        return [
            append_outcome(conn, ledger_id=ledger_id, outcome="adopted", source=ADOPTED_SOURCE,
                           device_id=device_id, recorded_at=moment)
            for ledger_id in ledger_ids
        ]


def record_turn(
    conn: sqlite3.Connection,
    principle_id: str,
    *,
    method: str,
    agent_turn: str = "",
    owner_turn: str = "",
    thread: str = "",
    now: datetime | None = None,
) -> bool:
    """Keep the agent output a correction was about. First capture wins."""
    if not _has(conn, "correction_turns"):
        return False
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO correction_turns "
            "(principle_id,method,thread,agent_turn,owner_turn,captured_at) "
            "VALUES (?,?,?,?,?,?)",
            (principle_id, method, thread[:200], redact(agent_turn.strip())[0][:MAX_TURN],
             redact(owner_turn.strip())[0][:MAX_TURN], _iso(now or utc_now())),
        )
        return cursor.rowcount == 1


def held(conn: sqlite3.Connection, principle_id: str) -> dict[str, int]:
    """Per task, the final result for one rule: how often it held, failed or was corrected."""
    rows = conn.execute(
        "SELECT status FROM rule_checks c WHERE principle_id=? AND rowid=("
        "SELECT rowid FROM rule_checks WHERE principle_id=c.principle_id "
        "AND conversation_id=c.conversation_id ORDER BY checked_at DESC,rowid DESC LIMIT 1)",
        (principle_id,),
    ).fetchall()
    tally: dict[str, int] = {}
    for (status,) in rows:
        tally[status] = tally.get(status, 0) + 1
    return tally


def safely(operation: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a recorder without ever failing the caller's task."""
    try:
        return operation(*args, **kwargs)
    except Exception:
        return None


def _pending_turns(conn: sqlite3.Connection, limit: int) -> dict[str, tuple[str, str]]:
    """Mined rules with no captured turn yet: rule id -> the owner message's source id
    and its vault file."""
    wanted: dict[str, tuple[str, str]] = {}
    rows = conn.execute(
        "SELECT p.id, p.provenance_json FROM owner_principles p WHERE p.kind='rule' "
        "AND p.provenance_json LIKE '%\"entry_id\"%' AND NOT EXISTS "
        "(SELECT 1 FROM correction_turns t WHERE t.principle_id=p.id) "
        "ORDER BY p.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for principle_id, provenance in rows:
        try:
            corrections = json.loads(provenance or "{}").get("corrections") or []
        except ValueError:
            corrections = []
        entry_id = next((c.get("entry_id") for c in corrections
                         if isinstance(c, dict) and c.get("entry_id")), None)
        found = entry_id and conn.execute(
            "SELECT external_id,vault_ref FROM entries WHERE id=? AND source='ai_conversations'",
            (entry_id,),
        ).fetchone()
        wanted[principle_id] = (found[0] or "", found[1] or "") if found else ("", "")
    return wanted


def _vault_turn(cfg: Any, vault_ref: str) -> tuple[str, str]:
    """The agent message the archive kept beside the owner's (``after``), for chats
    whose transcript file is gone."""
    if not vault_ref:
        return "", ""
    try:
        root = (cfg.data_dir / "vault").resolve()
        path = (root / vault_ref).resolve()
        if not path.is_relative_to(root):
            return "", ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("after") or ""), str(data.get("text") or "")


def capture_turns(
    conn: sqlite3.Connection, cfg: Any, lock: Any, *, limit: int = 20
) -> dict[str, int]:
    """Find, in the approved local transcripts, what the agent said before each mined
    correction. Files are read outside the lock; a turn that cannot be found is
    marked so it is not searched for again."""
    from varyz.session_miner import _paths, _turns

    with lock:
        wanted = _pending_turns(conn, limit)
    if not wanted:
        return {"kept": 0, "not_found": 0}
    # Several rules can come from one owner message.
    by_reference: dict[str, list[str]] = {}
    for principle_id, (reference, _vault) in wanted.items():
        if reference:
            by_reference.setdefault(reference, []).append(principle_id)
    sessions: dict[str, set[str]] = {}
    for reference in by_reference:
        adapter_id, _, rest = reference.partition(":")
        sessions.setdefault(adapter_id, set()).add(rest.partition(":")[0])
    found: dict[str, tuple[str, str, str]] = {}
    for adapter_id, wanted_sessions in sessions.items():
        try:
            paths = [(a, p) for a, p in _paths(cfg, adapter_id)
                     if any(p.stem.endswith(s) for s in wanted_sessions)]
        except ValueError:
            continue  # AI conversation history is not approved: nothing to read.
        for adapter, path in paths:
            stats: dict[str, Any] = {"oversized_lines": 0, "malformed_lines": 0}
            # The agent's whole reply since the owner last spoke: some clients
            # split one reply into many short messages.
            reply: list[str] = []
            try:
                for source in _turns(adapter, path, stats):
                    if source.turn.role == "assistant":
                        reply.append(source.turn.text)
                        continue
                    for principle_id in by_reference.get(source.reference, []):
                        found[principle_id] = (f"ai:{adapter_id}:{source.turn.session}",
                                               "\n\n".join(reply), source.turn.text)
                    reply = []
            except OSError:
                continue
    kept = missing = 0
    with lock:
        for principle_id, (reference, vault_ref) in wanted.items():
            thread, agent, owner = found.get(principle_id, ("", "", ""))
            method = "transcript"
            if not agent.strip():
                agent, owner = _vault_turn(cfg, vault_ref)
                method = "archive"
                parts = reference.split(":")
                thread = f"ai:{parts[0]}:{parts[1]}" if len(parts) > 2 else ""
            if agent.strip():
                kept += record_turn(conn, principle_id, method=method, agent_turn=agent[-MAX_TURN:],
                                    owner_turn=owner, thread=thread)
            else:
                missing += record_turn(conn, principle_id, method="not_found", thread=thread)
    return {"kept": kept, "not_found": missing}


_capture_lock = threading.Lock()
_last_capture = 0.0


def tick(conn: sqlite3.Connection, cfg: Any, lock: Any) -> None:
    """From the daemon's loop: a bounded capture every ten minutes, on its own thread."""
    global _last_capture
    if time.monotonic() - _last_capture < 600 or not _capture_lock.acquire(blocking=False):
        return
    _last_capture = time.monotonic()

    def work() -> None:
        try:
            capture_turns(conn, cfg, lock)
        except Exception:
            pass  # Best effort: the next tick tries again.
        finally:
            _capture_lock.release()

    threading.Thread(target=work, name="varyz-correction-turns", daemon=True).start()
