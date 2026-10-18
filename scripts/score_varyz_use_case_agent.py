"""Deterministically score agent answers on the frozen Varyz use-case eval."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_cases(path: Path, split: str) -> list[dict[str, Any]]:
    return [
        item
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for item in [json.loads(line)]
        if item["split"] == split
    ]


def score(cases: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    answers = {str(item["id"]): item for item in payload.get("answers", ())}
    results = []
    by_use_case: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        item = answers.get(case["id"], {})
        text = str(item.get("answer", "")).casefold()
        citations = [str(value) for value in item.get("citations", ())]
        if case.get("expect_none"):
            checks = {
                "abstained": bool(item.get("abstained")),
                "uncited": not citations,
            }
        elif case.get("expect_withheld"):
            checks = {
                "withheld_sensitive": bool(item.get("withheld_sensitive")),
                "abstained": bool(item.get("abstained")),
                "uncited": not citations,
            }
        else:
            checks = {
                f"group_{index}": any(term.casefold() in text for term in group)
                for index, group in enumerate(case["expect_groups"], start=1)
            }
            checks["all_references"] = set(case["required_refs"]) <= set(citations)
            checks["answered"] = not bool(item.get("abstained"))
            checks["not_marked_sensitive"] = not bool(item.get("withheld_sensitive"))
        passed = bool(checks) and all(checks.values())
        by_use_case[case["use_case"]].append(passed)
        results.append(
            {
                "id": case["id"],
                "use_case": case["use_case"],
                "passed": passed,
                "checks": checks,
            }
        )
    passed = sum(item["passed"] for item in results)
    return {
        "cases": len(results),
        "passed": passed,
        "rate": passed / len(results) if results else 0.0,
        "by_use_case": {
            key: {"passed": sum(values), "cases": len(values)}
            for key, values in sorted(by_use_case.items())
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", type=Path)
    parser.add_argument(
        "--cases", type=Path, default=Path("evals/varys_use_cases_v1.jsonl")
    )
    parser.add_argument("--split", choices=("dev", "held_out"), required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = score(
        load_cases(args.cases, args.split),
        json.loads(args.answer.read_text(encoding="utf-8")),
    )
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
