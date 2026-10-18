from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "score_codex_varyz.py"
SPEC = importlib.util.spec_from_file_location("score_codex_varyz", SCRIPT)
assert SPEC and SPEC.loader
scorer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scorer
SPEC.loader.exec_module(scorer)


def test_groups_citations_and_abstention_are_all_required():
    cases = [
        {
            "id": "fact",
            "expect_groups": [["natural key"], ["cursor", "checkpoint"]],
        },
        {"id": "unknown", "expect_none": True},
    ]
    payload = {
        "answers": [
            {
                "id": "fact",
                "answer": "A natural key and transaction checkpoint cursor prevent repeats.",
                "citations": ["ARCHITECTURE.md"],
                "abstained": False,
            },
            {
                "id": "unknown",
                "answer": "The archive does not establish this.",
                "citations": [],
                "abstained": True,
            },
        ]
    }

    result = scorer.score(cases, payload)

    assert result["passed"] == 2
    payload["answers"][0]["citations"] = []
    payload["answers"][1]["abstained"] = False
    assert scorer.score(cases, payload)["passed"] == 0
