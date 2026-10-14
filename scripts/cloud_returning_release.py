"""Patch only the reviewed returning-user files into the last verified helper wheel."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".tmp/cloud-redesign-release"
OUT = ROOT / ".tmp/cloud-returning-release"


def main():
    receipt = json.loads((BASE / "receipt.json").read_text())
    source = BASE / receipt["wheel"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == receipt["sha256"]
    names = ["cloud/guided.py", "cloud/ui.py", "cloud/computer_connect.js", "cloud/payment_wait.js"]
    with zipfile.ZipFile(source) as old:
        infos = old.infolist()
        files = {info.filename: old.read(info) for info in infos}
    records = [name for name in files if name.endswith(".dist-info/RECORD")]
    assert len(records) == 1
    for name in names:
        assert "varyz/" + name in files
        files["varyz/" + name] = (ROOT / "src/varyz" / name).read_bytes()
    table = io.StringIO(newline="")
    writer = csv.writer(table, lineterminator="\n")
    for name, content in files.items():
        if name != records[0]:
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            writer.writerow((name, "sha256=" + digest, len(content)))
    writer.writerow((records[0], "", ""))
    files[records[0]] = table.getvalue().encode()
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / source.name
    with zipfile.ZipFile(target, "w") as wheel:
        for info in infos:
            wheel.writestr(info, files[info.filename])
    with zipfile.ZipFile(target) as wheel:
        assert wheel.testzip() is None
        wheel.extractall(OUT / "unpacked")
    result = {
        "wheel": target.name,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "base_sha256": receipt["sha256"],
        "changed_files": names,
        "purpose": "Account-verified returning-user detection and dashboard skip",
    }
    (OUT / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
