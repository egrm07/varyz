"""The recall benchmark's own rules: frozen cases, clean distractors, scoring.

A benchmark whose distractors can answer an unanswerable case, or whose
sensitive cases pass when a stand-in is handed over, reports numbers that
mean nothing. These pin the rules the published figures depend on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "recall_bench.py"
SPEC = importlib.util.spec_from_file_location("recall_bench", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bench = importlib.util.module_from_spec(SPEC)
sys.modules["recall_bench"] = bench
SPEC.loader.exec_module(bench)


CASES = bench.load_jsonl(bench.BENCH / "cases.jsonl")
NEEDLES = bench.load_jsonl(bench.BENCH / "needles.jsonl")


def test_every_case_points_at_needles_that_exist():
    ids = {needle["external_id"] for needle in NEEDLES}
    assert len(ids) == len(NEEDLES)
    for case in CASES:
        for field in ("required", "stale", "withheld"):
            assert set(case.get(field, [])) <= ids, (case["id"], field)


def test_the_splits_are_the_same_shape():
    assert len({case["id"] for case in CASES}) == len(CASES)
    shapes = {}
    for case in CASES:
        shapes.setdefault(case["split"], {}).setdefault(case["type"], 0)
        shapes[case["split"]][case["type"]] += 1
    assert set(shapes) == {"dev", "held_out", "sealed", "sealed2"}
    assert shapes["sealed"] == shapes["held_out"]
    assert shapes["sealed2"] == shapes["held_out"]
    for split in ("held_out", "sealed", "sealed2"):
        assert shapes["dev"].get("none") == shapes[split].get("none")
        assert shapes["dev"].get("sensitive") == shapes[split].get("sensitive")


def test_distractors_are_deterministic_and_never_answer_a_case():
    first = bench.generate_distractors(count=400)
    assert first == bench.generate_distractors(count=400)
    for row in first:
        text = f"{row['title']} {row['body']}".lower()
        assert not any(word in text for word in bench.FORBIDDEN)


def test_a_forbidden_word_stops_the_build(monkeypatch):
    def leaky(rng):
        return "gmail", "message", "Hello", f"about {bench.FORBIDDEN[0]}"

    monkeypatch.setattr(bench, "GENERATORS", [(1.0, leaky)])
    with pytest.raises(AssertionError, match="forbidden"):
        bench.generate_distractors(count=1)


ANSWERABLE = {"id": "a", "type": "join", "required": ["x", "y"]}


def test_a_join_needs_every_record():
    half = bench.score_case(ANSWERABLE, bench.Returned(["x", "noise"]))
    assert not half["passed"]
    assert half["record_recall"] == 0.5
    assert half["unrelated"] == 1
    whole = bench.score_case(ANSWERABLE, bench.Returned(["noise", "y", "x"]))
    assert whole["passed"]
    assert whole["mrr"] == 0.5


def test_a_current_case_needs_the_new_fact_before_the_old():
    case = {"id": "c", "type": "current", "required": ["new"], "stale": ["old"]}
    assert bench.score_case(case, bench.Returned(["new", "old"]))["passed"]
    assert not bench.score_case(case, bench.Returned(["old", "new"]))["passed"]


def test_an_unanswerable_case_passes_only_on_silence():
    case = {"id": "n", "type": "none"}
    assert bench.score_case(case, bench.Returned([]))["passed"]
    result = bench.score_case(case, bench.Returned(["anything"]))
    assert not result["passed"] and result["false_evidence"]


def test_a_stand_in_for_a_withheld_record_fails():
    case = {"id": "s", "type": "sensitive", "withheld": ["secret"]}
    assert bench.score_case(case, bench.Returned([], withheld=1))["passed"]
    stand_in = bench.score_case(case, bench.Returned(["neighbour"]))
    assert not stand_in["passed"] and stand_in["stand_in"]
    leak = bench.score_case(case, bench.Returned(["secret"]))
    assert leak["leaked"] == ["secret"] and not leak["stand_in"]


def test_the_summary_counts_what_the_report_prints():
    results = [
        bench.score_case(ANSWERABLE, bench.Returned(["x", "y"])),
        bench.score_case(ANSWERABLE, bench.Returned([])),
        bench.score_case({"id": "n", "type": "none"}, bench.Returned(["z"])),
        bench.score_case(
            {"id": "s", "type": "sensitive", "withheld": ["q"]}, bench.Returned([], 1)
        ),
    ]
    summary = bench.summarise(results, [10.0, 20.0, 30.0, 40.0])
    assert summary["answered_correctly"] == [2, 4]
    assert summary["complete_evidence"] == [1, 2]
    assert summary["false_refusal"] == [1, 2]
    assert summary["false_evidence"] == [1, 1]
    assert summary["sensitive_leaks"] == 0
    assert summary["latency_ms"]["p50"] == 25.0
    low, high = summary["wilson95"]
    assert low < 0.5 < high
