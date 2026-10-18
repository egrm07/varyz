"""Apply a verified local wheel with a graceful stop and exact recovery-state restore."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from varyz.autosync import enabled_path, paused_path
from varyz.config import load_config
from varyz.resolution.availability import directory
from varyz.resolution.client import ResolutionClient, ResolutionClientError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/cloud-redesign-local-update"
RELEASE = ROOT / ".tmp/cloud-redesign-release"
if "--release-dir" in sys.argv:
    RELEASE = Path(sys.argv[sys.argv.index("--release-dir") + 1]).resolve()
    OUT = RELEASE / "local-update"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    config = Path.home() / ".varyz/config.toml"
    cfg = load_config(config)
    # The home file may redirect to the owner's existing archive. Upgrade the
    # resolved config that the running daemon uses, never the stale home archive.
    config = cfg._source_file
    assert config is not None and config.is_file()
    spec = importlib.util.spec_from_file_location(
        "redesign_upgrade", ROOT / "src/varyz/release_upgrade.py"
    )
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    environment = (
        Path(subprocess.check_output(["uv", "tool", "dir"], text=True).strip()) / "varyzai"
    )
    installed_python = environment / "Scripts/python.exe"
    installed_gui = environment / "Scripts/pythonw.exe"
    assert installed_python.is_file() and installed_gui.is_file()
    wheel = RELEASE / "varyzai-0.1.0-py3-none-any.whl"
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert checksum == json.loads((wheel.parent / "receipt.json").read_text())["sha256"]
    client = ResolutionClient(
        cfg.resolution.bind_host,
        cfg.resolution.port,
        auth_mode=cfg.resolution.auth_mode,
        archive=cfg.db_path,
        timeout=5,
    )
    try:
        was_running = client.status().running
    except ResolutionClientError:
        was_running = False
    paths = [
        directory(cfg) / "ENABLED",
        directory(cfg) / "PAUSED",
        enabled_path(cfg),
        paused_path(cfg),
    ]
    previous = {
        str(p): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in paths
    }
    state = {
        "phase": "stopping",
        "markers": previous,
        "daemon_was_running": was_running,
        "wheel_sha256": checksum,
    }
    (OUT / "state.json").write_text(json.dumps(state, indent=2))
    result = None
    try:
        # Stop owned watchdogs through their documented markers, never force-kill.
        paths[1].parent.mkdir(parents=True, exist_ok=True)
        paths[1].write_text("Cloud redesign release update\n")
        paths[0].unlink(missing_ok=True)
        paths[2].unlink(missing_ok=True)
        if was_running:
            client.stop()
        for _ in range(25):
            if not upgrade.running(environment, [environment / "Scripts/varyz.exe"]):
                break
            time.sleep(1)
        else:
            raise ValueError("Varyz is still in use; no installation was replaced.")
        state["phase"] = "updating"
        (OUT / "state.json").write_text(json.dumps(state, indent=2))
        result = upgrade.upgrade("uv", wheel, config, checksum, package_only=True)
    finally:
        # Existing pauses and consent survive successful updates and failures alike.
        for path in paths:
            value = previous[str(path)]
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(value))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if was_running and previous[str(paths[1])] is None:
            restart = subprocess.run(
                [
                    str(installed_python),
                    "-m",
                    "varyz.cli",
                    "daemon",
                    "start",
                    "--config",
                    str(config),
                ],
                capture_output=True,
                timeout=60,
                creationflags=flags,
            )
            if restart.returncode:
                raise ValueError("Local update ended; daemon restart needs recovery.")
        if previous[str(paths[0])] is not None:
            subprocess.Popen(
                [
                    str(installed_gui),
                    "-m",
                    "varyz.resolution.availability",
                    "--config",
                    str(config),
                    "--watch",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        if previous[str(paths[2])] is not None:
            subprocess.Popen(
                [str(installed_gui), "-m", "varyz.autosync_daemon", "--config", str(config)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        state["phase"] = (
            "applied" if result and result["status"] in {"applied", "up_to_date"} else "restored"
        )
        state["result"] = result
        (OUT / "state.json").write_text(json.dumps(state, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
