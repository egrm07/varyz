"""advice_bench: does an agent get the owner's rules before it builds?

Varyz advises coding agents: given a plan, `advise` returns the owner's
confirmed rules and project vision that bear on it. This benchmark measures the
two halves of that, offline and reproducibly.

fixture  A synthetic owner (`evals/advice_bench_v1/fixture.json`): global rules,
         two fictional projects with their own rules and vision, and plans that
         would break specific rules if built naively. The fixture was written
         before the benchmark first ran, and the ranker and miner are not tuned
         against it -- change it only with a new version directory.

  recall@k       share of expected rules in the first k returned
  all@k          plans whose expected rules all came back in the first k
  MRR            reciprocal rank of the first expected rule
  chance@k       the same recall for a random order of the in-scope rules
  leaks          plans that received another project's rule
  empty          an owner with no rules gets `known: false`, not filler

  miner          precision and recall of `rules_in_text` on labelled owner
                 turns (rule / not a rule)

live     Replays the owner's own archive, read-only, and prints counts only --
         no rule, turn or plan text ever reaches the output. Every rule the
         miner finds in an authored turn is loaded, unreviewed, into a
         throwaway in-memory store; then the owner's *preceding* turn in the
         same conversation is sent to `advise` as the plan. A hit means the
         rule the owner later had to state would have been in front of the
         agent before it worked. Compared with chance (k over the rules in
         scope). Mined candidates are noisy, so this is a floor, not a ceiling.

Usage:
    uv run --no-sync python scripts/advice_bench.py fixture
    uv run --no-sync python scripts/advice_bench.py live --config path/to/config.toml
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.db.connection import open_db  # noqa: E402
from varyz.resolution.advice import NOTHING_RECORDED, advise  # noqa: E402
from varyz.resolution.models import Caller  # noqa: E402
from varyz.resolution.principles import GLOBAL_SCOPE, SQLitePrincipleStore  # noqa: E402
from varyz.resolution.rule_miner import (  # noqa: E402
    MiningStats,
    _turn_scope,
    mine_turns,
    rules_in_text,
    select,
)

FIXTURE = ROOT / "evals" / "advice_bench_v1" / "fixture.json"
KS = (1, 3, 8)
DEVICE = "device:bench"
BENCH = Caller(
    agent_id="owner:bench",
    display_name="advice_bench",
    purpose="owner_knowledge",
    device_id=DEVICE,
    transport="stdio",
)


def _memory_store() -> tuple[sqlite3.Connection, SQLitePrincipleStore]:
    conn = open_db(Path(":memory:"))
    return conn, SQLitePrincipleStore(conn)


def _in_scope(scopes: dict[str, str], project: str | None) -> int:
    return sum(1 for scope in scopes.values() if scope in (GLOBAL_SCOPE, project))


def score_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    conn, store = _memory_store()
    ids: dict[str, str] = {}
    scopes: dict[str, str] = {}
    for rule in fixture["rules"]:
        principle_id = store.add(
            statement=rule["statement"], caller=BENCH, device_id=DEVICE,
            source="bench", kind=rule["kind"], scope=rule["scope"],
        )
        ids[principle_id] = rule["id"]
        scopes[rule["id"]] = rule["scope"]
    recall = {k: [] for k in KS}
    every = {k: 0 for k in KS}
    chance = {k: [] for k in KS}
    reciprocal: list[float] = []
    leaks = 0
    misses: list[str] = []
    for plan in fixture["plans"]:
        result = advise(conn, plan["plan"], project=plan["project"], limit=max(KS))
        ranked = [ids[item["id"]] for item in result["project_vision"] + result["owner_rules"]]
        expected = set(plan["expect"])
        if any(scopes[rule] not in (GLOBAL_SCOPE, plan["project"]) for rule in ranked):
            leaks += 1
        n = _in_scope(scopes, plan["project"])
        for k in KS:
            found = expected & set(ranked[:k])
            recall[k].append(len(found) / len(expected))
            every[k] += found == expected
            chance[k].append(min(k, n) / n)
        first = next((i for i, rule in enumerate(ranked, 1) if rule in expected), None)
        reciprocal.append(1 / first if first else 0.0)
        if not expected <= set(ranked[:3]):
            misses.append(plan["id"])
    conn.close()

    empty_conn, _ = _memory_store()
    empty = advise(empty_conn, "Add a settings page", project="tern")
    empty_ok = not empty["known"] and empty["how_to_use"] == NOTHING_RECORDED
    empty_conn.close()

    labels = fixture["turns"]
    predicted = [any(True for _ in rules_in_text(t["text"], correction_turn=True)) for t in labels]
    tp = sum(p and t["rule"] for p, t in zip(predicted, labels, strict=True))
    fp = sum(p and not t["rule"] for p, t in zip(predicted, labels, strict=True))
    fn = sum(not p and t["rule"] for p, t in zip(predicted, labels, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    miner_recall = tp / (tp + fn) if tp + fn else 0.0
    plans = len(fixture["plans"])
    return {
        "plans": plans,
        "rules": len(fixture["rules"]),
        **{f"recall@{k}": round(statistics.mean(recall[k]), 3) for k in KS},
        **{f"all@{k}": f"{every[k]}/{plans}" for k in KS},
        **{f"chance@{k}": round(statistics.mean(chance[k]), 3) for k in KS},
        "MRR": round(statistics.mean(reciprocal), 3),
        "leaks": leaks,
        "empty_result_correct": empty_ok,
        "missed_at_3": misses,
        "miner_labelled_turns": len(labels),
        "miner_precision": round(precision, 3),
        "miner_recall": round(miner_recall, 3),
        "miner_f1": round(2 * precision * miner_recall / (precision + miner_recall), 3)
        if precision + miner_recall else 0.0,
    }


def score_live(config_path: Path, k: int) -> dict[str, Any]:
    from varyz.config import load_config

    db_path = load_config(config_path).db_path
    live = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    live.row_factory = sqlite3.Row
    stats = MiningStats()
    try:
        found = list(mine_turns(live, stats))
        chosen = select(found, stats, cap=10**9)
        conn, store = _memory_store()
        loaded: list[tuple[str, Any]] = []
        for candidate in chosen:
            principle_id = store.add(
                statement=candidate.statement, caller=BENCH, device_id=DEVICE,
                source=candidate.source, scope=candidate.scope,
            )
            loaded.append((principle_id, candidate))
        scopes = [candidate.scope for _, candidate in loaded]
        hits: list[int] = []
        chance: list[float] = []
        no_prior = 0
        by_scope = {"project": [0, 0], "global": [0, 0]}
        for principle_id, candidate in loaded:
            prior = live.execute(
                """SELECT body FROM entries
                   WHERE source = 'ai_conversations' AND authored_by_user = 1
                     AND sensitivity != 'sensitive' AND thread_id = ?
                     AND occurred_start < ? AND id != ?
                   ORDER BY occurred_start DESC LIMIT 1""",
                (candidate.provenance["thread_id"], candidate.provenance["occurred"],
                 candidate.provenance["entry_id"]),
            ).fetchone()
            if prior is None:
                no_prior += 1
                continue
            scope, text = _turn_scope(str(prior["body"]))
            project = None if scope == GLOBAL_SCOPE else scope
            if not " ".join(text.split()):
                no_prior += 1
                continue
            returned, _ = store.applicable(text, project=project, limit=k)
            hit = int(any(item.principle.id == principle_id for item in returned))
            hits.append(hit)
            n = sum(1 for s in scopes if s in (GLOBAL_SCOPE, project))
            chance.append(min(k, n) / n)
            bucket = by_scope["global" if candidate.scope == GLOBAL_SCOPE else "project"]
            bucket[0] += hit
            bucket[1] += 1
        conn.close()
    finally:
        live.close()
    evaluated = len(hits)
    return {
        "mining": stats.as_dict(),
        "rules_loaded": len(loaded),
        "global_rules": sum(1 for s in scopes if s == GLOBAL_SCOPE),
        "project_scopes": len({s for s in scopes if s != GLOBAL_SCOPE}),
        "replayed": evaluated,
        "no_preceding_turn": no_prior,
        f"hit@{k}": round(sum(hits) / evaluated, 3) if evaluated else None,
        f"chance@{k}": round(statistics.mean(chance), 3) if chance else None,
        "lift": round((sum(hits) / evaluated) / statistics.mean(chance), 2)
        if evaluated and statistics.mean(chance) else None,
        f"hit@{k}_project_rules": f"{by_scope['project'][0]}/{by_scope['project'][1]}",
        f"hit@{k}_global_rules": f"{by_scope['global'][0]}/{by_scope['global'][1]}",
    }


def _print(result: dict[str, Any]) -> None:
    width = max(len(key) for key in result)
    for key, value in result.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for inner, count in value.items():
                print(f"  {inner:<{width}}  {count}")
        else:
            print(f"{key:<{width}}  {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("fixture", help="score the synthetic owner")
    fixture.add_argument("--json", action="store_true")
    live = commands.add_parser("live", help="replay the owner's archive, counts only")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--k", type=int, default=8)
    live.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "fixture":
        result = score_fixture(json.loads(FIXTURE.read_text(encoding="utf-8")))
    else:
        result = score_live(args.config, args.k)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
