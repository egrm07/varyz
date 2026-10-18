"""Verify plain resume against the actual current chat's recorded state."""
import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from varyz import __file__ as runtime_file
from varyz.mcp_server import VaryzServer

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("arm", choices=("baseline", "candidate", "staged", "installed"))
parser.add_argument("--round", default="round13")
args_cli = parser.parse_args()
if not args_cli.round.isalnum():
    raise ValueError("Use a simple private round name")
folder = root / "data/promise-archive-eval" / args_cli.round
output = folder / f"{args_cli.arm}.json"
if output.exists():
    raise FileExistsError("Never overwrite a recorded resume result")
truth = json.loads((folder / "truth.json").read_text(encoding="utf-8"))
source = json.loads((folder / "resume-source.json").read_text(encoding="utf-8"))[
    "result"]["structuredContent"]
assert source["metadata"]["authored"] and source["metadata"]["project"] == "varyz"
prompt = source["text"].split("\n\n", 1)[1]
settings = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
connection = settings["mcp_servers"]["varyz"]
args = connection.get("args", [])
path = Path(args[args.index("--config") + 1]) if "--config" in args else None
server = VaryzServer(path, agent_token=connection["env"]["VARYZ_AGENT_TOKEN"])
results = []
try:
    for task in ("resume", prompt):
        result = server.call("start_task", {"task": task, "project": "varyz",
            "conversation_id": truth["conversation_id"]})
        state = result.get("continuation", {}).get("state", {})
        results.append({"source_matches": state.get("reference") == truth["reference"],
                        "chat_matches": state.get("conversation_id") == truth["conversation_id"],
                        "raw": result})
finally:
    server.close()
runtime = Path(runtime_file).resolve().parent
out = {"truth_sha256": hashlib.sha256((folder / "truth.json").read_bytes()).hexdigest(),
       "source_sha256": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                         for name in ("mcp_server.py", "resolution/continuation.py")},
       "results": results}
output.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps({"arm": args_cli.arm, "n": len(results),
                  "correct_chat_and_source": sum(r["chat_matches"] and r["source_matches"]
                                                  for r in results)}))
