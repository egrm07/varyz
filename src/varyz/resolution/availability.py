"""Local daemon recovery, independent of source sync and subscription reading."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from varyz.config import Config, load_config
from varyz.db.connection import FileLock
from varyz.domain import iso, utc_now

REGISTRATION = "varyz:daemon-availability"
CHECK_SECONDS = 5


def directory(cfg: Config) -> Path:
    return cfg.data_dir / "daemon"


def is_paused(cfg: Config) -> bool:
    return (directory(cfg) / "PAUSED").exists()


def is_on_demand(cfg: Config) -> bool:
    return (directory(cfg) / "ON_DEMAND").is_file()


def use_on_demand(config_path: Path) -> None:
    """Remove owned login recovery; retain first-request startup without launching."""
    cfg = load_config(config_path)
    target = startup_path(config_path) if sys.platform == "win32" else None
    if target is not None and target.exists():
        if REGISTRATION not in target.read_text(encoding="utf-8"):
            raise ValueError("refusing to remove a startup entry not owned by Varyz")
        target.unlink()
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    (root / "ENABLED").unlink(missing_ok=True)
    (root / "ON_DEMAND").write_text((iso(utc_now()) or "") + "\n", encoding="utf-8")
    set_paused(cfg, False)


def set_paused(cfg: Config, paused: bool) -> None:
    path = directory(cfg) / "PAUSED"
    if paused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((iso(utc_now()) or "") + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _record(cfg: Config, status: str) -> None:
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    record = {"checked_at": iso(utc_now()), "status": status,
              "host": cfg.resolution.bind_host, "port": cfg.resolution.port}
    text = json.dumps(record, ensure_ascii=False)
    with (root / "availability.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")
    temporary = root / "status.json.tmp"
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(root / "status.json")


def check_once(config_path: Path, *, scheduled: bool = False) -> str:
    """Idempotent health check; never start remote services or bypass a pause."""
    from varyz.resolution.client import (
        DaemonUnavailableError,
        ResolutionClient,
        ensure_daemon,
    )

    cfg = load_config(config_path)
    root = directory(cfg)
    lock = FileLock(root / "CHECK.lock")
    if not lock.acquire():
        return "busy"
    try:
        if scheduled and not (root / "ENABLED").exists():
            return "disabled"
        if is_paused(cfg) or not cfg.resolution.autostart:
            status = "paused"
        elif cfg.resolution.bind_host not in {"127.0.0.1", "localhost", "::1"}:
            status = "failed:non_loopback"
        elif not cfg.db_path.is_file():
            status = "failed:missing_archive"
        else:
            client = ResolutionClient(
                cfg.resolution.bind_host, cfg.resolution.port,
                auth_mode=cfg.resolution.auth_mode, archive=cfg.db_path, timeout=5,
            )
            try:
                client.status()
                status = "running"
            except DaemonUnavailableError:
                status = "recovered" if ensure_daemon(client, config_path) else "failed:start"
        _record(cfg, status)
        return status
    except Exception as exc:
        # Exception text may contain private URLs, headers or archive contents.
        status = f"failed:{type(exc).__name__}"
        _record(cfg, status)
        return status
    finally:
        lock.release()


def watch(config_path: Path) -> int:
    """Singleton watchdog; no ingestion, updates or cloud calls of its own."""
    from varyz.autosync import code_fingerprint

    cfg = load_config(config_path)
    root = directory(cfg)
    lock = FileLock(root / "WATCH.lock")
    if not lock.acquire():
        return 0
    fingerprint = code_fingerprint()
    try:
        while (root / "ENABLED").exists():
            if code_fingerprint() != fingerprint:
                # The outer supervisor reloads new installed code after a minute.
                return 1
            check_once(config_path, scheduled=True)
            time.sleep(CHECK_SECONDS)
    finally:
        lock.release()
    return 0


def startup_path(config_path: Path) -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("Windows APPDATA is unavailable")
    key = os.path.normcase(str(config_path.resolve())).encode("utf-8")
    name = "VaryzDaemon-" + hashlib.sha256(key).hexdigest()[:12] + ".vbs"
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / name


def _vbs_string(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("startup paths must not contain newlines")
    return '"' + value.replace('"', '""') + '"'


def startup_script(config_path: Path, python: Path, *, gui_launcher: bool = False) -> str:
    cfg = load_config(config_path)
    command = subprocess.list2cmdline(
        [str(python)] if gui_launcher else
        [str(python), "-m", "varyz.resolution.availability",
         "--config", str(config_path.resolve()), "--watch"]
    )
    marker = _vbs_string(str(directory(cfg) / "ENABLED"))
    # The outer supervisor survives a failed import or a crashed Python worker.
    # A healthy worker runs until uninstalled; duplicate workers exit cleanly.
    return (
        f"' {REGISTRATION}\nOption Explicit\n"
        'Dim shell, files, result\nSet shell = CreateObject("WScript.Shell")\n'
        'Set files = CreateObject("Scripting.FileSystemObject")\n'
        f"Do While files.FileExists({marker})\n"
        "  result = 1\n  On Error Resume Next\n"
        f"  result = shell.Run({_vbs_string(command)}, 0, True)\n"
        "  If Err.Number <> 0 Then result = 1\n  Err.Clear\n  On Error GoTo 0\n"
        "  If result = 0 Then Exit Do\n"
        f"  WScript.Sleep {CHECK_SECONDS * 1000}\nLoop\n"
    )


def refresh_registration(config_path: Path) -> Path | None:
    """Upgrade an existing owned login entry without starting any process."""
    from varyz.windows_hook import write_launchers

    target = startup_path(config_path)
    if not target.is_file():
        return None
    if REGISTRATION not in target.read_text(encoding="utf-8"):
        raise ValueError("refusing to replace a startup entry not owned by Varyz")
    cfg = load_config(config_path)
    launcher = write_launchers(
        cfg.data_dir / "hooks", config_path.resolve(), {"watchdog": "watchdog"}
    )["watchdog"]
    temporary = target.with_suffix(".vbs.tmp")
    temporary.write_text(startup_script(config_path, launcher, gui_launcher=True), encoding="utf-8")
    temporary.replace(target)
    return target


def install(config_path: Path, *, dry_run: bool = False) -> Path:
    if sys.platform != "win32" or not shutil.which("wscript"):
        raise ValueError("daemon login recovery currently requires Windows and wscript")
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    target = startup_path(config_path)
    if target.exists() and REGISTRATION not in target.read_text(encoding="utf-8"):
        raise ValueError("refusing to replace a startup entry not owned by Varyz")
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise ValueError("pythonw.exe is required for invisible Windows startup")
    script = startup_script(config_path, python)
    if dry_run:
        return target
    status = check_once(config_path)
    if status not in {"running", "recovered", "paused"}:
        raise ValueError(f"daemon preflight failed: {status}")
    enabled = directory(cfg) / "ENABLED"
    was_enabled = enabled.exists()
    previous = target.read_text(encoding="utf-8") if target.exists() else None
    enabled.parent.mkdir(parents=True, exist_ok=True)
    enabled.write_text((iso(utc_now()) or "") + "\n", encoding="utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        refresh_registration(config_path)
        subprocess.Popen(
            [shutil.which("wscript") or "wscript.exe", str(target)],
            cwd=str(config_path.parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        if not was_enabled:
            enabled.unlink(missing_ok=True)
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(previous, encoding="utf-8")
        raise
    (directory(cfg) / "ON_DEMAND").unlink(missing_ok=True)
    return target


def uninstall(config_path: Path) -> None:
    cfg = load_config(config_path)
    target = startup_path(config_path)
    if target.exists() and REGISTRATION not in target.read_text(encoding="utf-8"):
        raise ValueError("refusing to remove a startup entry not owned by Varyz")
    set_paused(cfg, True)
    (directory(cfg) / "ENABLED").unlink(missing_ok=True)
    target.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    if args.watch:
        try:
            return watch(args.config)
        except Exception as exc:
            # Native GUI startup drains stderr; persist only the exception class.
            with contextlib.suppress(Exception):
                _record(load_config(args.config), f"failed:startup:{type(exc).__name__}")
            return 1
    return int(check_once(args.config).startswith("failed:"))


if __name__ == "__main__":
    raise SystemExit(main())
