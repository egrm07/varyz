"""Freeze the last untouched approved decision dimensions before replay."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
folder = root / "data/promise-archive-eval/round12"
path = folder / "dataset.json"
if path.exists():
    raise FileExistsError("Never overwrite a frozen window")
records = json.loads((folder / "unqueried-records.json").read_text(encoding="utf-8"))
dimensions = {
    "01M3A7Z5AK7RZQTY8ED4X3S8JX": [
        "What tone should the marketing website copy use?",
        "Should the marketing page explain technical details or focus on letting go?",
    ],
    "01M3A7Z1YA6C2GGCFDS5P547EW": [
        "Does the owner want website analytics?",
        "Which analytics should the marketing site use?",
    ],
}
cases = [{**r, "queries": dimensions[r["id"]], "split": "fresh_replication"}
         for r in records if r["id"] in dimensions]
assert len(cases) == 2
path.write_text(json.dumps({"date": "2026-10-09", "frozen_before_queries": True,
    "gate": {"minimum_correct": 0.8, "wrong": 0},
    "note": "Untested facts and query dimensions. Previously disclosed records; "
            "one relay cluster, only two facts. No tuning after this freeze. "
            "This small retrospective window cannot establish productivity or population lift.",
    "cases": cases}, indent=2), encoding="utf-8")
print(json.dumps({"cases": len(cases), "queries": 4,
                  "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
