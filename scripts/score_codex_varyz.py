"""Deterministically score the public-project Codex/Varyz development cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def score(cases: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    answers = {str(item["id"]): item for item in payload.get("answers", ())}
    results = []
    for case in cases:
        item = answers.get(case["id"], {})
        text = str(item.get("answer", "")).casefold()
        citations = [str(value) for value in item.get("citations", ())]
        if case.get("expect_none"):
            checks = {
                "abstained": bool(item.get("abstained")),
                "uncited": not citations,
            }
        else:
            checks = {
                f"group_{index}": any(term.casefold() in text for term in group)
                for index, group in enumerate(case["expect_groups"], start=1)
            }
            checks["cited"] = bool(citations)
            checks["answered"] = not bool(item.get("abstained"))
        results.append(
            {
                "id": case["id"],
                "passed": bool(checks) and all(checks.values()),
                "checks": checks,
            }
        )
    passed = sum(item["passed"] for item in results)
    return {
        "cases": len(results),
        "passed": passed,
        "rate": passed / len(results) if results else 0.0,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", type=Path)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("evals/codex_varys_dev.jsonl"),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = score(
        load_cases(args.cases),
        json.loads(args.answer.read_text(encoding="utf-8")),
    )
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
