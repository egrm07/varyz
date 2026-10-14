"""Evaluate one-shot Varyz recall and resolution on a frozen synthetic archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from varyz.config import Config, GraphConfig, StorageConfig
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import ResolutionRequest
from varyz.resolution.resolver import EvidenceTier, ResolutionResolver
from varyz.retrieval.answer import recall
from varyz.trust import register_agent

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "varys_capability_v3_cases.jsonl"
CORPUS = ROOT / "evals" / "varys_capability_v3_corpus.jsonl"


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _terms_present(text: str, groups: list[list[str]]) -> bool:
    lowered = text.casefold()
    return all(any(term.casefold() in lowered for term in group) for group in groups)


def _score_recall(case: dict[str, Any], answer: Any) -> bool:
    disposition = case.get("expected_disposition")
    refs = {item.external_id for item in answer.capsules}
    if disposition == "withheld":
        return answer.withheld_sensitive > 0 and not answer.capsules
    if disposition == "unknown":
        return not answer.capsules and answer.abstained
    text = "\n".join(item.text for item in answer.capsules)
    if (
        case.get("forbidden_instruction")
        and case["forbidden_instruction"].casefold() in text.casefold()
    ):
        return False
    return set(case.get("required_refs", ())) <= refs and _terms_present(
        text, case.get("expected_terms", [])
    )


def _score_resolution(case: dict[str, Any], answer: Any) -> bool:
    disposition = case.get("expected_disposition")
    if disposition == "withheld":
        return answer.disposition.value == "not_known" and bool(answer.withheld)
    if disposition == "unknown":
        return answer.disposition.value == "not_known" and not answer.answer
    if answer.disposition.value != "answered" or not answer.answer:
        return False
    text = answer.answer
    if (
        case.get("forbidden_instruction")
        and case["forbidden_instruction"].casefold() in text.casefold()
    ):
        return False
    refs = {
        item.provenance.record_id
        for item in (answer.life_result.answers if answer.life_result else ())
        if item.provenance.record_id
    }
    return set(case.get("required_refs", ())) <= refs and _terms_present(
        text, case.get("expected_terms", [])
    )


def _clean_refs(case: dict[str, Any], refs: set[str]) -> bool:
    if case.get("expected_disposition") in {"withheld", "unknown"}:
        return not refs
    return refs == set(case.get("required_refs", ()))


def evaluate(split: str) -> dict[str, Any]:
    cases = [item for item in _rows(CASES) if item["split"] == split]
    with tempfile.TemporaryDirectory(prefix="varyz-capability-v3-") as temp:
        root = Path(temp)
        cfg = Config(
            storage=StorageConfig(data_dir=root / "data"),
            graph=GraphConfig(excluded_sources=["whatsapp", "telegram"]),
            base_dir=root,
        )
        conn = open_db(cfg.db_path)
        try:
            with conn:
                for row in _rows(CORPUS):
                    occurred = datetime.fromisoformat(row["occurred"].replace("Z", "+00:00"))
                    normalized = NormalizedEntry(
                        kind=Kind(row["kind"]),
                        external_id=row["external_id"],
                        title=row["title"],
                        body=row["body"],
                        occurred_start=occurred,
                        captured_at=occurred,
                        authored_by_user=row["source"] == "ai_conversations",
                    )
                    entry_id, _ = dao.upsert_entry(
                        conn,
                        normalized,
                        source=row["source"],
                        sensitivity=Sensitivity(row.get("sensitivity", "personal")),
                    )
                    dao.replace_chunks(conn, entry_id, [f"{row['title']}\n\n{row['body']}"])
            caller = register_agent(
                conn,
                agent_id="agent:capability-v3",
                display_name="Capability benchmark",
                purpose="owner_context",
                device_id="fixture",
            ).caller
            resolver = ResolutionResolver((SQLiteKnowledgeStore(conn), EvidenceTier(conn, cfg)))
            results: list[dict[str, Any]] = []
            for case in cases:
                started = time.perf_counter()
                evidence = recall(
                    conn,
                    cfg,
                    case["question"],
                    embedder=None,
                    limit=8,
                    excluded_sources=tuple(cfg.graph.excluded_sources),
                )
                recall_ms = round((time.perf_counter() - started) * 1000, 2)
                started = time.perf_counter()
                resolution = resolver.resolve(
                    ResolutionRequest(
                        question=case["question"],
                        caller=caller,
                        purpose=caller.purpose,
                        allow_escalation=False,
                    )
                )
                resolve_ms = round((time.perf_counter() - started) * 1000, 2)
                recall_refs = {item.external_id for item in evidence.capsules}
                resolve_refs = {
                    item.provenance.record_id
                    for item in (resolution.life_result.answers if resolution.life_result else ())
                    if item.provenance.record_id
                }
                recall_pass = _score_recall(case, evidence)
                resolve_pass = _score_resolution(case, resolution)
                results.append(
                    {
                        "id": case["id"],
                        "axis": case["axis"],
                        "expected_disposition": case.get("expected_disposition", "answer"),
                        "no_context_pass": case.get("expected_disposition")
                        in {"unknown", "withheld"},
                        "recall_pass": recall_pass,
                        "resolve_pass": resolve_pass,
                        "recall_clean_pass": recall_pass and _clean_refs(case, recall_refs),
                        "resolve_clean_pass": resolve_pass and _clean_refs(case, resolve_refs),
                        "recall_refs": sorted(recall_refs),
                        "resolve_refs": sorted(resolve_refs),
                        "recall_withheld": evidence.withheld_sensitive,
                        "recall_abstained": evidence.abstained,
                        "resolve_disposition": resolution.disposition.value,
                        "resolve_tier": resolution.tier,
                        "resolve_withheld": bool(resolution.withheld),
                        "resolve_answer": resolution.answer,
                        "recall_ms": recall_ms,
                        "resolve_ms": resolve_ms,
                    }
                )
        finally:
            conn.close()
    by_axis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        by_axis[item["axis"]].append(item)
    return {
        "benchmark": "varyz_capability_v3",
        "split": split,
        "cases_hash": _sha(CASES),
        "corpus_hash": _sha(CORPUS),
        "cases": len(results),
        "no_context_passed": sum(item["no_context_pass"] for item in results),
        "recall_passed": sum(item["recall_pass"] for item in results),
        "resolve_passed": sum(item["resolve_pass"] for item in results),
        "recall_clean_passed": sum(item["recall_clean_pass"] for item in results),
        "resolve_clean_passed": sum(item["resolve_clean_pass"] for item in results),
        "by_axis": {
            axis: {
                "no_context": sum(item["no_context_pass"] for item in rows),
                "recall": sum(item["recall_pass"] for item in rows),
                "resolve": sum(item["resolve_pass"] for item in rows),
                "recall_clean": sum(item["recall_clean_pass"] for item in rows),
                "resolve_clean": sum(item["resolve_clean_pass"] for item in rows),
                "cases": len(rows),
            }
            for axis, rows in sorted(by_axis.items())
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, choices=("dev", "held_out"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.split)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary_keys = (
        "split",
        "cases",
        "no_context_passed",
        "recall_passed",
        "resolve_passed",
        "recall_clean_passed",
        "resolve_clean_passed",
        "by_axis",
    )
    print(json.dumps({key: result[key] for key in summary_keys}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
