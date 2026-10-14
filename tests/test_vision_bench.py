from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vision_bench.py"
SPEC = importlib.util.spec_from_file_location("vision_bench", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
vision_bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vision_bench
SPEC.loader.exec_module(vision_bench)


def test_dev_split_holds_the_safety_invariants_of_the_vision() -> None:
    result = vision_bench.run("dev", runs=3)

    assert result["paraphrase"]["wrong_answers"] == 0
    assert result["withheld_escalation"]["leaks"] == 0
    assert result["withheld_escalation"]["dead_ends"] == 0
    assert result["correction"]["stale_answer"] == 0
    assert result["correction"]["retracted_hidden"] is True
    assert result["disclosure"]["unauthorized_disclosures"] == 0
    for path, expected_tier in (
        ("tier1_exact", 1), ("tier1_paraphrase", 1), ("tier2_evidence", 2),
        ("tier2_withheld", 2), ("miss_not_known", 5),
    ):
        assert result["latency"][path]["tier"] == expected_tier, path


def test_held_out_splits_are_hashed_apart_and_burn_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(vision_bench, "HELDOUT_LEDGER", ledger)
    assert vision_bench.dataset_hash("held_out") == vision_bench.dataset_hash("dev")
    assert len({vision_bench.dataset_hash(split) for split in vision_bench.HELD_OUT_SPLITS}) == 4

    vision_bench._burn_held_out("first", "held_out_v2")
    with pytest.raises(SystemExit, match="already scored"):
        vision_bench._burn_held_out("again", "held_out_v2")
    vision_bench._burn_held_out("other split", "held_out")
    assert [item["label"] for item in json.loads(ledger.read_text(encoding="utf-8"))] == [
        "first", "other split",
    ]
