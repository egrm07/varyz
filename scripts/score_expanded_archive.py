"""Score private real-archive replays; publish counts and hashes only."""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "data/promise-archive-eval"
WINDOWS = (2, 3, 4, 6, 8, 9, 12)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(fraction * len(values)) - 1)]


def main():
    bindings = read(PRIVATE / "audited-source-bindings.json")
    aliases = read(PRIVATE / "audited-semantic-aliases.json")["aliases"]
    windows = []
    pairs = []
    hashes = {}
    for number in WINDOWS:
        folder = PRIVATE / f"round{number}"
        baseline = {2: "paired-baseline.json", 4: "final-baseline.json"}.get(
            number, "baseline.json")
        candidate = "candidate.json" if number == 12 else "source-guarded.json"
        data = read(folder / "dataset.json")
        arms = {"baseline": read(folder / baseline), "candidate": read(folder / candidate)}
        for name in ("dataset.json", baseline, candidate):
            hashes[f"round{number}/{name}"] = sha(folder / name)
        outcome = {"window": number, "facts": len(data["cases"]),
                   "fresh_untuned": number == 12, "arms": {}}
        successes = {}
        for arm, result in arms.items():
            assert result["dataset_sha256"] == sha(folder / "dataset.json")
            labels = []
            strict = semantic = wrong = unknown = 0
            for index, case in enumerate(data["cases"]):
                compatible = [a for a in aliases
                              if a["round"] == number and a["case"] == index]
                for query_index, _ in enumerate(case["queries"]):
                    value = result["results"][2 * index + query_index]
                    key = value.get("answered_by", {}).get("knowledge_id")
                    exact = (value.get("disposition") == "answered"
                             and value.get("answer") == case["answer"]
                             and key in bindings[str(number)][index])
                    audited = exact or any(key == a["source_id"]
                                           and value.get("answer") == a["answer"]
                                           for a in compatible)
                    strict += exact
                    semantic += audited
                    wrong += value.get("disposition") == "answered" and not audited
                    unknown += value.get("disposition") != "answered"
                    labels.append(bool(audited))
            successes[arm] = labels
            latencies = result.get("latency_seconds", [])
            outcome["arms"][arm] = {
                "n": len(labels), "source_bound_exact_correct": strict,
                "posthoc_audited_correct": semantic, "wrong": wrong, "unknown": unknown,
                "latency_seconds": {"n": len(latencies),
                    "median": statistics.median(latencies),
                    "p95": percentile(latencies, .95)} if latencies else None,
                "model": result.get("classifier_model"),
                "source_sha256": result.get("source_sha256"),
            }
        for i, _case in enumerate(data["cases"]):
            delta = sum(successes["candidate"][2*i:2*i+2]) - sum(
                successes["baseline"][2*i:2*i+2])
            pairs.append(delta / 2)
        outcome["paired_discordance"] = {
            "candidate_only": sum(c and not b for b, c in zip(
                successes["baseline"], successes["candidate"], strict=True)),
            "baseline_only": sum(b and not c for b, c in zip(
                successes["baseline"], successes["candidate"], strict=True)),
        }
        windows.append(outcome)
    totals = {arm: {key: sum(w["arms"][arm][key] for w in windows)
                    for key in ("n", "source_bound_exact_correct",
                                "posthoc_audited_correct", "wrong", "unknown")}
              for arm in ("baseline", "candidate")}
    rng = random.Random(1207)
    bootstrap = [statistics.mean(rng.choices(pairs, k=len(pairs))) for _ in range(2000)]
    report = {
        "date": "2026-10-09", "facts": len(pairs), "windows": windows,
        "totals": totals, "always_abstain_floor": {"correct": 0, "wrong": 0},
        "random_answer_floor": "Undefined for free-form sourced owner decisions.",
        "paired_case_bootstrap": {"seed": 1207, "resamples": 2000,
            "gain": statistics.mean(pairs), "descriptive_95_interval":
            [percentile(bootstrap, .025), percentile(bootstrap, .975)],
            "caveat": "Two queries per fact clustered. Source conversations remain correlated; "
                      "this is descriptive, not population significance or a promotion gate."},
        "replication_gate": {"minimum_correct": .8, "maximum_wrong": 0,
            "passed": False, "reason": "Fresh untouched window 3/4, equal to baseline. "
            "Other windows used during development cannot establish fresh replication."},
        "authority": read(PRIVATE / "round11/authority.json"),
        "artifact_checks": read(PRIVATE / "round11/card-checks.json")["checks"],
        "caveats": ["Retrospective recall, not human prompt counts or productivity.",
                    "Queries authored by evaluator; recorded decisions are real.",
                    "Post-hoc valid duplicate-answer audit is secondary; exact gold unchanged.",
                    "Proxy source groups are not independent conversations.",
                    "Recall candidate remains in checkout; no unreplicated recall rollout."],
        "private_input_sha256": hashes,
        "scorer_sha256": sha(Path(__file__)),
    }
    output = ROOT / "reports/promise_archive_expanded_2026-10-09.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"facts": len(pairs), "totals": totals,
                      "replication_passed": False, "report": str(output)}))


if __name__ == "__main__":
    main()
