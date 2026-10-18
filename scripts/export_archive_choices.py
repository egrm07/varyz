"""Bounded approved owner-knowledge export for local private evaluation.

Reuses existing registered identity and its owner_knowledge capability. Never
exports raw archive entries or withheld evidence, and never changes a grant.
"""

import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from varyz.mcp_server import VaryzServer
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.resolution.knowledge import SQLiteKnowledgeStore, owner_knowledge_denial
from varyz.scopes import normalize_scope
from varyz.trust import capability_limits

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--round", default="round4")
parser.add_argument("--exclude", action="append", type=Path, default=[])
args_cli = parser.parse_args()
if not args_cli.round.isalnum():
    raise ValueError("Round must be a simple directory name")
settings = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
connection = settings["mcp_servers"]["varyz"]
args = connection.get("args", [])
path = Path(args[args.index("--config") + 1]) if "--config" in args else None
with_server = VaryzServer(path, agent_token=connection["env"]["VARYZ_AGENT_TOKEN"])
try:
    granted = with_server._briefing_caller_limit({"purpose": "owner_context"})
    if isinstance(granted, dict):
        raise PermissionError("Existing caller does not cover owner knowledge")
    caller, _ = granted
    if owner_knowledge_denial(with_server.conn, caller):
        raise PermissionError("Existing caller does not cover owner knowledge")
    max_items, max_chars = capability_limits(with_server.conn, caller)
    existing = json.loads((root / "data/promise-archive-eval/round2/briefing.json").read_text(
        encoding="utf-8"))
    seen = {x["question"] for x in existing["project_decisions"]}
    for previous in args_cli.exclude:
        seen.update(x["question"] for x in json.loads(previous.read_text(
            encoding="utf-8"))["records"])
    gate = SensitivityGate(with_server.cfg.sensitivity)
    candidates = []
    for entry in SQLiteKnowledgeStore(with_server.conn).current():
        if normalize_scope(entry.scope) != "varyz" or entry.origin != "own":
            continue
        if entry.question in seen or entry.author_id != "owner:local":
            continue
        if str(gate.label("ai_conversations", entry.question, entry.answer)) not in {
            "personal", "public",
        }:
            continue
        value = {"id": entry.id, "question": entry.question, "answer": entry.answer,
                 "source": entry.source, "date": entry.stated_at.isoformat(),
                 "provenance": dict(entry.provenance)}
        candidates.append(value)
    # Stable order; unknown facts are not selected according to candidate results.
    candidates.sort(key=lambda x: hashlib.sha256(x["id"].encode()).hexdigest())
    used = 0
    selected = []
    for value in candidates:
        size = len(json.dumps(value, ensure_ascii=False))
        if len(selected) >= min(12, max_items) or used + size > max_chars:
            continue
        selected.append(value)
        used += size
    target = root / "data/promise-archive-eval" / args_cli.round / "approved_choices.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"records": selected, "max_chars": max_chars,
                                 "used_chars": used, "max_items": max_items}, indent=2),
                      encoding="utf-8")
    print(json.dumps({"selected": len(selected), "used_chars": used,
                      "existing_capability_limit": max_chars}))
finally:
    with_server.close()
