"""Read the existing approved Varyz project snapshot; no sync or grant changes."""

import argparse
import json
import tomllib
from pathlib import Path

from varyz.cloud.cli import _connected

parser = argparse.ArgumentParser()
parser.add_argument("tool", choices=("search", "fetch", "session_history"))
parser.add_argument("arguments", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
private = (Path(__file__).resolve().parents[1] / "data/promise-archive-eval").resolve()
if not args.output.resolve().is_relative_to(private):
    raise ValueError("Raw history must remain in the private evaluation directory")
arguments = json.loads(args.arguments.read_text(encoding="utf-8"))
if arguments.get("project") != "varyz":
    raise ValueError("Only the authorized Varyz project scope may be read")
if args.tool == "search" and arguments.get("limit", 4) > 4:
    raise ValueError("Use a bounded search")
connection = tomllib.loads((Path.home() / ".codex/config.toml").read_text(
    encoding="utf-8"))["mcp_servers"]["varyz"]
cli_args = connection.get("args", [])
config = Path(cli_args[cli_args.index("--config") + 1]) if "--config" in cli_args else None
with _connected(config) as (_, _, client):
    result = client.request("POST", "/mcp/archive", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": args.tool, "arguments": arguments},
    })
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({"output": str(args.output), "tool": args.tool,
                  "rpc_error": "error" in result,
                  "tool_error": result.get("result", {}).get("isError", False)}))
