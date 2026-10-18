"""Replay approved real decision cases with the existing registered local identity.

Credentials stay in memory; no credential, source excerpt or answer is printed.
Private raw outputs belong under the existing gitignored evaluation directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import tomllib
from pathlib import Path

from varyz import __file__ as runtime_file
from varyz.config import load_config
from varyz.mcp_server import VaryzServer
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon


def run(dataset: Path, output: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    private = (root / "data/promise-archive-eval").resolve()
    if not output.resolve().is_relative_to(private):
        raise ValueError("Raw replay output must remain in the private evaluation directory")
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    server_config = config["mcp_servers"]["varyz"]
    arguments = server_config.get("args", [])
    path = None
    if "--config" in arguments:
        path = Path(arguments[arguments.index("--config") + 1])
    token = server_config["env"]["VARYZ_AGENT_TOKEN"]
    cfg = load_config(path)
    os.environ.pop("CODEX_THREAD_ID", None)
    data = json.loads(dataset.read_text(encoding="utf-8"))
    runtime_root = Path(runtime_file).resolve().parent
    fingerprint = {
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "source_sha256": {
            name: (hashlib.sha256((runtime_root / name).read_bytes()).hexdigest()
                   if (runtime_root / name).exists() else None)
            for name in (
                "resolution/knowledge.py",
                "resolution/continuation.py",
                "resolution/briefing_budget.py",
                "resolution/paraphrase_verify.py",
                "mcp_server.py",
            )
        },
        "paraphrase_verifier": cfg.workflows.paraphrase_verifier,
        "classifier_model": cfg.workflows.classifier_model,
        "classifier_transport": "configured_local_model",
    }
    results = []
    diagnostics = []
    latencies = []
    # Same real archive and registered identity, isolated daemon transport.
    # Normal resolution audit writes are allowed; no knowledge or grants are created.
    with build_resolution_daemon(cfg.db_path, config=cfg, host="127.0.0.1", port=0) as daemon:
        host, port = daemon.address
        server = VaryzServer(
            path,
            agent_token=token,
            resolution_client=ResolutionClient(host, port, archive=cfg.db_path),
        )
        try:
            for case in data["cases"]:
                for query in case["queries"]:
                    started = time.perf_counter()
                    value = server.call(
                        "ask",
                        {
                            "question": query,
                            "project": "varyz",
                            "conversation_id": "archive-replay-r2",
                            "allow_escalation": False,
                        },
                    )
                    results.append(value)
                    latencies.append(time.perf_counter() - started)
                    verifier = daemon.service.knowledge._verifier
                    diagnostics.append([
                        (key, p, hashlib.sha256(
                            daemon.service.knowledge.get(key).answer.encode()
                        ).hexdigest())
                        for key, p in verifier.last
                    ] if verifier else [])
            continuations = []
            for client in ("Codex", "Claude Code"):
                value = server.call(
                    "start_task",
                    {
                        "task": f"Continue the previous {client} Varyz session.",
                        "project": "varyz",
                        "conversation_id": "01a11de0-9014-7bc0-be1f-ec07594187d5",
                    },
                )
                continuations.append(value)
        finally:
            server.close()
    output.write_text(
        json.dumps({**fingerprint, "results": results, "diagnostics": diagnostics,
                    "latency_seconds": latencies,
                    "continuations": continuations}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    expected = [case["answer"] for case in data["cases"] for _ in case["queries"]]
    correct = sum(result.get("answer") == answer
                  for result, answer in zip(results, expected, strict=True))
    print(
        json.dumps(
            {
                "n": len(results),
                "exact_string_correct": correct,
                "raw_output": str(output),
                "credential_exposed": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.dataset, args.output)
