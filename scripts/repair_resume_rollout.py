"""Rebuild the MCP patch from its backup, retaining only the verified resume edit."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path

from rollout_product_promises import digest, installed

root = Path(__file__).resolve().parents[1]
folder = root / "data/promise-rollout/20261009T102137"
path = folder / "manifest.json"
value = json.loads(path.read_text(encoding="utf-8"))
target = installed()
row = next(r for r in value["files"] if r["file"] == "mcp_server.py")
record_path = Path(value["record"]["path"])
assert digest(target / row["file"]) == row["after"]
assert digest(folder / "backup" / row["file"]) == row["before"]
assert digest(record_path) == value["record"]["after"]
content = (folder / "backup" / row["file"]).read_bytes()
old = b"cfg=current_cfg, max_chars=min(2500, max_chars),"
assert content.count(old) == 1
content = content.replace(old, b"agent_id=caller.agent_id, " + old)
destination = target / row["file"]
temporary = destination.with_suffix(".resume-only-update")
temporary.write_bytes(content)
os.replace(temporary, destination)
row["attempted_after"] = row["after"]
row["after"] = digest(destination)
row["reconstructed_from_backup"] = True
# The staged snapshot must match the corrected manifest too.
for entry in value["files"]:
    staged = folder / "runtime/varyz" / entry["file"]
    staged.write_bytes((target / entry["file"]).read_bytes())
records = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))))
for record in records:
    if record[0] == "varyz/mcp_server.py":
        record[1] = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()).decode().rstrip("=")
        record[2] = str(len(content))
buffer = io.StringIO(newline="")
csv.writer(buffer).writerows(records)
record_path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
value["record"]["after"] = digest(record_path)
value["correction"] = "Restored unrelated Cloud and MCP edits; only verified resume edits retained."
path.write_text(json.dumps(value, indent=2), encoding="utf-8")
assert all(digest(target / r["file"]) == r["after"] for r in value["files"])
print(json.dumps({"mcp_patch": "one argument added to existing continuation call",
                  "unrelated_checkout_edits_preserved": True}))
