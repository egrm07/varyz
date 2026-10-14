"""Measure how well each local classifier tells new work from a small change.

    uv run python scripts/task_size_bench.py [--candidates lexicon,embed,model]
                                             [--model qwen3.5:4b]
                                             [--out reports/task_size_2026-09-25]

A workflow nudge is raised only when a task starts *new* work at a later step;
a small change to work already there is just done. This bench scores the
candidates for that call on two authored sets in scripts/fixtures: the dev set
(the prompt wording, the few-shot examples and the embedding's centroids come
only from it, and it is the ``EXAMPLES`` shipped in ``varyz.resolution.task_size``)
and the held-out set, written with it before any run and never tuned on.
Nothing here reads the owner's archive or prompts.

Candidates, scored through the classes varyz ships:

* ``lexicon`` - the verb lexicon in ``varyz.resolution.workflows`` (baseline);
* ``embed`` - ``EmbeddingSizer`` (nearest centroid) on the archive's local
  sentence embedding; dev is scored leave-one-out. A k-nearest-neighbour head
  on the same vectors is reported for comparison and not shipped;
* ``model`` - ``ModelSizer``: a local model through Ollama asked for one letter,
  read from the first token's log-probabilities, with the eight dev examples
  (shipped) and zero-shot (not shipped).

Accuracy carries a Wilson 95% interval; chance is 50% and the majority floor is
the larger class's share. Each candidate is compared with the lexicon on the
held-out set by an exact McNemar test. Latency is wall clock per call on this
machine; for the model, the cold first call (loading it) is reported apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts" / "fixtures"

from varyz.resolution.task_size import (  # noqa: E402
    EXAMPLES,
    FEW_SHOT,
    LETTER_PROMPT,
    EmbeddingSizer,
    ModelSizer,
)
from varyz.resolution.workflows import parse_steps, task_size  # noqa: E402


def load(name: str) -> tuple[list[dict[str, Any]], dict[str, list[str]], str]:
    path = FIXTURES / name
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    return data["items"], data["workflows"], hashlib.sha256(raw).hexdigest()


def wilson(correct: int, total: int) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    z = 1.96
    p = correct / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (
        1 + z * z / total
    )
    return (round(centre - half, 3), round(centre + half, 3))


def score(items: Sequence[dict[str, Any]], predictions: Sequence[str]) -> dict[str, Any]:
    pairs = list(zip(items, predictions, strict=True))
    correct = sum(item["label"] == got for item, got in pairs)
    majority = max(
        sum(item["label"] == "new" for item in items),
        sum(item["label"] == "small" for item in items),
    )
    return {
        "n": len(items),
        "correct": correct,
        "accuracy": round(correct / len(items), 3),
        "ci95": wilson(correct, len(items)),
        "majority_floor": round(majority / len(items), 3),
        "confusion": {
            f"{want}->{got}": sum(
                item["label"] == want and pred == got for item, pred in pairs
            )
            for want in ("new", "small")
            for got in ("new", "small")
        },
        "errors": [
            {"task": item["task"], "label": item["label"], "got": pred}
            for item, pred in pairs
            if item["label"] != pred
        ],
    }


def mcnemar(
    items: Sequence[dict[str, Any]], a: Sequence[str], b: Sequence[str]
) -> dict[str, Any]:
    """Exact two-sided McNemar test on the items the two candidates disagree on."""
    rows = list(zip(items, a, b, strict=True))
    only_a = sum(x == item["label"] != y for item, x, y in rows)
    only_b = sum(y == item["label"] != x for item, x, y in rows)
    n = only_a + only_b
    k = min(only_a, only_b)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"only_first_right": only_a, "only_second_right": only_b,
            "p_two_sided": round(p, 5)}


# -- candidates ---------------------------------------------------------------


def run_lexicon(
    items: Sequence[dict[str, Any]], workflows: dict[str, list[str]]
) -> list[str]:
    return [
        task_size(item["task"], parse_steps(workflows[item["workflow"]])) for item in items
    ]


def run_embed(
    dev: Sequence[dict[str, Any]], held: Sequence[dict[str, Any]]
) -> tuple[dict[str, dict[str, list[str]]], dict[str, Any]]:
    import numpy as np

    from varyz.config import load_config
    from varyz.pipeline.embed import Embedder

    embedder = Embedder(load_config(None))
    shipped = EmbeddingSizer(embedder.embed_documents)
    started = time.perf_counter()
    held_shipped = [shipped(item["task"], ()).size for item in held]
    per_call = (time.perf_counter() - started) * 1000 / len(held)

    def unit(texts: list[str]) -> Any:
        vectors = np.array(embedder.embed_documents(texts), dtype=float)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    dev_vecs = unit([item["task"] for item in dev])
    held_vecs = unit([item["task"] for item in held])
    labels = np.array([item["label"] for item in dev])

    def centroid(train: Any, lab: Any, test: Any) -> list[str]:
        new = train[lab == "new"].mean(axis=0)
        small = train[lab == "small"].mean(axis=0)
        return ["new" if row @ new >= row @ small else "small" for row in test]

    def knn(train: Any, lab: Any, test: Any, k: int = 5) -> list[str]:
        out = []
        for row in test:
            top = (train @ row).argsort()[::-1][:k]
            out.append("new" if sum(1 if lab[i] == "new" else -1 for i in top) > 0
                       else "small")
        return out

    results: dict[str, dict[str, list[str]]] = {}
    for name, head in (("embed-centroid (shipped)", centroid), ("embed-knn5", knn)):
        loo: list[str] = []
        for i in range(len(dev)):
            keep = np.arange(len(dev)) != i
            loo.extend(head(dev_vecs[keep], labels[keep], dev_vecs[i : i + 1]))
        results[name] = {"dev": loo, "held_out": head(dev_vecs, labels, held_vecs)}
    # The shipped class and the reference computation must agree.
    assert held_shipped == results["embed-centroid (shipped)"]["held_out"]
    return results, {"per_call_ms_after_load": round(per_call, 1)}


def run_model(
    model: str, dev: Sequence[dict[str, Any]], held: Sequence[dict[str, Any]]
) -> tuple[dict[str, dict[str, list[str]]], dict[str, Any], dict[str, Any]]:
    import httpx

    results: dict[str, dict[str, list[str]]] = {}
    probabilities: dict[str, Any] = {}
    latencies: list[float] = []
    cold: float | None = None
    try:
        for few_shot in (True, False):
            sizer = ModelSizer(model, timeout_seconds=120.0, few_shot=few_shot)
            name = f"model {model}" + (" few-shot (shipped)" if few_shot else " zero-shot")
            probabilities[name] = {}
            for split, items in (("dev", dev), ("held_out", held)):
                values = []
                for item in items:
                    started = time.perf_counter()
                    values.append(sizer.p_new(item["task"]))
                    elapsed = (time.perf_counter() - started) * 1000
                    if cold is None:
                        cold = elapsed
                    else:
                        latencies.append(elapsed)
                probabilities[name][split] = [round(v, 4) for v in values]
            results[name] = {
                split: ["new" if p >= 0.5 else "small" for p in values]
                for split, values in probabilities[name].items()
            }
    finally:
        httpx.post("http://127.0.0.1:11434/api/generate",
                   json={"model": model, "keep_alive": 0}, timeout=30.0)
    latency = {
        "cold_first_call_ms": round(cold or 0.0, 1),
        "warm_median_ms": round(statistics.median(latencies), 1),
        "warm_p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1),
    }
    return results, latency, probabilities


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--candidates", default="lexicon,embed,model")
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    dev, workflows, dev_hash = load("task_size_dev.json")
    held, held_workflows, held_hash = load("task_size_held_out.json")
    workflows = {**workflows, **held_workflows}
    assert [(item["task"], item["label"]) for item in dev] == list(EXAMPLES), (
        "the shipped EXAMPLES must be the dev set"
    )
    predictions: dict[str, dict[str, list[str]]] = {}
    latency: dict[str, Any] = {}
    probabilities: dict[str, Any] = {}
    for candidate in (part.strip() for part in args.candidates.split(",")):
        print(f"running {candidate} ...", file=sys.stderr, flush=True)
        if candidate == "lexicon":
            started = time.perf_counter()
            predictions["lexicon (baseline)"] = {
                "dev": run_lexicon(dev, workflows), "held_out": run_lexicon(held, workflows)
            }
            latency["lexicon"] = {"per_call_ms": round(
                (time.perf_counter() - started) * 1000 / (len(dev) + len(held)), 3)}
        elif candidate == "embed":
            result, timing = run_embed(dev, held)
            predictions.update(result)
            latency["embed"] = timing
        elif candidate == "model":
            result, timing, probs = run_model(args.model, dev, held)
            predictions.update(result)
            latency[f"model {args.model}"] = timing
            probabilities.update(probs)
        else:
            parser.error(f"unknown candidate {candidate}")

    results = {
        name: {split: score(dev if split == "dev" else held, preds[split])
               for split in ("dev", "held_out")}
        for name, preds in predictions.items()
    }
    baseline = predictions.get("lexicon (baseline)")
    comparisons = {
        name: mcnemar(held, preds["held_out"], baseline["held_out"])
        for name, preds in predictions.items()
        if baseline is not None and name != "lexicon (baseline)"
    }
    summary = {
        "date": time.strftime("%Y-%m-%d"),
        "fixtures_sha256": {"dev": dev_hash, "held_out": held_hash},
        "prompt": LETTER_PROMPT,
        "few_shot_examples": [task for task, _ in FEW_SHOT],
        "chance": 0.5,
        "results": results,
        "held_out_vs_lexicon": comparisons,
        "latency": latency,
    }
    for name, result in results.items():
        print(
            f"{name:44s} dev {result['dev']['accuracy']:.3f}  held-out "
            f"{result['held_out']['correct']}/{result['held_out']['n']} "
            f"{result['held_out']['accuracy']:.3f} {result['held_out']['ci95']}"
        )
    print(json.dumps({"held_out_vs_lexicon": comparisons, "latency": latency}, indent=1))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(
            json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (args.out / "p_new.json").write_text(
            json.dumps(probabilities, indent=1) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
