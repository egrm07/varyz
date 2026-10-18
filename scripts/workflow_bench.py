"""Measure the owner workflow offline: scope, kinds of work, interruptions.

    .venv/Scripts/python.exe scripts/workflow_bench.py [--out reports/x.json]

Everything runs in a temporary archive with a simulated owner — no live data, no
network, no model. The labels in scripts/fixtures/workflow_bench.json were
written before the first run and are not tuned to the results.

Metrics:

* scope default accuracy — when the owner does not say whether an answer is a
  general preference or a one-off choice, how often varyz's guess matches the
  label; a project choice guessed general is a *leak* and counted separately;
* kind-of-work detection — recall and exact match of the domains a task names;
* owner interruptions — a simulated owner answers the same questions across
  three projects: how many times were they asked, vs asking every time, and did
  any one-off answer reach another project (must be 0).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "fixtures" / "workflow_bench.json"

from varyz.mcp_server import VaryzServer  # noqa: E402
from varyz.resolution.client import ResolutionClient  # noqa: E402
from varyz.resolution.daemon import build_resolution_daemon  # noqa: E402
from varyz.resolution.domains import detect_domains  # noqa: E402
from varyz.resolution.escalation import NullNotifier  # noqa: E402
from varyz.resolution.models import Caller  # noqa: E402
from varyz.resolution.scope import suggest_scope  # noqa: E402

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:bench",
    transport="stdio",
)


def scope_accuracy(pairs: list[dict[str, str]]) -> dict[str, Any]:
    correct = leaks = over_narrow = 0
    misses = []
    for pair in pairs:
        guess = "general" if suggest_scope(pair["q"], pair["a"], "lumen").general else "project"
        if guess == pair["label"]:
            correct += 1
            continue
        if guess == "general":
            leaks += 1
        else:
            over_narrow += 1
        misses.append({"q": pair["q"], "a": pair["a"], "label": pair["label"], "guess": guess})
    return {
        "n": len(pairs),
        "accuracy": round(correct / len(pairs), 3),
        "leaks_project_guessed_general": leaks,
        "over_narrow_general_guessed_project": over_narrow,
        "misses": misses,
    }


def domain_detection(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    exact = hit = wanted = extra = 0
    misses = []
    for item in tasks:
        found = set(detect_domains(item["task"]))
        expected = set(item["domains"])
        exact += found == expected
        hit += len(found & expected)
        wanted += len(expected)
        extra += len(found - expected)
        if found != expected:
            misses.append({"task": item["task"], "expected": sorted(expected),
                           "found": sorted(found)})
    return {
        "n": len(tasks),
        "exact_match": round(exact / len(tasks), 3),
        "recall": round(hit / wanted, 3),
        "extra_domains": extra,
        "misses": misses,
    }


def owner_simulation(sim: dict[str, Any], *, owner_picks_scope: bool) -> dict[str, Any]:
    # Windows can hold the database file a moment after the daemon stops.
    with tempfile.TemporaryDirectory(prefix="varyz-bench-", ignore_cleanup_errors=True) as folder:
        root = Path(folder)
        config = root / "config.toml"
        config.write_text(
            f'[storage]\ndata_dir = "{(root / "data").as_posix()}"\n'
            "[resolution]\nautostart = false\n",
            encoding="utf-8",
        )
        daemon = build_resolution_daemon(
            root / "data" / "varyz.db", host="127.0.0.1", port=0,
            device_id="device:bench", notifier=NullNotifier(),
        )
        with daemon:
            host, port = daemon.address
            server = VaryzServer(config, agent_token="",
                                 resolution_client=ResolutionClient(host, port))
            try:
                return _simulate(server, sim, owner_picks_scope=owner_picks_scope)
            finally:
                server.close()
                daemon.stop()


def _simulate(server: VaryzServer, sim: dict[str, Any], *, owner_picks_scope: bool) -> dict:
    project_answers: dict[str, str] = {}  # answer text -> the project it was given for
    interruptions: dict[str, int] = {}
    leaks = []
    wrong_answers = 0
    for project in sim["projects"]:
        asked = 0
        for item in sim["questions"]:
            result = server.call("ask", {"question": item["q"], "project": project})
            if result.get("ask_owner"):
                asked += 1
                answer = item["a"]
                if item["scope"] == "project":
                    answer = f"{item['a']} [{project}]"
                arguments = {"escalation_id": result["ask_owner"]["escalation_id"],
                             "answer": answer}
                if owner_picks_scope:
                    arguments["scope"] = item["scope"]
                server.call("answer_owner_question", arguments)
                if item["scope"] == "project":
                    project_answers[answer] = project
                continue
            got = result.get("answer")
            if got in project_answers and project_answers[got] != project:
                leaks.append({"question": item["q"], "from": project_answers[got],
                              "to": project})
            elif got != item["a"] and item["scope"] == "general":
                wrong_answers += 1
        interruptions[project] = asked
    paraphrased = 0
    for paraphrase in sim.get("paraphrases", {}).values():
        result = server.call("ask", {"question": paraphrase, "project": "newproject"})
        paraphrased += bool(result.get("answer")) and not result.get("ask_owner")
    questions = len(sim["questions"])
    total = sum(interruptions.values())
    general = sum(1 for item in sim["questions"] if item["scope"] == "general")
    return {
        "owner_picks_scope": owner_picks_scope,
        "interruptions_per_project": interruptions,
        "interruptions_total": total,
        "ask_every_time_baseline": questions * len(sim["projects"]),
        "ideal": questions + (questions - general) * (len(sim["projects"]) - 1),
        "cross_project_leaks": len(leaks),
        "leaks": leaks,
        "wrong_answers": wrong_answers,
        "paraphrases_answered_without_asking": f"{paraphrased}/{len(sim.get('paraphrases', {}))}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path,
                        default=ROOT / "reports" / f"workflow_bench_{date.today()}.json")
    args = parser.parse_args(argv)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    started = time.perf_counter()

    result = {
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "scope_default": scope_accuracy(fixture["scope_pairs"]),
        "kind_of_work": domain_detection(fixture["domain_tasks"]),
        "owner_picks_scope": owner_simulation(fixture["owner_sim"], owner_picks_scope=True),
        "varyz_default_scope": owner_simulation(fixture["owner_sim"], owner_picks_scope=False),
    }
    result["seconds"] = round(time.perf_counter() - started, 2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")

    scope = result["scope_default"]
    kinds = result["kind_of_work"]
    print(f"scope default   accuracy {scope['accuracy']:.0%} of {scope['n']}  "
          f"leaks {scope['leaks_project_guessed_general']}  "
          f"over-narrow {scope['over_narrow_general_guessed_project']}")
    print(f"kind of work    exact {kinds['exact_match']:.0%}  recall {kinds['recall']:.0%}  "
          f"extra {kinds['extra_domains']}")
    for key in ("owner_picks_scope", "varyz_default_scope"):
        sim = result[key]
        print(f"{key:<20}interruptions {sim['interruptions_total']} "
              f"(ask-every-time {sim['ask_every_time_baseline']}, ideal {sim['ideal']}) "
              f"per project {sim['interruptions_per_project']}  "
              f"leaks {sim['cross_project_leaks']}  wrong {sim['wrong_answers']}  "
              f"paraphrases {sim['paraphrases_answered_without_asking']}")
    print(f"wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    leaked = result["owner_picks_scope"]["cross_project_leaks"]
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
