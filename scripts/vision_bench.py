"""Vision-conformance benchmark: does Varyz behave like the product in the pivot document?

Offline and deterministic. Seeds a temporary database from the synthetic sets in
``evals/varyz_vision_v1_*`` and drives the real resolver, knowledge store, escalation queue
and trust layer. It measures the loop the pivot rests on rather than retrieval alone:

1. tier-1 paraphrase matching: recall and, separately, wrong owner answers;
2. the learning loop: owner interruptions per decision across repeated agent questions;
3. escalation when the only matching evidence is withheld (the loop must not dead-end);
4. correction: a superseding answer replaces the old one on exact and paraphrased asks;
5. disclosure: denied or revoked callers must receive no owner knowledge;
6. latency of each resolution path.

``held_out`` scores the queries written before tuning; ``held_out_v2``
(``evals/varyz_vision_v2_heldout_queries.jsonl``) was frozen after the first burn and before
further matcher work, and ``held_out_v3`` after the second. Each is recorded once per dataset
hash in ``reports/vision_heldout_ledger.json`` and requires ``--authorize-held-out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from varyz.config import Config, StorageConfig
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.escalation import SQLiteEscalationQueue
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.resolver import EvidenceTier, ResolutionResolver
from varyz.trust import grant_agent_capability, register_agent, revoke_agent

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "evals" / "varyz_vision_v1_knowledge.jsonl"
QUERIES = ROOT / "evals" / "varyz_vision_v1_queries.jsonl"
# Frozen after the v1 held-out burn and before any further matcher work.
QUERIES_V2 = ROOT / "evals" / "varyz_vision_v2_heldout_queries.jsonl"
QUERIES_V3 = ROOT / "evals" / "varyz_vision_v3_heldout_queries.jsonl"
QUERIES_V4 = ROOT / "evals" / "varyz_vision_v4_heldout_queries.jsonl"
HELD_OUT_SPLITS = ("held_out", "held_out_v2", "held_out_v3", "held_out_v4")
HELDOUT_LEDGER = ROOT / "reports" / "vision_heldout_ledger.json"
OWNER = Caller(
    agent_id="owner:bench", display_name="Owner", purpose="owner_knowledge", device_id="bench"
)

# Sensitive-only evidence: the archive has a match but may not release it to an agent.
WITHHELD_CORPUS = (
    ("therapy-note", "Therapist session notes", "Talked with my therapist about the Quill "
     "relocation decision; leaning towards moving the Quill team to Lisbon next spring."),
    ("salary-mail", "Salary review", "Salary review for the Quill hiring plan: offer the "
     "senior engineer EUR 95k and hold the contractor budget flat."),
)
WITHHELD_QUESTIONS = (
    "Where is the Quill team relocating to?",
    "What salary should the Quill senior engineer offer be?",
)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _queries(split: str) -> Path:
    return {
        "held_out_v2": QUERIES_V2, "held_out_v3": QUERIES_V3, "held_out_v4": QUERIES_V4,
    }.get(split, QUERIES)


def dataset_hash(split: str = "dev") -> str:
    digest = hashlib.sha256()
    for path in (KNOWLEDGE, _queries(split)):
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Harness:
    """A fresh temporary archive with knowledge, evidence, escalation and trust."""

    def __init__(self, root: Path, verifier: Any = None) -> None:
        self.cfg = Config(storage=StorageConfig(data_dir=root / "data"), base_dir=root)
        self.conn = open_db(self.cfg.db_path)
        self.knowledge = SQLiteKnowledgeStore(self.conn, verifier=verifier)
        self.queue = SQLiteEscalationQueue(self.conn, self.knowledge, device_id="bench")
        self.resolver = ResolutionResolver(
            (self.knowledge, EvidenceTier(self.conn, self.cfg)), escalation_queue=self.queue
        )
        with self.conn:
            self.agent = register_agent(
                self.conn, agent_id="agent:bench", display_name="Bench agent",
                purpose="owner_context", device_id="bench",
            ).caller

    def seed_knowledge(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        ids = {}
        for row in rows:
            entry_id = self.knowledge.remember(
                question=row["question"], answer=row["answer"], caller=OWNER,
                basis=AnswerBasis.EXPLICIT, source="bench", device_id="bench",
            )
            ids[entry_id] = row["id"]
        return ids

    def seed_evidence(
        self, rows: tuple[tuple[str, str, str], ...], sensitivity: Sensitivity
    ) -> None:
        occurred = datetime(2026, 9, 1, tzinfo=UTC)
        with self.conn:
            for external_id, title, body in rows:
                entry = NormalizedEntry(
                    kind=Kind("note"), external_id=external_id, title=title, body=body,
                    occurred_start=occurred, captured_at=occurred, authored_by_user=True,
                )
                entry_id, _ = dao.upsert_entry(
                    self.conn, entry, source="documents", sensitivity=sensitivity
                )
                dao.replace_chunks(self.conn, entry_id, [f"{title}\n\n{body}"])

    def request(self, question: str, caller: Caller | None = None, *,
                escalate: bool = False) -> ResolutionRequest:
        caller = caller or self.agent
        return ResolutionRequest(
            question=question, caller=caller, purpose=caller.purpose, allow_escalation=escalate
        )

    def close(self) -> None:
        self.conn.close()


def _answer_to_id(result: Resolution | None, answers: dict[str, str]) -> str | None:
    if result is None or result.answer is None or result.tier != 1:
        return None
    return answers.get(result.answer, "?")


def bench_paraphrase(h: Harness, split: str) -> dict[str, Any]:
    knowledge = _rows(KNOWLEDGE)
    h.seed_knowledge(knowledge)
    answers = {row["answer"]: row["id"] for row in knowledge}
    cases = [row for row in _rows(_queries(split)) if row["split"] == split]
    outcomes = []
    for case in cases:
        got = _answer_to_id(h.knowledge.try_resolve(h.request(case["question"])), answers)
        expect = case["expect"]
        if expect is None:
            outcome = "correct_abstain" if got is None else "wrong_answer"
        elif got == expect:
            outcome = "correct_answer"
        else:
            outcome = "miss" if got is None else "wrong_answer"
        outcomes.append({"id": case["id"], "expect": expect, "got": got, "outcome": outcome})
    positives = [o for o in outcomes if o["expect"] is not None]
    negatives = [o for o in outcomes if o["expect"] is None]
    trusted = sum(o["outcome"] in {"correct_answer", "correct_abstain"} for o in outcomes)
    return {
        "cases": len(outcomes),
        "trusted": trusted,
        "trusted_rate": round(trusted / len(outcomes), 4),
        "paraphrase_recall": round(
            sum(o["outcome"] == "correct_answer" for o in positives) / len(positives), 4
        ),
        "wrong_answers": sum(o["outcome"] == "wrong_answer" for o in outcomes),
        "wrong_on_positive": sum(o["outcome"] == "wrong_answer" for o in positives),
        "false_match_on_negative": sum(o["outcome"] == "wrong_answer" for o in negatives),
        "negative_cases": len(negatives),
        "outcomes": outcomes,
    }


def bench_learning_loop(h: Harness, split: str) -> dict[str, Any]:
    """First ask escalates; the owner answers once; later paraphrases should not interrupt."""
    queries = [
        row for row in _rows(_queries(split)) if row["split"] == split and row["expect"]
    ]
    by_decision: dict[str, list[str]] = {}
    for row in queries:
        by_decision.setdefault(row["expect"], []).append(row["question"])
    knowledge = {row["id"]: row for row in _rows(KNOWLEDGE)}
    interruptions = 0
    first_escalated = 0
    reused = 0
    later_asks = 0
    for decision, asks in sorted(by_decision.items()):
        first = h.resolver.resolve(h.request(knowledge[decision]["question"], escalate=True))
        if first.escalation_id and first.basis == AnswerBasis.ESCALATED:
            first_escalated += 1
            interruptions += 1
            h.queue.answer(first.escalation_id, knowledge[decision]["answer"], device_id="bench")
        for ask in asks:
            later_asks += 1
            result = h.resolver.resolve(h.request(ask, escalate=True))
            if result.tier == 1 and result.answer == knowledge[decision]["answer"]:
                reused += 1
            elif result.basis == AnswerBasis.ESCALATED:
                interruptions += 1
    decisions = len(by_decision)
    return {
        "decisions": decisions,
        "first_ask_escalated": first_escalated,
        "later_asks": later_asks,
        "later_asks_answered_from_owner_answer": reused,
        "owner_interruptions": interruptions,
        "interruptions_per_decision": round(interruptions / decisions, 3),
        "ideal_interruptions_per_decision": 1.0,
        "pending_after": len(h.queue.pending()),
    }


def bench_withheld_escalation(h: Harness) -> dict[str, Any]:
    h.seed_evidence(WITHHELD_CORPUS, Sensitivity.SENSITIVE)
    rows = []
    for question in WITHHELD_QUESTIONS:
        quiet = h.resolver.resolve(h.request(question, escalate=False))
        loud = h.resolver.resolve(h.request(question, escalate=True))
        rows.append({
            "no_escalation": {"disposition": quiet.disposition.value,
                              "withheld": len(quiet.withheld), "leaked": quiet.answer is not None},
            "escalation_allowed": {"disposition": loud.disposition.value,
                                   "escalated": loud.escalation_id is not None,
                                   "withheld": len(loud.withheld),
                                   "leaked": loud.answer is not None},
        })
    return {
        "questions": len(rows),
        "leaks": sum(
            r["no_escalation"]["leaked"] or r["escalation_allowed"]["leaked"] for r in rows
        ),
        "escalated_when_allowed": sum(r["escalation_allowed"]["escalated"] for r in rows),
        "dead_ends": sum(not r["escalation_allowed"]["escalated"] for r in rows),
        "rows": rows,
    }


def bench_correction(h: Harness) -> dict[str, Any]:
    ids = h.seed_knowledge(_rows(KNOWLEDGE))
    old_id = next(key for key, value in ids.items() if value == "k02")
    new_answer = "SQLite for the alpha; Postgres on Supabase only after launch."
    h.knowledge.remember(
        question="Which database does Kestrel use?", answer=new_answer, caller=OWNER,
        basis=AnswerBasis.EXPLICIT, source="bench", device_id="bench", supersedes_id=old_id,
    )
    asks = ("Which database does Kestrel use?", "What database is Kestrel built on?",
            "What does Kestrel use for its database?")
    results = [h.knowledge.try_resolve(h.request(q)) for q in asks]
    current = sum(r is not None and r.answer == new_answer for r in results)
    stale = sum(r is not None and r.answer == "Postgres hosted on Supabase." for r in results)
    retract = getattr(h.knowledge, "retract", None)
    retracted_hidden = None
    if callable(retract):
        target = next(key for key, value in ids.items() if value == "k05")
        retract(target, caller=OWNER, device_id="bench")
        after = h.knowledge.try_resolve(h.request("Does the owner prefer morning meetings?"))
        retracted_hidden = after is None
    return {
        "asks": len(asks),
        "current_answer": current,
        "stale_answer": stale,
        "history_preserved": len(h.knowledge.history("Which database does Kestrel use?")) == 2,
        "retract_supported": callable(retract),
        "retracted_hidden": retracted_hidden,
    }


def bench_disclosure(h: Harness) -> dict[str, Any]:
    h.seed_knowledge(_rows(KNOWLEDGE))
    with h.conn:
        denied_id = "agent:bench-denied"
        denied = register_agent(
            h.conn, agent_id=denied_id, display_name="Denied", purpose="shopping",
            device_id="bench",
        ).caller
        zero = grant_agent_capability(h.conn, denied_id, purpose="shopping_zero",
                                      max_items=0, max_chars=0)
        denied = denied.model_copy(update={"capability_id": zero, "purpose": "shopping_zero"})
        revoked = register_agent(
            h.conn, agent_id="agent:bench-revoked", display_name="Revoked",
            purpose="owner_context", device_id="bench",
        ).caller
        revoke_agent(h.conn, revoked.agent_id, capability_id=revoked.capability_id)
    low_trust = Caller(agent_id="agent:unregistered", display_name="Unregistered",
                       purpose="owner_context", device_id="bench")
    questions = ("Which database does Kestrel use?", "Can agents share the owner's calendar "
                 "with other apps?", "What is the pricing for the Lumen app?")
    callers: dict[str, tuple[Caller, bool]] = {
        "registered": (h.agent, True),
        "low_trust_unregistered": (low_trust, True),
        "zero_capability": (denied, False),
        "revoked_capability": (revoked, False),
    }
    report: dict[str, Any] = {}
    unauthorized = 0
    should_withhold = 0
    for name, (caller, allowed) in callers.items():
        disclosed = 0
        for question in questions:
            result = h.resolver.resolve(h.request(question, caller))
            disclosed += result.answer is not None
        report[name] = {"asked": len(questions), "disclosed": disclosed, "allowed": allowed}
        if not allowed:
            should_withhold += len(questions)
            unauthorized += disclosed
    report["unauthorized_disclosures"] = unauthorized
    report["unauthorized_disclosure_rate"] = round(unauthorized / should_withhold, 4)
    return report


def _timed(fn: Callable[[], object], runs: int) -> dict[str, float]:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {"p50_ms": round(statistics.median(samples), 3),
            "p95_ms": round(samples[int(0.95 * (len(samples) - 1))], 3),
            "runs": runs}


def bench_latency(h: Harness, runs: int) -> dict[str, Any]:
    h.seed_knowledge(_rows(KNOWLEDGE))
    h.seed_evidence(WITHHELD_CORPUS, Sensitivity.SENSITIVE)
    h.seed_evidence((("adr-7", "ADR 7: sync journal", "Decided the importer writes an "
                      "append-only journal before advancing the sync cursor."),),
                    Sensitivity.PERSONAL)
    paths = {
        "tier1_exact": "Which database does Kestrel use?",
        "tier1_paraphrase": "What does Kestrel use for its database?",
        "tier2_evidence": "Why does the importer write a journal before the sync cursor?",
        "tier2_withheld": WITHHELD_QUESTIONS[0],
        "miss_not_known": "What is the owner's favourite restaurant?",
    }
    report = {}
    for name, question in paths.items():
        # Record where each path actually landed so a timing is never mislabelled.
        probe = h.resolver.resolve(h.request(question))
        report[name] = {
            **_timed(lambda q=question: h.resolver.resolve(h.request(q)), runs),
            "tier": probe.tier,
            "disposition": probe.disposition.value,
            "withheld": len(probe.withheld),
        }
    return report


def run(split: str, runs: int, verifier: Any = None) -> dict[str, Any]:
    sections: dict[str, Callable[[Harness], dict[str, Any]]] = {
        "paraphrase": lambda h: bench_paraphrase(h, split),
        "learning_loop": lambda h: bench_learning_loop(h, split),
        "withheld_escalation": bench_withheld_escalation,
        "correction": bench_correction,
        "disclosure": bench_disclosure,
        "latency": lambda h: bench_latency(h, runs),
    }
    results: dict[str, Any] = {
        "suite": "varyz_vision_v1", "split": split, "dataset_sha256": dataset_hash(split),
        "paraphrase_verifier": verifier is not None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    for name, section in sections.items():
        with tempfile.TemporaryDirectory(prefix=f"varyz-vision-{name}-") as directory:
            harness = Harness(Path(directory), verifier)
            try:
                results[name] = section(harness)
            finally:
                harness.close()
    return results


def _burn_held_out(label: str, split: str) -> None:
    ledger = (
        json.loads(HELDOUT_LEDGER.read_text(encoding="utf-8"))
        if HELDOUT_LEDGER.exists()
        else []
    )
    digest = dataset_hash(split)
    if any(item["dataset_sha256"] == digest for item in ledger):
        raise SystemExit(f"held-out already scored for dataset {digest[:12]}; refusing")
    ledger.append({"dataset_sha256": digest, "label": label,
                   "scored_at": datetime.now(UTC).isoformat(timespec="seconds")})
    HELDOUT_LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", *HELD_OUT_SPLITS), default="dev")
    parser.add_argument("--authorize-held-out", metavar="LABEL")
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--verifier", action="store_true",
        help="add the local-model paraphrase verifier (needs Ollama on loopback)",
    )
    args = parser.parse_args()
    if args.split in HELD_OUT_SPLITS:
        if not args.authorize_held_out:
            parser.error(f"{args.split} requires --authorize-held-out LABEL")
        _burn_held_out(args.authorize_held_out, args.split)
    verifier = None
    if args.verifier:
        from varyz.config import load_config
        from varyz.resolution.local_classifier import embedder_from_config, local_model_from_config
        from varyz.resolution.paraphrase_verify import ParaphraseVerifier

        cfg = load_config(None)
        verifier = ParaphraseVerifier(local_model_from_config(cfg), embedder_from_config(cfg))
    payload = run(args.split, args.runs, verifier)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {k: {kk: vv for kk, vv in v.items() if not isinstance(vv, (list, dict))}
               for k, v in payload.items() if isinstance(v, dict) and k != "latency"}
    summary["latency"] = payload["latency"]
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
