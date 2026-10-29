"""Stdlib-only worker for native Windows and macOS installers (inert on import)."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = "native-state.json"
IDENTITY = "varyz-native-macos-v1" if sys.platform == "darwin" else "varyz-native-windows-v1"
UV_NAME = "uv.exe" if os.name == "nt" else "uv"
PYTHON_NAME = "runtime/python.exe" if os.name == "nt" else "runtime/bin/python3"
LAUNCHER_NAME = "varyz.exe" if os.name == "nt" else "varyz"
WINDOWS = os.name == "nt"
# How long uninstall waits for the processes `varyz uninstall` stopped to end.
STOP_WAIT = 20


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child_environment() -> dict[str, str]:
    # A launcher may inherit a different Python installation from its parent.
    # Keep Varyz and uv settings, but never redirect the bundled interpreter.
    return {key: value for key, value in os.environ.items()
            if key.upper() not in {"PYTHONHOME", "PYTHONPATH"}}


def execute(args: list[str], *, timeout: int = 900) -> str:
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_environment(),
        timeout=timeout,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if result.returncode:
        # Subprocess output can contain credentials or source paths; don't put it in dialogs.
        raise ValueError("An installer check failed. No successful installation was recorded.")
    return result.stdout.strip()


def default_config() -> Path:
    configured = os.environ.get("VARYZ_CONFIG") or os.environ.get("VARYS_CONFIG")
    if configured:
        return Path(configured).resolve()
    current, legacy = Path.home() / ".varyz/config.toml", Path.home() / ".varys/config.toml"
    return legacy if not current.exists() and legacy.exists() else current


def load_upgrade():
    spec = importlib.util.spec_from_file_location(
        "native_release_upgrade", HERE / "release_upgrade.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_payload(folder: Path) -> dict:
    manifest = json.loads((folder / "payload.json").read_text(encoding="utf-8"))
    if manifest.get("identity") != IDENTITY or not manifest.get("files"):
        raise ValueError("This is not a Varyz native installer payload.")
    if not re.fullmatch(r"\d+\.\d+\.\d+-[0-9a-f]{16}", manifest.get("build_id", "")):
        raise ValueError("Invalid native runtime identity.")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        path = (folder / relative).resolve(strict=True)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(folder.resolve())
        ):
            raise ValueError("Unsafe installer payload path.")
        if not path.is_file() or sha256(path) != expected:
            raise ValueError("Installer payload checksum failed.")
    required = [UV_NAME, PYTHON_NAME, "native.py", "release_upgrade.py", manifest["wheel"]]
    if WINDOWS:
        required.append("runtime/pythonw.exe")
    if not all(name in manifest["files"] for name in required):
        raise ValueError("Installer payload is incomplete.")
    return manifest


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_state(app: Path) -> dict:
    state = json.loads((app / STATE).read_text(encoding="utf-8"))
    if state.get("identity") != IDENTITY or Path(state["app"]).resolve() != app.resolve():
        raise ValueError("Native installation ownership could not be verified.")
    payload = Path(state["payload"]).resolve(strict=True)
    if not payload.is_relative_to((app / "versions").resolve()):
        raise ValueError("Native runtime is outside the installation.")
    return state


def install(app: Path) -> dict:
    manifest = check_payload(HERE)
    uv = str(HERE / UV_NAME)
    upgrade = load_upgrade()
    root = Path(execute([uv, "tool", "dir"])).resolve()
    bin_root = Path(execute([uv, "tool", "dir", "--bin"])).resolve()
    environment = root / "varyzai"
    config = default_config()
    if (app / STATE).exists():
        config = Path(read_state(app)["config"])
    if config.is_relative_to(app):
        raise ValueError("The archive configuration must stay outside the application folder.")
    if config.exists():
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
        data = Path(raw.get("storage", {}).get("data_dir", "data"))
        data = (data if data.is_absolute() else config.parent / data).resolve()
        if data.is_relative_to(app):
            raise ValueError("The archive must stay outside the removable application folder.")
    if upgrade.running(environment, [bin_root / LAUNCHER_NAME]):
        raise ValueError(
            "Close your Varyz agents and stop its daemon/background sync, then retry. "
            "Nothing was replaced."
        )
    wheel = HERE / manifest["wheel"]
    expected = manifest["files"][manifest["wheel"]]
    upgrade.check_wheel(wheel, expected)
    existing = (environment / "uv-receipt.toml").exists()
    if existing and (app / STATE).exists():
        previous = read_state(app)
        if (
            sha256(environment / "uv-receipt.toml") == previous["receipt_sha256"]
            and check_payload(Path(previous["payload"])) == manifest
        ):
            execute([str(upgrade.python_in(environment)), "-c", "import varyz.cli"])
            return {"status": "already_installed", "version": manifest["version"]}
    if existing:
        # Validate the old receipt, version and archive before copying files or touching uv.
        upgrade.upgrade(uv, wheel, config, expected, dry_run=True, wheelhouse=HERE / "wheelhouse")
    elif environment.exists():
        raise ValueError(
            "An incomplete Varyz tool environment exists. Repair it before native installation."
        )
    destination = app / "versions" / manifest["build_id"]
    if destination.exists():
        if check_payload(destination) != manifest:
            raise ValueError("The retained runtime does not match this release.")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(HERE, destination, symlinks=True)
        check_payload(destination)
    uv = str(destination / UV_NAME)
    # Only the installer process and its children see these settings. No machine config changes.
    os.environ.update(
        {
            "UV_NO_INDEX": "true",
            "UV_FIND_LINKS": str(destination / "wheelhouse"),
            "UV_OFFLINE": "true",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": str(app / "cache"),
        }
    )
    wheel = destination / manifest["wheel"]
    if existing:
        result = upgrade.upgrade(
            uv, wheel, config, expected, wheelhouse=destination / "wheelhouse",
            python=destination / PYTHON_NAME,
        )
    else:
        try:
            execute(
                [
                    uv,
                    "tool",
                    "install",
                    str(wheel),
                    "--no-config",
                    "--python",
                    str(destination / PYTHON_NAME),
                ]
            )
            execute(
                [
                    str(upgrade.python_in(environment)),
                    "-c",
                    "import varyz.cli, varyz.mcp_server, varyz.autosync_daemon, sqlite_vec",
                ]
            )
        except Exception:
            # This branch started without any uv Varyz tool; remove only the new tool.
            if (environment / "uv-receipt.toml").exists():
                execute([uv, "tool", "uninstall", "varyzai"])
            raise
        result = {"status": "installed", "version": manifest["version"]}
    state = {
        "identity": IDENTITY,
        "app": str(app.resolve()),
        "payload": str(destination.resolve()),
        "environment": str(environment),
        "config": str(config),
        "receipt_sha256": sha256(environment / "uv-receipt.toml"),
        "result": result,
    }
    atomic_json(app / STATE, state)
    result["removed_old_runtimes"] = prune_versions(app, destination, environment)
    return result


def runtime_home(environment: Path) -> Path | None:
    """The folder of the interpreter the tool environment runs on."""
    try:
        text = (environment / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home" and value.strip():
            return Path(value.strip()).resolve()
    return None


def prune_versions(app: Path, keep: Path, environment: Path) -> list[str]:
    """Delete release folders nothing runs on any more; keep the current one.

    A folder the tool environment still runs on (an install from before the
    tool was moved to each release's own Python) is never removed.
    """
    home = runtime_home(environment)
    versions = (app / "versions").resolve()
    removed = []
    for folder in sorted(versions.iterdir()) if versions.is_dir() else []:
        folder = folder.resolve()
        if folder == keep.resolve() or not folder.is_dir() or folder.parent != versions:
            continue
        if home is None or home.is_relative_to(folder):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed.append(folder.name)
    return removed


def uninstall_check(app: Path) -> dict:
    state = read_state(app)
    environment = Path(state["environment"])
    uv = str(Path(state["payload"]) / UV_NAME)
    if environment.resolve() != (Path(execute([uv, "tool", "dir"])) / "varyzai").resolve():
        raise ValueError("The uv installation location changed; automatic removal was refused.")
    receipt = environment / "uv-receipt.toml"
    if not receipt.exists():
        return {"status": "already_removed"}
    if sha256(receipt) != state["receipt_sha256"]:
        raise ValueError(
            "Varyz was changed outside this installer. Review its installation before removing it."
        )
    # Running Varyz processes are not a reason to refuse: some (setup's
    # learning, the daemons) run without a window, so the owner could not
    # close them. The uninstall step stops them itself.
    return {"status": "ready_to_remove"}


def uninstall(app: Path) -> dict:
    checked = uninstall_check(app)
    if checked["status"] == "already_removed":
        return checked
    state = read_state(app)
    environment = Path(state["environment"])
    uv = str(Path(state["payload"]) / UV_NAME)
    upgrade = load_upgrade()
    config = Path(state["config"])
    # Always run: besides the archive's integrations it stops every process
    # this installation still runs, which would otherwise hold its files.
    execute(
        [str(upgrade.python_in(environment)), "-m", "varyz", "uninstall"]
        + (["--config", str(config)] if config.exists() else [])
    )
    tool = tomllib.loads((environment / "uv-receipt.toml").read_text(encoding="utf-8"))["tool"]
    launchers = [Path(entry["install-path"]) for entry in tool["entrypoints"]]
    deadline = time.monotonic() + STOP_WAIT
    while upgrade.running(environment, launchers):
        if time.monotonic() > deadline:
            raise ValueError(
                "Varyz is still running and could not be stopped. Restart the computer, "
                "then uninstall again; your archive is kept."
            )
        time.sleep(1)
    execute([uv, "tool", "uninstall", "varyzai"])
    return {"status": "uninstalled", "retained": "Archive, configuration, models and credentials"}


def setup(app: Path) -> dict:
    state = read_state(app)
    environment = Path(state["environment"])
    if not (environment / "uv-receipt.toml").exists():
        raise ValueError("Varyz is no longer installed. Run the Varyz installer again.")
    # Native integration needs absolute command paths, so don't edit the user's PATH.
    python = str(load_upgrade().python_in(environment))
    config = Path(state["config"])
    config.parent.mkdir(parents=True, exist_ok=True)
    log = app / "setup.log"
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            [python, "-m", "varyz", "setup", "--config", str(config), "--web"],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=child_environment(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    if result.returncode not in (0, 130):
        raise ValueError(
            f"Setup needs attention. Its log is at {log}. Reopen Varyz Setup to retry."
        )
    return {"status": "setup_finished" if result.returncode == 0 else "setup_paused"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("install", "uninstall", "uninstall-check", "setup", "verify")
    )
    parser.add_argument("--app", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = {"status": "verified", "version": check_payload(HERE)["version"]}
        else:
            if args.app is None:
                raise ValueError("The native installation folder is required.")
            actions = {
                "install": install,
                "uninstall": uninstall,
                "uninstall-check": uninstall_check,
                "setup": setup,
            }
            result = actions[args.action](args.app.resolve())
        code = 0
    except Exception as error:
        result, code = {"status": "failed", "message": str(error)}, 1
    if args.result:
        atomic_json(args.result, result)
    elif code and os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, result["message"], "Varyz", 0x10)
    elif sys.stdout is not None:
        print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
