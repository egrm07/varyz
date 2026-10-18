"""Score a blind agent run on the frozen Varyz routing benchmark."""

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
    decisions = {str(item.get("id")): item for item in payload.get("decisions", ())}
    results = []
    for case in cases:
        decision = decisions.get(case["id"], {})
        route_ok = decision.get("route") == case["expected_route"]
        handling_ok = decision.get("handling") == case["expected_handling"]
        results.append(
            {
                "id": case["id"],
                "passed": route_ok and handling_ok,
                "route_ok": route_ok,
                "handling_ok": handling_ok,
                "route": decision.get("route"),
                "handling": decision.get("handling"),
            }
        )
    passed = sum(item["passed"] for item in results)
    route_passed = sum(item["route_ok"] for item in results)
    handling_passed = sum(item["handling_ok"] for item in results)
    total = len(results)
    return {
        "cases": total,
        "passed": passed,
        "rate": passed / total if total else 0.0,
        "route_accuracy": route_passed / total if total else 0.0,
        "handling_accuracy": handling_passed / total if total else 0.0,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", type=Path)
    parser.add_argument(
        "--cases", type=Path, default=Path("evals/varys_agent_routing_v1.jsonl")
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = score(load_cases(args.cases), json.loads(args.answer.read_text(encoding="utf-8")))
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
