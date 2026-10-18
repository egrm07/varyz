"""Score Varyz retrieval on the frozen public multi-source use-case benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import sqlite_vec

from varyz.config import load_config
from varyz.retrieval.answer import recall


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a non-mutating retrieval handle with the vec virtual table loaded."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_cases(path: Path, split: str) -> list[dict[str, Any]]:
    return [
        item
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for item in [json.loads(line)]
        if item["split"] == split
    ]


def _group_checks(text: str, groups: list[list[str]]) -> dict[str, bool]:
    lowered = text.casefold()
    return {
        f"group_{index}": any(term.casefold() in lowered for term in group)
        for index, group in enumerate(groups, start=1)
    }


def score_case(conn: Any, cfg: Any, case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    capsules: dict[str, Any] = {}
    withheld = 0
    abstentions = []
    for query in case["queries"][:3]:
        evidence = recall(conn, cfg, query, embedder=None, limit=8)
        withheld += evidence.withheld_sensitive
        abstentions.append(evidence.abstained)
        for capsule in evidence.capsules:
            capsules[capsule.external_id] = capsule
    text = "\n".join(capsule.text for capsule in capsules.values())
    refs = sorted(capsules)
    if case.get("expect_none"):
        checks = {"abstained": not capsules and all(abstentions)}
    elif case.get("expect_withheld"):
        checks = {"withheld": withheld > 0, "not_released": not capsules}
    else:
        checks = _group_checks(text, case["expect_groups"])
        checks["all_references"] = set(case["required_refs"]) <= set(refs)
    return {
        "id": case["id"],
        "use_case": case["use_case"],
        "passed": all(checks.values()),
        "checks": checks,
        "queries": len(case["queries"][:3]),
        "found_references": refs,
        "sources": sorted({capsule.source for capsule in capsules.values()}),
        "withheld_sensitive": withheld,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def evaluate(cases_path: Path, config_path: Path, split: str) -> dict[str, Any]:
    cases = load_cases(cases_path, split)
    cfg = load_config(config_path)
    conn = open_readonly(cfg.db_path)
    try:
        results = [score_case(conn, cfg, case) for case in cases]
    finally:
        conn.close()
    by_use_case: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        by_use_case[result["use_case"]].append(result["passed"])
    passed = sum(result["passed"] for result in results)
    no_context = sum(
        bool(case.get("expect_none") or case.get("expect_withheld")) for case in cases
    )
    latencies = [result["latency_ms"] for result in results]
    return {
        "benchmark": "varyz_use_cases_v1",
        "split": split,
        "cases_hash": hashlib.sha256(cases_path.read_bytes()).hexdigest()[:16],
        "cases": len(cases),
        "passed": passed,
        "rate": passed / len(cases) if cases else 0.0,
        "no_context_floor": {
            "passed": no_context,
            "rate": no_context / len(cases) if cases else 0.0,
            "definition": (
                "Only correct abstention and privacy withholding pass without "
                "archive context."
            ),
        },
        "latency_ms": {
            "median": round(statistics.median(latencies), 2) if latencies else 0.0,
            "max": max(latencies, default=0.0),
            "total": round(sum(latencies), 2),
        },
        "by_use_case": {
            key: {"passed": sum(values), "cases": len(values)}
            for key, values in sorted(by_use_case.items())
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("evals/varys_use_cases_v1.jsonl"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("data/evals/varys_use_cases_v1/config.toml"),
    )
    parser.add_argument("--split", choices=("dev", "held_out"), required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = evaluate(args.cases, args.config, args.split)
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
