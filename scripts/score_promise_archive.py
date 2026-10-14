"""Score frozen real-archive tool replays; export only aggregate data publicly."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def score(folder: Path) -> dict:
    dataset_bytes = (folder / "dataset.json").read_bytes()
    results_bytes = (folder / "results.json").read_bytes()
    dataset = json.loads(dataset_bytes)
    results = json.loads(results_bytes)
    decisions = [c for c in dataset["cases"] if c["promise"] == "fewer_prompts"]
    answers = results["decisions"]["results"]
    assert len(answers) == 2 * len(decisions)
    exact = paraphrase = wrong = abstained = 0
    exact_only = paraphrase_only = 0
    windows: dict[str, dict[str, int]] = {}
    for i, case in enumerate(decisions):
        paired = [
            a.get("disposition") == "answered" and a.get("answer") == case["answer"]
            for a in answers[2 * i : 2 * i + 2]
        ]
        exact_only += int(paired[0] and not paired[1])
        paraphrase_only += int(paired[1] and not paired[0])
        window = windows.setdefault(case["split"], {"n": 0, "correct": 0, "wrong": 0})
        for kind, answer in enumerate(answers[2 * i : 2 * i + 2]):
            correct = (
                answer.get("disposition") == "answered" and answer.get("answer") == case["answer"]
            )
            if kind == 0:
                exact += int(correct)
            else:
                paraphrase += int(correct)
                wrong += int(answer.get("disposition") == "answered" and not correct)
                abstained += int(answer.get("disposition") != "answered")
                window["n"] += 1
                window["correct"] += int(correct)
                window["wrong"] += int(answer.get("disposition") == "answered" and not correct)
    start = {}
    for result in results["start_task"]:
        assert result["status"] == "fulfilled", "Tool error must be scored explicitly"
        start[result["value"]["case_id"]] = result["value"]["response"]
    habits = continuity = away_controls = 0
    continuation_statuses = []
    for case in dataset["cases"]:
        if case["promise"] == "habits":
            response = start[case["id"]]
            rules = response.get("how_owner_works", []) + response.get("project_rules", [])
            # Match one complete rule, not disconnected keywords from separate rules.
            habits += int(
                any(
                    all(term in rule["statement"].lower() for term in case["required"])
                    for rule in rules
                )
            )
        elif case["promise"] == "continuity":
            value = start[case["id"]].get("continuation") or {}
            continuation_statuses.append(value.get("status", "missing"))
            continuity += int(bool(value.get("pending")))
        elif case["promise"] == "away":
            away_controls += int(start[case["id"]].get("away_mode", {}).get("active") is False)
    standards = 0
    for item in results["finish_check"]:
        value = item["response"]
        verification = value.get("verification", {})
        texts = [x["item"] for x in value.get("definition_of_done", [])]
        texts += [x["rule"] for x in value.get("standing_rules", [])]
        keyword = "popup" if item["case_id"] == "standards-popup" else "SEO basics"
        standards += int(
            any(keyword in t for t in texts)
            and verification.get("ready_to_report") is False
            and bool(verification.get("unchecked"))
        )
    return {
        "date": "2026-10-09",
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "real_source_records": len(dataset["sources"]),
        "owner_source_thread_clusters": len(
            {s["metadata"]["thread_id"] for s in dataset["sources"]}
        ),
        "frozen_cases": len(dataset["cases"]),
        "fewer_prompts": {
            "exact_correct": exact,
            "paraphrase_correct": paraphrase,
            "n_decisions": len(decisions),
            "wrong": wrong,
            "abstained": abstained,
            "windows": windows,
        },
        "habits": {
            "expected_rule_present": habits,
            "n": 2,
            "actual_agent_compliance": "not_measured",
        },
        "continuity": {
            "automatic_state_present": continuity,
            "n": 2,
            "statuses": continuation_statuses,
            "history_fallback_returned_reports": sum(
                bool(r["response"].get("reports")) for r in results["continuation_fallback"]
            ),
            "fallback_label_limit": (
                "Claude gold was mined-only; mixed-origin fallback returns a newer live report. "
                "Do not score source identity against the old mined gold."
            ),
        },
        "away": {
            "non_activation_controls_pass": away_controls,
            "n": 2,
            "unattended_positive_outcomes": "not_measured",
        },
        "standards": {
            "relevant_standard_and_incomplete_gate": standards,
            "n": 2,
            "finished_output_quality": "not_measured",
        },
        "baselines": {
            "abstain_all": {"correct": 0, "wrong": 0, "n": 4},
            "exact_replay": {"correct": exact, "n": 4},
            "random_chance": (
                "Undefined for free-form answers; no finite options or random choice test."
            ),
        },
        "paired_statistics": {
            "exact_only_success": exact_only,
            "paraphrase_only_success": paraphrase_only,
            "two_sided_exact_mcnemar_p": min(
                1.0,
                2
                * sum(
                    math.comb(exact_only + paraphrase_only, i)
                    for i in range(min(exact_only, paraphrase_only) + 1)
                )
                / (2 ** (exact_only + paraphrase_only)),
            ),
            "interpretation": (
                "Descriptive only: four pairs in two decision-thread clusters; "
                "no significance or population-effect claim."
            ),
        },
        "limitations": [
            "Targeted approved Varyz sample, not a random or representative archive census.",
            "Real source questions and answers; four short rephrasings authored by evaluator.",
            "Freeze preceded measured calls; "
            "current knowledge already contains historical answers. "
            "This is retrospective recall, not prospective preference prediction.",
            "Three decision cases share one source thread; "
            "small correlated N prevents reliable significance claims.",
            "Held-out window paraphrase: zero of one; later window: two of three. "
            "Effectiveness replication gate does not pass.",
            "No engine changes made or tuned against these cases.",
            "Raw sources, labels and results remain under gitignored data. "
            "Public report contains aggregates only.",
            "No generated deliverables, human prompt counts, "
            "arbitrary quality review or unattended positive run.",
            "Local withheld/access-required observations are recorded failures or limits; "
            "Cloud supplied already-approved labels only, not replacement local answers.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = score(args.folder)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
