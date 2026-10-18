"""Publish aggregate manual audits only; raw archive messages stay private."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
private = root / "data/promise-archive-eval"
inputs = ["round26/labels.json", "round26/reader.json", "round27/labels.json",
          "round27/reader.json", "round28/labels.json", "round28/reader.json",
          "round30/labels.json", "round30/reader.json"]
report = {
    "date": "2026-10-09",
    "metric": "Manual source-bound retention of four required facts in four real messages.",
    "grading": "Post-run manual audit against pre-run frozen source labels; "
               "counts facts, not accepted-note totals or model self-grades.",
    "paired_first_replication": {
        "messages": 4, "required_facts": 4,
        "baseline": {"correct": 1, "unsupported": 1, "missing": 2},
        "candidate": {"correct": 2, "unsupported": 0, "missing": 2},
        "gate": {"minimum_correct_fraction": 0.8, "maximum_unsupported": 0,
                 "passed": False},
        "failure_categories": ["Missed document privacy restriction in both arms.",
                               "Missed project repair-model choice in both arms.",
                               "Baseline reversed when the main agent can reply."],
    },
    "latest_development": {
        "messages": 4, "required_positive_facts": 2, "correct": 1,
        "unsupported": 0, "missed": 1,
        "negative_controls_respected": 2,
        "change": "Keeps paired project choice; rejects rule missing named source mode.",
        "fresh_replication": False,
    },
    "always_abstain_floor": {"correct": 0, "unsupported": 0},
    "random_answer_floor": "Undefined for free-form source-grounded facts.",
    "model": "qwen3.5:4b", "temperature": 0.0,
    "model_seed": "Not exposed by the existing reader provider.",
    "candidate_installed": False, "live_owner_ledger_changed": False,
    "caveats": [
        "Four-message replication is too small for population inference.",
        "These are extraction review notes, not confirmed rules or lasting habit usage.",
        "Sources were approved Varyz history; some facts had been disclosed in other contexts.",
        "After the first replication, further reader changes remain unreplicated.",
        "No confidence interval or significance claim for these manually graded few facts.",
    ],
    "private_input_sha256": {name: hashlib.sha256((private / name).read_bytes()).hexdigest()
                             for name in inputs},
}
destination = root / "reports/promise_archive_reader_2026-10-09.json"
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"candidate_correct": 2, "baseline_correct": 1,
                  "required_facts": 4, "gate_passed": False}))
