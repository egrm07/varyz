from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trr.py"
SPEC = importlib.util.spec_from_file_location("trr", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
trr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trr
SPEC.loader.exec_module(trr)


CASES = [
    {
        "id": "answer",
        "split": "dev",
        "expect_groups": [["append-only"], ["transaction"]],
        "required_refs": ["chat", "commit"],
    },
    {"id": "unknown", "split": "dev", "expect_none": True},
    {"id": "private", "split": "dev", "expect_withheld": True},
]


def _score(answers):
    return trr.score(CASES, {"resolutions": answers}, suite="fixture", split="dev")


def test_trr_scores_the_three_trusted_outcomes_separately() -> None:
    result = _score(
        [
            {
                "id": "answer",
                "disposition": "answered",
                "answer": "An append-only journal is replayed in one transaction.",
                "citations": ["chat", "commit"],
            },
            {"id": "unknown", "disposition": "not_known"},
            {
                "id": "private",
                "disposition": "clarification",
                "basis": "escalated",
                "escalation_id": "esc-1",
            },
        ]
    )

    assert result["trr"] == 1.0
    assert result["buckets"]["answered_correctly"]["count"] == 1
    assert result["buckets"]["correctly_abstained"]["count"] == 1
    assert result["buckets"]["escalated_appropriately"]["count"] == 1


def test_wrong_answers_are_split_by_failure_mode() -> None:
    cases = [
        CASES[0],
        {**CASES[0], "id": "bad-source"},
        {**CASES[0], "id": "withheld"},
    ]
    result = trr.score(
        cases,
        {
            "answers": [
                {
                    "id": "answer",
                    "answer": "A queue did it.",
                    "citations": ["chat", "commit"],
                    "abstained": False,
                },
                {
                    "id": "bad-source",
                    "answer": "An append-only journal uses one transaction.",
                    "citations": ["chat"],
                    "abstained": False,
                },
                {"id": "withheld", "answer": "", "citations": [], "abstained": True},
            ]
        },
        suite="fixture",
    )

    assert result["wrong"] == {
        "fabricated": {"count": 1, "rate": 1 / 3},
        "mis_sourced": {"count": 1, "rate": 1 / 3},
        "wrongly_withheld": {"count": 1, "rate": 1 / 3},
    }


def test_blanket_abstention_scores_zero_and_reports_the_rate() -> None:
    result = _score(
        [{"id": case["id"], "disposition": "not_known"} for case in CASES]
    )

    assert result["blanket_abstention"] is True
    assert result["abstention_rate"] == 1.0
    assert result["trr"] == 0.0


def test_legacy_baselines_are_compared_without_claiming_the_same_corpus() -> None:
    baselines = {
        label: {"cases": 6, "passed": passed, "rate": passed / 6}
        for label, passed in zip("ABCD", (1, 1, 4, 5), strict=True)
    }
    result = trr.score(
        [CASES[1]],
        {"answers": [{"id": "unknown", "abstained": True}]},
        suite="fixture",
        baselines=baselines,
    )

    # One unknown-only fixture is blanket abstention, so it cannot manufacture a win.
    assert result["trr"] == 0.0
    assert result["baselines"]["D"]["delta"] == -(5 / 6)
    assert "not scores on this TRR corpus" in result["report"]["caveats"][0]


def test_heldout_requires_explicit_authorization_before_cases_are_read(tmp_path: Path) -> None:
    missing_cases = tmp_path / "missing.jsonl"
    answers = tmp_path / "answers.json"
    answers.write_text('{"answers": []}', encoding="utf-8")

    with pytest.raises(trr.HeldoutRefused, match="sealed"):
        trr.evaluate(missing_cases, answers, split="held_out")


def test_heldout_ledger_can_be_burned_only_once(tmp_path: Path) -> None:
    ledger = tmp_path / "heldout-ledger.json"

    trr.reserve_heldout_once(ledger, suite="fixture", cases_hash="abc")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["scored"]["fixture:abc"]["cases_sha256"] == "abc"
    with pytest.raises(trr.HeldoutRefused, match="already scored"):
        trr.reserve_heldout_once(ledger, suite="fixture", cases_hash="abc")


def test_dev_loader_never_mixes_in_heldout_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '\n'.join(
            (
                '{"id":"d","split":"dev","expect_none":true}',
                '{"id":"h","split":"held_out","expect_none":true}',
            )
        ),
        encoding="utf-8",
    )

    assert [case["id"] for case in trr.load_cases(path, "dev")] == ["d"]
