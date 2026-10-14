"""Restore only unrelated files from one verified local rollout backup."""
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
names = {"cloud/sharing_models.py", "cloud/sharing_routes.py",
         "cloud/sharing_service.py", "cloud/sharing_ui.py"}
rows = [row for row in value["files"] if row["file"] in names]
assert len(rows) == len(names) and value["status"] == "applied"
assert Path(value["target"]).resolve() == target
record_path = Path(value["record"]["path"])
assert digest(record_path) == value["record"]["after"]
for row in rows:
    assert digest(target / row["file"]) == row["after"]
    assert digest(folder / "backup" / row["file"]) == row["before"]
for row in rows:
    destination = target / row["file"]
    temporary = destination.with_suffix(".restored-update")
    temporary.write_bytes((folder / "backup" / row["file"]).read_bytes())
    os.replace(temporary, destination)
    row["attempted_after"] = row["after"]
    row["after"] = row["before"]
    row["unrelated_reverted"] = True
records = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))))
for record in records:
    if record[0] in {"varyz/" + name for name in names}:
        content = (target.parent / record[0]).read_bytes()
        record[1] = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()).decode().rstrip("=")
        record[2] = str(len(content))
buffer = io.StringIO(newline="")
csv.writer(buffer).writerows(records)
record_path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
value["record"]["attempted_after"] = value["record"]["after"]
value["record"]["after"] = digest(record_path)
value["correction"] = "Restored four unrelated Cloud files; only continuation files differ."
path.write_text(json.dumps(value, indent=2), encoding="utf-8")
assert all(digest(target / row["file"]) == row["after"] for row in value["files"])
print(json.dumps({"restored": sorted(names), "final_changed_files":
                  [r["file"] for r in value["files"] if r["before"] != r["after"]]}))
