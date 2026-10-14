"""Verify the installed Cloud helper and live daemon without uploading records."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import httpx

import varyz
from varyz.config import load_config
from varyz.resolution.client import ResolutionClient

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".tmp/cloud-redesign-release"
OUT = ROOT / ".tmp/cloud-redesign-local-update"
if "--release-dir" in sys.argv:
    RELEASE = Path(sys.argv[sys.argv.index("--release-dir") + 1]).resolve()
    OUT = RELEASE / "local-update"


def main():
    package = Path(varyz.__file__).resolve().parent
    assert "uv/tools/varyzai" in package.as_posix(), "Probe must use the installed tool."
    receipt = json.loads((RELEASE / "receipt.json").read_text())
    wheel = RELEASE / receipt["wheel"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == receipt["sha256"]
    names = [
        "cloud/computer.py",
        "cloud/computer_connect.js",
        "cloud/payment_wait.js",
        "cloud/client.py",
        "cloud/cli.py",
        "cloud/guided.py",
        "cloud/project_selection.py",
        "cloud/session_report_sync.py",
        "release_upgrade.py",
    ]
    with zipfile.ZipFile(wheel) as contents:
        for name in names:
            assert (package / name).read_bytes() == contents.read("varyz/" + name), name
    cfg = load_config(Path.home() / ".varyz/config.toml")
    client = ResolutionClient(
        cfg.resolution.bind_host,
        cfg.resolution.port,
        auth_mode=cfg.resolution.auth_mode,
        archive=cfg.db_path,
        timeout=5,
    )
    assert client.status().running
    url = f"http://{cfg.resolution.bind_host}:{cfg.resolution.port}/cloud/computer"
    detected = httpx.get(url, headers={"Origin": "https://cloud.varyzai.com"}, timeout=5)
    detected.raise_for_status()
    value = detected.json()
    assert value.get("browser_oauth") is True, "Running helper is older than installed package."
    out = {
        "installed_python": sys.executable,
        "installed_package": str(package),
        "wheel_sha256": receipt["sha256"],
        "package_files_match": True,
        "daemon_running": True,
        "browser_oauth_capability": True,
        "records_uploaded": False,
    }
    target = OUT / "verification.json"
    target.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
