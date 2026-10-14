"""Read four already-approved real messages in an isolated local profile reader."""
import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution import profile_reader
from varyz.resolution.profile_reader import (
    LOCAL_REPLY_TOKENS,
    PROFILE_VERSION,
    SCHEMA,
    ProfileState,
    accept,
    next_task,
    parse_items,
)
from varyz.resolution.rule_reader import reader_from_config

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--round", default="round14")
parser.add_argument("--suite", choices=("development", "fresh", "replication"),
                    default="development")
args_cli = parser.parse_args()
if not args_cli.round.isalnum():
    raise ValueError("Use a simple private round name")
folder = root / "data/promise-archive-eval" / args_cli.round
folder.mkdir(parents=True, exist_ok=True)
output = folder / "reader.json"
if output.exists():
    raise FileExistsError("Keep the first real-reader result")
source_round = "round17" if args_cli.suite == "fresh" else "round10"
names = ("brand", "security", "declined", "publication") if args_cli.suite == "fresh" else (
    "oneoff", "latency", "handoff", "away")
sources = [root / f"data/promise-archive-eval/{source_round}/{name}-source.json"
           for name in names]
labels = {"oneoff": "Learn the general correction: do not offer to save task-specific choices.",
          "latency": "Task-specific test request; do not infer an unconditional standing rule.",
          "handoff": "Do not infer a global standing preference from this isolated complaint.",
          "away": "Implementation request: no away activation or standing away instruction."}
if args_cli.suite == "fresh":
    labels = {
        "brand": "Retain explicit Varyz Cloud brand choice as a project decision, "
                 "not all projects.",
        "security": "Defer upload approval prompts until other useful authorized work is done; "
                    "do not suppress approval or authorize uploads.",
        "declined": "Don't save means do not learn the quoted General-first grouping preference.",
        "publication": "Question about testing without publication; not permission to publish "
                       "or an unconditional ban on publishing.",
    }
if args_cli.suite == "replication":
    sources = [root / f"data/promise-archive-eval/round24/{name}-source.json"
               for name in ("font", "plugin", "repair", "ongoing")]
    labels = {
        "font": "Retain hyphens instead of em dashes and keep Monetization.md internal; "
                "do not infer a standing font selection or publish the document.",
        "plugin": "Factual confirmation request only; do not infer permission to save rules "
                  "or a standing owner preference from the asserted plugin limitation.",
        "repair": "Varyz doctor repair should use the specified powerful model choices; "
                  "do not infer all agents must use these models or activate repair.",
        "ongoing": "Repair subagent continues after the main agent replies or works; "
                   "retain repair scope, no general authorization for background jobs.",
    }
(folder / "labels.json").write_text(json.dumps({"frozen_before_reader": True,
    "labels": labels, "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in sources}}, indent=2), encoding="utf-8")
connection = tomllib.loads((Path.home() / ".codex/config.toml").read_text(
    encoding="utf-8"))["mcp_servers"]["varyz"]
args = connection.get("args", [])
path = Path(args[args.index("--config") + 1]) if "--config" in args else None
cfg = load_config(path)
reader = reader_from_config(cfg, schema=SCHEMA, max_tokens=LOCAL_REPLY_TOKENS)
conn = open_db(folder / "isolated.sqlite")
try:
    for path in sources:
        source = json.loads(path.read_text(encoding="utf-8"))["result"]["structuredContent"]
        meta = source["metadata"]
        assert meta["authored"] and meta["project"] == "varyz"
        conn.execute("""INSERT INTO entries (id,kind,source,external_id,content_hash,
            body,occurred_start,ingested_at,thread_id,sensitivity,authored_by_user,
            conversation_metadata_json) VALUES (?,'message','ai_conversations',?,?,?,?,?,
            ?,'personal',1,'{}')""", (source["id"], source["id"],
            hashlib.sha256(source["text"].encode()).hexdigest(), source["text"],
            meta["occurred_at"], meta["occurred_at"], meta["thread_id"]))
    conn.commit()
    state = ProfileState(folder / "isolated-profile.json")
    task = next_task(state, conn, tier="local", reader="local")
    assert task and task.kind == "read"
    reply = reader.complete(task.instructions, task.text)
    items = parse_items(reply)
    accepted = accept(state, conn, task.id, items, tier="local")
    result = {"profile_version": PROFILE_VERSION, "model": cfg.workflows.classifier_model,
              "reader_sha256": hashlib.sha256(Path(profile_reader.__file__)
                                               .read_bytes()).hexdigest(),
              "instructions_sha256": hashlib.sha256(task.instructions.encode()).hexdigest(),
              "model_metrics": getattr(reader, "metrics", []),
              "read_prompt_chars": len(task.instructions), "task_chars": len(task.text),
              "raw_reply": reply, "notes": state.notes, "accepted": accepted.notes,
              "dropped": accepted.dropped, "live_ledger_changed": False}
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"messages": len(sources), "accepted_notes": accepted.notes,
                      "dropped": accepted.dropped, "live_ledger_changed": False}))
finally:
    conn.close()
