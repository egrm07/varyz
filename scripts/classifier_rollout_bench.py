"""Measure each new use of the local classifier against the rule it replaces.

    uv run python scripts/classifier_rollout_bench.py --out reports/classifier_rollout_2026-09-25
        [--sections work,stop_gate,redo,scope,ask] [--authorize-held-out LABEL]

One local model (qwen3.5:4b through Ollama on loopback) now answers five closed
questions in varyz. For each, this scores the shipped chain against the rule it
replaces, on authored sets written and frozen before any run:

* ``work`` - does a prompt ask for work (brief the agent) or only for a reply?
  ``hooks.is_task`` with the wording rule vs with the chain. Authored dev and
  held-out sets, plus the owner's real prompts labelled locally
  (``data/labels``, never committed; only aggregate numbers leave this machine).
* ``stop_gate`` - the finish check skips a small change. Scored on the real
  prompts labelled new or small: a new task skipped is a definition of done
  missed (the cost), a small one skipped is a block saved.
* ``redo`` - does a task redo a step? ``workflows.is_redo`` (verb list) vs the chain.
* ``scope`` - is an unscoped answer general or for this project?
  ``suggest_scope`` heuristics vs the model at the fail-narrow bar.
* ``ask`` - the paraphrase verifier behind the strict matcher. Development is
  the vision sets v1-v3 (burned); the v4 held-out set is scored once, through
  the ledger, only with ``--authorize-held-out``.

Accuracy carries a Wilson 95% interval; each chain is compared with its rule by
an exact two-sided McNemar test on the same items. Latency is wall clock on this
machine: warm (model loaded) median and p95, and cold (the model unloaded first).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts" / "fixtures"
LABELS = ROOT / "data" / "labels"
sys.path.insert(0, str(ROOT / "scripts"))

from task_size_bench import mcnemar, wilson  # noqa: E402

from varyz.config import load_config  # noqa: E402
from varyz.hooks import is_task, task_text  # noqa: E402
from varyz.resolution.local_classifier import (  # noqa: E402
    BinaryClassifier,
    Centroids,
    LocalModel,
    embedder_from_config,
    local_model_from_config,
)
from varyz.resolution.prompt_kind import WORK_EXAMPLES, WORK_QUESTION  # noqa: E402
from varyz.resolution.scope import SCOPE_QUESTION, scope_exchange, suggest_scope  # noqa: E402
from varyz.resolution.task_size import (  # noqa: E402
    REDO_EXAMPLES,
    REDO_QUESTION,
    EmbeddingSizer,
    ModelSizer,
)
from varyz.resolution.workflows import is_redo, lexicon_sizer, parse_steps  # noqa: E402

MODEL = "qwen3.5:4b"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(name: str) -> tuple[dict[str, Any], str]:
    path = FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8")), sha(path)


def score(labels: Sequence[str], preds: Sequence[str]) -> dict[str, Any]:
    correct = sum(a == b for a, b in zip(labels, preds, strict=True))
    kinds = sorted(set(labels) | set(preds))
    return {
        "n": len(labels),
        "correct": correct,
        "accuracy": round(correct / len(labels), 3) if labels else None,
        "ci95": wilson(correct, len(labels)),
        "confusion": {
            f"{want}->{got}": sum(
                a == want and b == got for a, b in zip(labels, preds, strict=True)
            )
            for want in kinds for got in kinds if want != got
        },
    }


def compare(labels: Sequence[str], chain: Sequence[str], rule: Sequence[str]) -> dict[str, Any]:
    items = [{"label": label} for label in labels]
    return mcnemar(items, chain, rule)


class Timer:
    def __init__(self) -> None:
        self.samples: list[float] = []

    def __call__(self, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        value = fn()
        self.samples.append((time.perf_counter() - started) * 1000)
        return value

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {}
        ordered = sorted(self.samples)
        return {
            "n": len(ordered),
            "median_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 1),
        }


def unload() -> None:
    httpx.post("http://127.0.0.1:11434/api/generate",
               json={"model": MODEL, "keep_alive": 0}, timeout=60.0)
    time.sleep(2.0)


def cold_call(fn: Callable[[], Any]) -> float:
    """One call with the model unloaded first: what a use pays after 5 idle minutes."""
    unload()
    started = time.perf_counter()
    fn()
    return round((time.perf_counter() - started) * 1000, 1)


def chain_probabilities(model: LocalModel, question: Any, texts: Sequence[str]) -> tuple[
    list[float], Timer
]:
    timer = Timer()
    return [timer(lambda t=text: model.p_a(question, t)) for text in texts], timer


# -- work vs question ------------------------------------------------------------


def real_prompts() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The labelled sample of the owner's prompts; ids and labels only leave this."""
    sample = LABELS / "real_prompts_sample.jsonl"
    labels_path = LABELS / "real_prompts_labels.txt"
    prompts = {
        row["id"]: row for row in (
            json.loads(line) for line in sample.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    items = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or line.startswith("#") or parts[0] not in prompts:
            continue
        kind = parts[1]
        size = parts[2] if len(parts) > 2 else None
        items.append({**prompts[parts[0]], "kind": kind, "size": size})
    return items, {"sample_sha256": sha(sample), "labels_sha256": sha(labels_path)}


def bench_work(cfg: Any, model: LocalModel, embed: Any) -> dict[str, Any]:
    dev, dev_hash = fixture("work_question_dev.json")
    held, held_hash = fixture("work_question_held_out.json")
    assert [(i["prompt"], i["label"] == "work") for i in dev["items"]] == list(WORK_EXAMPLES)
    real, real_hashes = real_prompts()
    real = [item for item in real if item["kind"] in {"W", "Q"}]
    sets = {
        "dev": ([i["prompt"] for i in dev["items"]], [i["label"] for i in dev["items"]]),
        "held_out": ([i["prompt"] for i in held["items"]], [i["label"] for i in held["items"]]),
        "real": ([i["prompt"] for i in real],
                 ["work" if i["kind"] == "W" else "question" for i in real]),
    }
    centroids = Centroids(embed, WORK_QUESTION.examples)
    embed_chain = BinaryClassifier(
        WORK_QUESTION, lambda t: True, centroids=centroids
    )
    out: dict[str, Any] = {
        "hashes": {"dev": dev_hash, "held_out": held_hash, **real_hashes},
        "sets": {},
    }
    embed_timer = Timer()
    all_model_timer = Timer()
    thresholds = (0.3, 0.4, 0.5, 0.6, 0.7)
    for name, (texts, labels) in sets.items():
        rule = ["work" if is_task(t) else "question" for t in texts]
        embedded = [
            "work" if embed_timer(lambda t=t: is_task(t, embed_chain)) else "question"
            for t in texts
        ]
        probs: list[float | None] = []
        for text in texts:
            # Gated exactly as the hook gates: notices and short prompts never reach it.
            if is_task(text, lambda _t: _Always()):
                probs.append(all_model_timer(
                    lambda t=text: model.p_a(WORK_QUESTION, task_text(t))))
            else:
                probs.append(None)
        by_threshold = {
            str(th): ["work" if p is not None and p >= th else "question" for p in probs]
            for th in thresholds
        }
        entry = {
            "rule": score(labels, rule),
            "embedding": score(labels, embedded),
            "model_by_threshold": {th: score(labels, preds)["accuracy"]
                                   for th, preds in by_threshold.items()},
            "model@0.5": score(labels, by_threshold["0.5"]),
            "model@0.5_vs_rule": compare(labels, by_threshold["0.5"], rule),
            "embedding_vs_rule": compare(labels, embedded, rule),
        }
        if name != "real":
            entry["rule_errors"] = [t for t, want, got in zip(texts, labels, rule, strict=True)
                                    if want != got]
            entry["model_errors"] = [t for t, want, got in zip(texts, labels, by_threshold["0.5"],
                                                               strict=True) if want != got]
        else:
            # Real prompts: ids only, and only into data/.
            ids = [i["id"] for i in real]
            local = {"rule_wrong": [i for i, want, got in zip(ids, labels, rule, strict=True)
                                    if want != got],
                     "model_wrong": [i for i, want, got in zip(ids, labels, by_threshold["0.5"],
                                                               strict=True) if want != got],
                     "p_work": dict(zip(ids, probs, strict=True))}
            (LABELS / "work_question_real_errors.json").write_text(
                json.dumps(local, indent=1) + "\n", encoding="utf-8")
        out["sets"][name] = entry
    out["latency"] = {
        "model_warm": all_model_timer.summary(),
        "model_cold_ms": cold_call(lambda: model.p_a(WORK_QUESTION, "add a footer")),
        "embedding_in_process": embed_timer.summary(),
        "hook_process": hook_process_latency(),
    }
    return out


class _Always:
    is_a = True


def hook_process_latency() -> dict[str, Any]:
    """A fresh Python process, as a hook runs: import, build the chain, classify once."""
    script = (
        "import time,sys;t=time.perf_counter();"
        "from varyz.config import load_config;from varyz.hooks import is_task;"
        "from varyz.resolution.prompt_kind import prompt_classifier_from_config;"
        "cfg=load_config(None);cfg.workflows.prompt_classifier=sys.argv[1];"
        "c=prompt_classifier_from_config(cfg);"
        "r=is_task('check whether the nightly backup finished and tell me',c);"
        "print(round((time.perf_counter()-t)*1000,1), getattr(c,'skipped',None))"
    )
    result: dict[str, Any] = {}
    for mode in ("rule", "model", "embedding"):
        runs = []
        for _ in range(5):
            started = time.perf_counter()
            done = subprocess.run(  # noqa: S603 - fixed local interpreter
                [sys.executable, "-c", script, mode], capture_output=True, text=True,
                cwd=ROOT, check=True,
            )
            runs.append((time.perf_counter() - started) * 1000)
            inner = float(done.stdout.split()[0])
        result[mode] = {"process_median_ms": round(statistics.median(runs), 1),
                        "classify_step_last_ms": inner}
    return result


# -- stop gate -------------------------------------------------------------------


def bench_stop_gate(cfg: Any, model: LocalModel, embed: Any) -> dict[str, Any]:
    real, hashes = real_prompts()
    sized = [item for item in real if item["kind"] == "W" and item["size"] in {"new", "small"}]
    labels = [item["size"] for item in sized]
    model_sizer = ModelSizer(MODEL)
    embedding_sizer = EmbeddingSizer(embed)
    timer = Timer()
    runs = {
        "lexicon": [lexicon_sizer(i["prompt"], ()).size for i in sized],
        "embedding": [embedding_sizer(i["prompt"], ()).size for i in sized],
        "model_chain": [timer(lambda t=i["prompt"]: model_sizer(t, ()).size) for i in sized],
    }
    out: dict[str, Any] = {"hashes": hashes, "n_new": labels.count("new"),
                           "n_small": labels.count("small")}
    for name, preds in runs.items():
        pairs = list(zip(labels, preds, strict=True))
        out[name] = {
            **score(labels, preds),
            # The cost: a new task let through without its definition of done.
            "new_skipped": sum(want == "new" and got == "small" for want, got in pairs),
            # The gain: a small change not blocked.
            "small_skipped": sum(want == "small" and got == "small" for want, got in pairs),
        }
    out["model_vs_lexicon"] = compare(labels, runs["model_chain"], runs["lexicon"])
    out["latency_model_warm"] = timer.summary()
    ids = [i["id"] for i in sized]
    (LABELS / "stop_gate_real_errors.json").write_text(json.dumps({
        name: [i for i, want, got in zip(ids, labels, preds, strict=True) if want != got]
        for name, preds in runs.items()
    }, indent=1) + "\n", encoding="utf-8")
    return out


# -- redo ------------------------------------------------------------------------


def bench_redo(cfg: Any, model: LocalModel, embed: Any) -> dict[str, Any]:
    dev, dev_hash = fixture("redo_dev.json")
    held, held_hash = fixture("redo_held_out.json")
    assert [(i["task"], i["label"] == "redo") for i in dev["items"]] == list(REDO_EXAMPLES)
    workflows = {**dev["workflows"], **held["workflows"]}
    centroids = Centroids(embed, REDO_QUESTION.examples)
    out: dict[str, Any] = {"hashes": {"dev": dev_hash, "held_out": held_hash}, "sets": {}}
    timer = Timer()
    for name, data in (("dev", dev), ("held_out", held)):
        items = data["items"]
        labels = [i["label"] for i in items]
        rule = ["redo" if is_redo(i["task"], parse_steps(workflows[i["workflow"]])) else "not"
                for i in items]
        embedded = ["redo" if centroids.is_a(i["task"]) else "not" for i in items]
        probs = [timer(lambda t=i["task"]: model.p_a(REDO_QUESTION, t)) for i in items]
        modelled = ["redo" if p >= 0.5 else "not" for p in probs]
        out["sets"][name] = {
            "rule": score(labels, rule),
            "embedding" + (" (in-sample)" if name == "dev" else ""): score(labels, embedded),
            "model": score(labels, modelled),
            "model_vs_rule": compare(labels, modelled, rule),
            "rule_errors": [i["task"] for i, g in zip(items, rule, strict=True) if g != i["label"]],
            "model_errors": [i["task"] for i, g in zip(items, modelled, strict=True)
                             if g != i["label"]],
        }
    out["latency_model_warm"] = timer.summary()
    return out


# -- scope -----------------------------------------------------------------------


def bench_scope(cfg: Any, model: LocalModel, embed: Any) -> dict[str, Any]:
    bench, dev_hash = fixture("workflow_bench.json")
    held, held_hash = fixture("scope_held_out.json")
    out: dict[str, Any] = {"hashes": {"dev (workflow_bench.json)": dev_hash,
                                      "held_out": held_hash}, "sets": {}}
    timer = Timer()
    thresholds = (0.5, 0.6, 0.7, 0.8, 0.9)
    for name, items in (("dev", bench["scope_pairs"]), ("held_out", held["items"])):
        labels = [i["label"] for i in items]
        rule = ["general" if suggest_scope(i["q"], i["a"], "lumen").general else "project"
                for i in items]
        probs = [timer(lambda i=i: model.p_a(SCOPE_QUESTION, scope_exchange(i["q"], i["a"])))
                 for i in items]
        entry: dict[str, Any] = {"rule": {**score(labels, rule), "leaks": _leaks(labels, rule)}}
        for th in thresholds:
            # The shipped path: explicit wording first, then the model at this bar.
            shipped = [
                "general" if suggest_scope(i["q"], i["a"], "lumen", model=_Fixed(p), threshold=th)
                .general else "project"
                for i, p in zip(items, probs, strict=True)
            ]
            entry[f"chain@{th}"] = {**score(labels, shipped), "leaks": _leaks(labels, shipped),
                                    "vs_rule": compare(labels, shipped, rule)}
        entry["rule_errors"] = [f"{i['q']} | {i['a']} ({i['label']})"
                                for i, g in zip(items, rule, strict=True) if g != i["label"]]
        from varyz.resolution.scope import GENERAL_THRESHOLD
        shipped_th = GENERAL_THRESHOLD
        shipped = [
            "general" if suggest_scope(i["q"], i["a"], "lumen", model=_Fixed(p),
                                       threshold=shipped_th).general else "project"
            for i, p in zip(items, probs, strict=True)
        ]
        entry["chain_errors"] = [f"{i['q']} | {i['a']} ({i['label']})"
                                 for i, g in zip(items, shipped, strict=True) if g != i["label"]]
        out["sets"][name] = entry
    out["latency_model_warm"] = timer.summary()
    return out


def _leaks(labels: Sequence[str], preds: Sequence[str]) -> int:
    """A project choice stored as a general habit: the costly error."""
    return sum(want == "project" and got == "general"
               for want, got in zip(labels, preds, strict=True))


class _Fixed:
    """A stand-in model that answers a probability already computed."""

    def __init__(self, p: float) -> None:
        self.p = p

    def p_a(self, question: Any, text: str, *, few_shot: bool = True) -> float:
        return self.p


# -- ask paraphrase --------------------------------------------------------------


def bench_ask(cfg: Any, model: LocalModel, embed: Any, authorize: str | None) -> dict[str, Any]:
    import vision_bench as vb

    from varyz.resolution.paraphrase_verify import THRESHOLD, TOP_K, ParaphraseVerifier

    knowledge = vb._rows(vb.KNOWLEDGE)
    answers = {row["answer"]: row["id"] for row in knowledge}
    dev_sets = {
        "v1_dev": (vb.QUERIES, "dev"), "v1_held_out(burned)": (vb.QUERIES, "held_out"),
        "v2(burned)": (vb.QUERIES_V2, "held_out_v2"), "v3(burned)": (vb.QUERIES_V3, "held_out_v3"),
    }
    sets = dict(dev_sets)
    if authorize:
        vb._burn_held_out(authorize, "held_out_v4")
        sets["v4_held_out"] = (vb.QUERIES_V4, "held_out_v4")
    verifier = ParaphraseVerifier(model, embed, threshold=0.0)
    out: dict[str, Any] = {
        "hashes": {name: sha(path) for name, (path, _) in sets.items()}
        | {"knowledge": sha(vb.KNOWLEDGE)},
        "top_k": TOP_K, "shipped_threshold": THRESHOLD, "sets": {},
    }
    strict_timer, verify_timer = Timer(), Timer()
    thresholds = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98)
    for name, (path, split) in sets.items():
        cases = [row for row in vb._rows(path) if row["split"] == split]
        with tempfile.TemporaryDirectory(prefix="varyz-rollout-ask-") as directory:
            harness = vb.Harness(Path(directory), verifier)
            ids = {}
            try:
                ids = harness.seed_knowledge(knowledge)
                rows = []
                for case in cases:
                    request = harness.request(case["question"])
                    harness.knowledge._verifier = None
                    strict = strict_timer(
                        lambda r=request, store=harness.knowledge: store.try_resolve(r)
                    )
                    harness.knowledge._verifier = verifier
                    verifier.last = []
                    started = time.perf_counter()
                    harness.knowledge.try_resolve(request)
                    if verifier.last:
                        verify_timer.samples.append((time.perf_counter() - started) * 1000)
                    rows.append({
                        "id": case["id"], "expect": case["expect"],
                        "strict": vb._answer_to_id(strict, answers),
                        "candidates": [(ids.get(key, key), round(p, 4))
                                       for key, p in verifier.last],
                    })
            finally:
                harness.close()
        entry: dict[str, Any] = {"cases": len(rows),
                                 "positives": sum(r["expect"] is not None for r in rows)}
        entry["strict"] = _ask_score(rows, lambda r: r["strict"])
        for th in thresholds:
            entry[f"verified@{th}"] = _ask_score(rows, lambda r, th=th: _pick(r, th))
        entry["verified_shipped_errors"] = [
            {"id": r["id"], "expect": r["expect"], "got": _pick(r, THRESHOLD),
             "candidates": r["candidates"]}
            for r in rows if _pick(r, THRESHOLD) != r["expect"] and _pick(r, THRESHOLD) is not None
        ]
        entry["verified_shipped_misses"] = [
            {"id": r["id"], "expect": r["expect"], "candidates": r["candidates"]}
            for r in rows if r["expect"] is not None and _pick(r, THRESHOLD) is None
        ]
        out["sets"][name] = entry
    out["latency"] = {
        "strict_only": strict_timer.summary(),
        "with_verifier_when_it_runs": verify_timer.summary(),
        "verifier_cold_ms": _ask_cold(model, embed, knowledge),
    }
    return out


def _pick(row: dict[str, Any], threshold: float) -> str | None:
    if row["strict"] is not None:
        return str(row["strict"])
    yes = [key for key, p in row["candidates"] if p >= threshold]
    return yes[0] if len(yes) == 1 else None


def _ask_score(
    rows: Sequence[dict[str, Any]], got: Callable[[dict[str, Any]], Any]
) -> dict[str, Any]:
    positives = [r for r in rows if r["expect"] is not None]
    negatives = [r for r in rows if r["expect"] is None]
    correct_pos = sum(got(r) == r["expect"] for r in positives)
    return {
        "recall": round(correct_pos / len(positives), 4) if positives else None,
        "recall_ci95": wilson(correct_pos, len(positives)),
        "answered": correct_pos,
        "wrong_on_positive": sum(got(r) is not None and got(r) != r["expect"] for r in positives),
        "false_match_on_negative": sum(got(r) is not None for r in negatives),
        "wrong_answers": sum(got(r) is not None and got(r) != r["expect"] for r in rows),
    }


def _ask_cold(model: LocalModel, embed: Any, knowledge: list[dict[str, Any]]) -> float:
    from varyz.resolution.paraphrase_verify import ParaphraseVerifier

    verifier = ParaphraseVerifier(model, embed)
    candidates = [(row["id"], row["question"], row["answer"]) for row in knowledge]
    verifier.rank("warm the embedding", candidates)
    return cold_call(lambda: verifier.pick("Which Python installer do we use?", candidates))


# -- main ------------------------------------------------------------------------


SECTIONS = ("work", "stop_gate", "redo", "scope", "ask")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--sections", default=",".join(SECTIONS))
    parser.add_argument("--authorize-held-out", metavar="LABEL",
                        help="score the v4 ask held-out set once (recorded in the ledger)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = load_config(None)
    model = local_model_from_config(cfg)
    model.timeout_seconds = 120.0
    model.ready()
    embed = embedder_from_config(cfg)
    args.out.mkdir(parents=True, exist_ok=True)
    for section in (part.strip() for part in args.sections.split(",")):
        print(f"running {section} ...", file=sys.stderr, flush=True)
        started = time.perf_counter()
        if section == "ask":
            result = bench_ask(cfg, model, embed, args.authorize_held_out)
        else:
            result = {"work": bench_work, "stop_gate": bench_stop_gate, "redo": bench_redo,
                      "scope": bench_scope}[section](cfg, model, embed)
        result["model"] = MODEL
        result["date"] = time.strftime("%Y-%m-%d")
        result["section_seconds"] = round(time.perf_counter() - started, 1)
        (args.out / f"{section}.json").write_text(
            json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(_brief(result), indent=1), flush=True)
    return 0


def _brief(result: dict[str, Any]) -> Any:
    if isinstance(result, dict):
        return {k: _brief(v) for k, v in result.items()
                if not k.endswith("errors") and not k.endswith("misses") and k != "hashes"}
    return result


if __name__ == "__main__":
    raise SystemExit(main())
