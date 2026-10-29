"""Standalone, stdlib-only upgrade runner for downloaded uv tool releases.

Run with an independent Python, after closing Varyz agents and the daemon.
The release installer supplies the wheel and checksum; no live installation
is modified merely by importing this module or using --dry-run.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
import zipfile
from contextlib import ExitStack
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

TIMEOUT = 900


def execute(args: list[str], *, timeout: int = TIMEOUT) -> str:
    """Never expose subprocess stderr, which may contain registry credentials."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Upgrade subprocess could not finish.") from exc
    if result.returncode:
        raise ValueError("Upgrade subprocess failed; the installation was not verified.")
    return result.stdout.strip()


@contextlib.contextmanager
def lock(path: Path):
    """The same OS byte/flock protocol as Varyz's existing process locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(
                "Varyz is busy. Close its agents and stop its daemon, then retry."
            ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_wheel(path: Path, expected: str) -> str:
    if not path.is_file() or path.suffix != ".whl" or digest(path) != expected:
        raise ValueError("Release wheel is missing or its checksum does not match.")
    with zipfile.ZipFile(path) as archive:
        metadata = []
        for name in archive.namelist():
            parts = Path(name).parts
            if not parts or Path(name).is_absolute() or ".." in parts or "\\" in name:
                raise ValueError("Unsafe release wheel path.")
            if name.endswith(".dist-info/METADATA"):
                metadata.append(name)
        if len(metadata) != 1:
            raise ValueError("Release wheel has invalid package metadata.")
        value = BytesParser().parsebytes(archive.read(metadata[0]))
        if value.get("Name", "").casefold().replace("_", "-") != "varyzai":
            raise ValueError("The release wheel is not Varyz.")
        version = value.get("Version")
        if not version:
            raise ValueError("Release wheel has no version.")
        return version


def check_version(environment: Path, target: str) -> None:
    paths = [
        *environment.glob("Lib/site-packages/varyzai-*.dist-info/METADATA"),
        *environment.glob("lib/python*/site-packages/varyzai-*.dist-info/METADATA"),
    ]
    if len(paths) != 1:
        raise ValueError("The installed Varyz version could not be verified.")
    current = BytesParser().parsebytes(paths[0].read_bytes()).get("Version", "")
    if not all(re.fullmatch(r"\d+(?:\.\d+)*", v) for v in (current, target)):
        raise ValueError("Prerelease versions need a reviewed upgrade path; nothing changed.")
    left, right = tuple(map(int, current.split("."))), tuple(map(int, target.split(".")))
    length = max(len(left), len(right))
    if right + (0,) * (length - len(right)) < left + (0,) * (length - len(left)):
        raise ValueError("An older release cannot replace the installed version.")


def is_same_release(receipt: dict, expected: str, data: Path) -> bool:
    for entry in receipt["tool"]["requirements"]:
        if entry.get("name") != "varyzai":
            continue
        path = entry.get("path")
        url = urlsplit(entry.get("url", ""))
        if not path and url.scheme == "file" and not url.netloc:
            path = url2pathname(url.path)
        if path:
            cached = Path(path).resolve()
            state_file = cached.parent / "state.json"
            if cached.parent.parent == data / "autosync/releases" and state_file.is_file():
                state = json.loads(state_file.read_text())
                if (
                    state.get("phase") == "applied"
                    and state.get("wheel_sha256") == expected
                    and cached.is_file()
                    and digest(cached) == expected
                ):
                    return True
    return False


def python_in(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def archive_paths(config: Path, *, allow_missing: bool = False) -> tuple[Path, Path]:
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    data = Path(raw.get("storage", {}).get("data_dir", "data"))
    data = (data if data.is_absolute() else config.parent / data).resolve()
    current, legacy = data / "varyz.db", data / "varys.db"
    if current.exists() and legacy.exists():
        raise ValueError("Two archive names exist. Resolve the legacy archive before upgrading.")
    archive = legacy if legacy.exists() else current
    if not archive.is_file() and not allow_missing:
        raise ValueError("The configured archive is missing; repair the existing setup first.")
    return data, archive


def requirement(value: dict) -> str:
    """Preserve supported receipt extras; reject unsupported forms rather than lose them."""
    if set(value) - {"name", "extras", "specifier", "marker", "url", "path", "directory"}:
        raise ValueError("Custom uv requirements need a reviewed upgrade path; nothing changed.")
    name = value.get("name")
    if not isinstance(name, str) or not name or name.startswith("-"):
        raise ValueError("Invalid uv requirement receipt.")
    extras = value.get("extras", [])
    result = name + ("[" + ",".join(extras) + "]" if extras else "")
    url = value.get("url")
    if value.get("path") or value.get("directory"):
        url = Path(value.get("path") or value["directory"]).resolve().as_uri()
    result += " @ " + url if url else value.get("specifier", "")
    return result + (" ; " + value["marker"] if value.get("marker") else "")


def validate_native_options(tool: dict, wheelhouse: Path | None) -> None:
    """Only the native caller may preserve a verified local wheelhouse receipt."""
    options = tool.get("options", {})
    if not options:
        return
    links = options.get("find-links", [])
    if wheelhouse is None or set(options) != {"find-links"} or len(links) != 1:
        raise ValueError("Custom uv tool options need a reviewed upgrade path; nothing changed.")
    parsed = urlsplit(links[0])
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Native receipt wheelhouse must be local.")
    previous = Path(url2pathname(parsed.path)).resolve(strict=True)
    manifest = json.loads((previous.parent / "payload.json").read_text(encoding="utf-8"))
    if (
        manifest.get("identity") not in {"varyz-native-windows-v1", "varyz-native-macos-v1"}
        or previous.name != "wheelhouse"
        or previous.parent.name != manifest.get("build_id")
    ):
        raise ValueError("Native receipt does not point at its retained release payload.")
    wheels = list(previous.glob("*.whl"))
    expected_files = {name for name in manifest["files"] if name.startswith("wheelhouse/")}
    if not wheels or {"wheelhouse/" + path.name for path in wheels} != expected_files:
        raise ValueError("Retained native dependencies are incomplete.")
    for path in wheels:
        name = "wheelhouse/" + path.name
        if path.is_symlink() or manifest["files"].get(name) != digest(path):
            raise ValueError("Retained native dependencies failed verification.")


def install_arguments(
    receipt: dict,
    wheel: Path,
    destination: Path,
    *,
    wheelhouse: Path | None = None,
) -> list[str]:
    tool = receipt["tool"]
    if set(tool) - {"requirements", "entrypoints", "constraints", "overrides", "options", "python"}:
        raise ValueError("Unknown uv tool receipt settings; nothing changed.")
    validate_native_options(tool, wheelhouse)
    primary = [r for r in tool["requirements"] if r.get("name") == "varyzai"]
    if len(primary) != 1:
        raise ValueError("The existing uv tool receipt is not a Varyz installation.")
    if primary[0].get("editable"):
        raise ValueError("Editable source installs need a reviewed upgrade path; nothing changed.")
    extras = primary[0].get("extras", [])
    package = "varyzai" + ("[" + ",".join(extras) + "]" if extras else "")
    args = [package + " @ " + wheel.as_uri(), "--force", "--reinstall", "--no-config"]
    if wheelhouse is not None:
        args += ["--find-links", str(wheelhouse)]
    if tool.get("python"):
        args += ["--python", tool["python"]]
    for entry in tool["requirements"]:
        if entry is not primary[0]:
            args += ["--with", requirement(entry)]
    for field, flag in (("constraints", "--constraints"), ("overrides", "--overrides")):
        entries = tool.get(field, [])
        if entries:
            text = "\n".join(requirement(r) for r in entries) + "\n"
            path = destination / (field + ".txt")
            path.write_text(text, encoding="utf-8")
            args += [flag, str(path)]
    return args


def use_python(args: list[str], python: Path) -> list[str]:
    """Install on `python` instead of any interpreter the receipt recorded."""
    if not python.is_file():
        raise ValueError("The release's own Python is missing; nothing was replaced.")
    args = list(args)
    if "--python" in args:
        at = args.index("--python")
        del args[at:at + 2]
    return args + ["--python", str(python)]


def running(environment: Path, launchers: list[Path]) -> bool:
    """Inspect process identity, retaining/printing no unrelated command lines."""
    if os.name == "nt":
        output = execute(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -ErrorAction Stop | "
                "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress",
            ],
            timeout=30,
        )
        rows = json.loads(output)
        if isinstance(rows, dict):
            rows = [rows]
        commands = [
            str(r.get("ExecutablePath", "")) + " " + str(r.get("CommandLine", ""))
            for r in rows
            if r["ProcessId"] != os.getpid()
        ]
    else:
        output = execute(["ps", "-axo", "pid=,command="], timeout=30)
        commands = [
            line.strip().split(None, 1)[1]
            for line in output.splitlines()
            if len(line.strip().split(None, 1)) == 2
            and line.strip().split(None, 1)[0] != str(os.getpid())
        ]
    targets = [str(environment) + os.sep, *map(str, launchers)]
    return any(
        target.casefold() in command.casefold() for target in targets for command in commands
    )


def backup_db(source: Path, target: Path) -> None:
    with (
        contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        contextlib.closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Archive backup failed its integrity check.")


def prune_backups(parent: Path, keep: Path) -> int:
    """Remove what earlier upgrades left behind; return how many folders went.

    Every upgrade kept a whole tool environment and up to three copies of the
    archive, so a Mac upgraded 0.1.0 to 0.1.5 held 271 MB of them. Only the
    newest backup is still a rollback; within it the pre-upgrade archive is
    kept once (original.db) and the duplicate copy goes; its wheel stays,
    because the uv receipt installs from that path. A backup whose
    upgrade was interrupted is never touched: it is what recovery needs.
    """
    for name in ("archive.db", "archive.db-wal", "archive.db-shm"):
        if (keep / "original.db").is_file():
            with contextlib.suppress(OSError):
                (keep / name).unlink(missing_ok=True)
    removed = 0
    for state_path in parent.glob("*/state.json"):
        folder = state_path.parent
        if folder.resolve() == keep.resolve() or folder.parent.resolve() != parent.resolve():
            continue
        try:
            phase = json.loads(state_path.read_text(encoding="utf-8")).get("phase")
        except (OSError, ValueError):
            continue
        if phase not in {"applied", "rolled_back", "backing_up"}:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += not folder.exists()
    return removed


def write_state(folder: Path, value: dict) -> None:
    temporary = folder / "state.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, folder / "state.json")


def bounded(path: Path, parent: Path) -> Path:
    path = path.absolute()
    if path.parent.resolve() != parent.resolve() or path.is_symlink():
        raise ValueError("Unsafe upgrade destination; nothing changed.")
    return path


def rename(source: Path, destination: Path) -> None:
    """Allow a just-exited Windows verifier's file handles to finish closing."""
    for attempt in range(6):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def restore(folder: Path, state: dict, environment: Path, archive: Path, launchers: list[Path]):
    """Restore from retained snapshots; move failed artifacts aside, never recursively delete."""
    original = folder / "original.db"
    if original.exists() and (
        digest(original) != state.get("original_digest")
        or (archive.exists() and digest(archive) != state.get("candidate_digest"))
    ):
        raise ValueError(
            "Archive changed after the interrupted upgrade; keep it and recover manually."
        )
    if environment.exists():
        failed = bounded(
            environment.with_name("varyzai.failed-" + uuid.uuid4().hex), environment.parent
        )
        rename(bounded(environment, environment.parent), failed)
    shutil.copytree(folder / "environment", environment, symlinks=True)
    for path in launchers:
        name = path.name
        snapshot = folder / "launchers" / name
        if path.exists() or path.is_symlink():
            path.unlink()
        if snapshot.is_symlink():
            path.symlink_to(os.readlink(snapshot))
        else:
            shutil.copy2(snapshot, path)
    if original.exists():
        if archive.exists():
            rename(archive, archive.with_name(archive.name + ".failed-" + uuid.uuid4().hex))
        rename(original, archive)
    state["phase"] = "rolled_back"
    write_state(folder, state)


def upgrade(
    uv: str,
    wheel: Path,
    config: Path,
    expected: str,
    *,
    dry_run: bool = False,
    recovery: Path | None = None,
    wheelhouse: Path | None = None,
    package_only: bool = False,
    python: Path | None = None,
) -> dict:
    """`python` is the interpreter the new release ships. Without it the tool
    keeps the one it was first installed with: uv records that `--python` in
    the receipt, so a Mac on 0.1.5 still ran on the 0.1.0 runtime, and
    deleting that old release folder broke every `varyz` command."""
    config = config.resolve(strict=True)
    tool_root = Path(execute([uv, "tool", "dir"])).resolve(strict=True)
    bin_root = Path(execute([uv, "tool", "dir", "--bin"])).resolve(strict=True)
    environment = bounded(tool_root / "varyzai", tool_root)
    if Path(sys.prefix).resolve().is_relative_to(environment.resolve()):
        raise ValueError(
            "Use the independent release installer; do not run inside the replaced tool."
        )
    data, archive = archive_paths(config, allow_missing=recovery is not None)
    if recovery is not None:
        recorded = Path(json.loads((recovery / "state.json").read_text())["archive"])
        if recorded.parent != data or recorded.name not in {"varyz.db", "varys.db"}:
            raise ValueError("Recovery does not match the configured archive.")
        archive = recorded
    if (
        data.is_relative_to(environment)
        or environment.is_relative_to(data)
        or config.is_relative_to(environment)
    ):
        raise ValueError("Archive/config and the uv tool environment must be separate.")
    receipt_path = (
        (recovery / "environment/uv-receipt.toml")
        if recovery
        else (environment / "uv-receipt.toml")
    )
    receipt = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
    launchers = []
    for entry in receipt["tool"]["entrypoints"]:
        if entry.get("from") != "varyzai":
            raise ValueError("Additional tool entrypoints need a reviewed upgrade path.")
        path = Path(entry["install-path"]).absolute()
        if path.parent.resolve() != bin_root or (
            recovery is None and not (path.exists() or path.is_symlink())
        ):
            raise ValueError("The installed command path is invalid; repair uv tool registration.")
        launchers.append(path)
    if not launchers:
        raise ValueError("No installed Varyz command was found.")
    version = check_wheel(wheel.resolve(), expected) if recovery is None else None
    if recovery is None:
        check_version(environment, version)
    # Validate custom settings even in dry-run, without creating files.
    tool = receipt["tool"]
    validate_native_options(tool, wheelhouse)
    if set(tool) - {
        "requirements",
        "entrypoints",
        "constraints",
        "overrides",
        "options",
        "python",
    }:
        raise ValueError("Custom uv settings need a reviewed upgrade path; nothing changed.")
    for field in ("requirements", "constraints", "overrides"):
        for item in tool.get(field, []):
            if item.get("name") != "varyzai" or field != "requirements":
                requirement(item)
    if dry_run:
        return {
            "status": "dry_run",
            "version": version,
            "backup": "Environment, command launchers, config and archive",
            "requires": "Close Varyz agents and stop its daemon before applying",
        }
    if recovery is None and is_same_release(receipt, expected, data):
        return {"status": "up_to_date", "version": version}
    with ExitStack() as stack:
        for path in (
            tool_root / ".varyz-release.lock",
            data / "autosync/UPDATE.lock",
            data / "autosync/DAEMON.lock",
            data / (archive.stem + ".lock"),
        ):
            stack.enter_context(lock(path))
        if running(environment, launchers):
            raise ValueError("Close Varyz agents and stop its daemon before upgrading.")
        if recovery is not None:
            folder = recovery.resolve(strict=True)
            state = json.loads((folder / "state.json").read_text())
            if digest(config) != digest(folder / "config.toml"):
                raise ValueError(
                    "Configuration changed since the upgrade; review recovery manually."
                )
            if (
                folder.parent != data / "autosync/releases"
                or state.get("phase") not in {"installing", "committing"}
                or state.get("environment") != str(environment)
                or state.get("archive") != str(archive)
            ):
                raise ValueError("This is not an interrupted upgrade for this installation.")
            restore(folder, state, environment, archive, launchers)
            return {"status": "rolled_back", "backup": str(folder)}
        if (data / "autosync/UPDATE_PAUSED").exists():
            raise ValueError("Updates are paused. Resume them before installing a release.")
        parent = data / "autosync/releases"
        for pending in parent.glob("*/state.json"):
            if json.loads(pending.read_text()).get("phase") in {"installing", "committing"}:
                raise ValueError(
                    f"Recover the interrupted upgrade before applying another: {pending.parent}"
                )
        if not package_only:
            for suffix in ("-wal", "-shm"):
                if archive.with_name(archive.name + suffix).exists():
                    raise ValueError(
                        "Archive is still open or has pending WAL state. Close Varyz and retry."
                    )
        guard = stack.enter_context(
            contextlib.closing(
                sqlite3.connect(
                    archive.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=0,
                )
                if package_only
                else sqlite3.connect(archive, timeout=0)
            )
        )
        if not package_only:
            guard.execute("BEGIN IMMEDIATE")
        if guard.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
            with zipfile.ZipFile(wheel) as contents:
                available = {
                    int(Path(name).name.split("_", 1)[0])
                    for name in contents.namelist()
                    if re.fullmatch(r"varyz/db/migrations/\d{4}_.+\.sql", name)
                }
            applied = {r[0] for r in guard.execute("SELECT version FROM schema_migrations")}
            if not applied <= available:
                raise ValueError("Release migrations are older than the existing archive.")
            if package_only and applied != available:
                raise ValueError(
                    "Program-only upgrade requires every release migration already applied."
                )
        elif package_only:
            raise ValueError("Program-only upgrade requires a current migration ledger.")
        parent.mkdir(parents=True, exist_ok=True)
        folder = parent / uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        environment_size = sum(
            p.stat().st_size for p in environment.rglob("*") if p.is_file() and not p.is_symlink()
        )
        needed = environment_size + 3 * archive.stat().st_size
        if shutil.disk_usage(data).free < needed + wheel.stat().st_size + 64 * 1024 * 1024:
            raise ValueError("Not enough space for upgrade backups; nothing was replaced.")
        if shutil.disk_usage(tool_root).free < 2 * environment_size + 64 * 1024 * 1024:
            raise ValueError("Not enough tool-disk space for installation and rollback.")
        state = {
            "phase": "backing_up",
            "environment": str(environment),
            "archive": str(archive),
            "version": version,
            "wheel_sha256": expected,
            "package_only": package_only,
        }
        write_state(folder, state)
        shutil.copytree(environment, folder / "environment", symlinks=True)
        (folder / "launchers").mkdir()
        for path in launchers:
            shutil.copy2(path, folder / "launchers" / path.name, follow_symlinks=False)
        shutil.copy2(config, folder / "config.toml")
        backup_db(archive, folder / "archive.db")
        backup_db(folder / "archive.db", folder / "candidate.db")
        if package_only:
            with contextlib.closing(sqlite3.connect(folder / "candidate.db")) as db:
                schema_before = db.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                version_before = db.execute("PRAGMA user_version").fetchone()[0]
        cached = folder / wheel.name
        shutil.copy2(wheel, cached)
        args = install_arguments(receipt, cached, folder, wheelhouse=wheelhouse)
        if python is not None:
            args = use_python(args, python)
        if "--python" not in args:
            settings = dict(
                line.split("=", 1)
                for line in (environment / "pyvenv.cfg").read_text().splitlines()
                if "=" in line
            )
            home = Path(settings.get("home ", settings.get("home", "")).strip())
            base_python = home / ("python.exe" if os.name == "nt" else "python")
            if not base_python.is_file():
                raise ValueError(
                    "The existing base interpreter is missing; repair the runtime first."
                )
            args += ["--python", str(base_python)]
        state["phase"] = "installing"
        write_state(folder, state)
        try:
            execute([uv, "tool", "install", *args])
            python = str(python_in(environment))
            execute([python, "-c", "import varyz.cli, varyz.mcp_server, varyz.autosync_daemon"])
            execute(
                [
                    python,
                    "-c",
                    "import sys; from varyz.config import load_config; "
                    "from pathlib import Path; load_config(Path(sys.argv[1])); "
                    "import sqlite3, sqlite_vec; from varyz.db.connection import migrate; "
                    "c=sqlite3.connect(sys.argv[2]); c.enable_load_extension(True); "
                    "sqlite_vec.load(c); c.enable_load_extension(False); migrate(c); "
                    "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; c.close()",
                    str(config),
                    str(folder / "candidate.db"),
                ]
            )
            if digest(config) != digest(folder / "config.toml") or running(environment, launchers):
                raise ValueError("Configuration changed or Varyz restarted during the upgrade.")
            for suffix in ("-wal", "-shm"):
                if (folder / ("candidate.db" + suffix)).exists():
                    raise ValueError("Verified archive is still open; upgrade was not committed.")
            if package_only:
                with contextlib.closing(sqlite3.connect(folder / "candidate.db")) as db:
                    if (
                        db.execute(
                            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                        ).fetchall()
                        != schema_before
                        or db.execute("PRAGMA user_version").fetchone()[0] != version_before
                    ):
                        raise ValueError("Release changed the schema; use a full archive upgrade.")
            guard.rollback()
            guard.close()
            if not package_only:
                for suffix in ("-wal", "-shm"):
                    if archive.with_name(archive.name + suffix).exists():
                        raise ValueError(
                            "The live archive was reopened; upgrade was not committed."
                        )
            state["phase"] = "committing"
            state["candidate_digest"] = digest(folder / "candidate.db")
            state["original_digest"] = digest(archive)
            write_state(folder, state)
            if not package_only:
                rename(archive, folder / "original.db")
                rename(folder / "candidate.db", archive)
            state["phase"] = "applied"
            write_state(folder, state)
        except (Exception, KeyboardInterrupt) as error:
            try:
                restore(folder, state, environment, archive, launchers)
            except (OSError, ValueError):
                raise ValueError(
                    f"Upgrade interrupted; retained backup needs recovery: {folder}"
                ) from None
            raise ValueError(
                f"Upgrade failed; previous installation and archive restored. Backup: {folder}"
            ) from error
        removed = prune_backups(parent, folder)
    return {
        "status": "applied",
        "version": version,
        "backup": str(folder),
        "removed_old_backups": removed,
        "archive_replaced": not package_only,
        "next_step": "Restart your agents and daemon. Existing connections and sources are kept.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sha256", default="")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--package-only",
        action="store_true",
        help="Upgrade the program without replacing an already-current archive",
    )
    parser.add_argument("--recover", type=Path)
    args = parser.parse_args()
    if args.recover is None and args.wheel is None:
        parser.error("--wheel and --sha256 are required unless recovering")
    try:
        result = upgrade(
            args.uv,
            args.wheel or Path("."),
            args.config,
            args.sha256,
            dry_run=args.dry_run,
            recovery=args.recover,
            package_only=args.package_only,
        )
    except (ValueError, OSError, sqlite3.Error, tomllib.TOMLDecodeError, zipfile.BadZipFile) as exc:
        # Paths/status are actionable; no provider output or secret receipt fields.
        message = (
            str(exc) if isinstance(exc, ValueError) else "Upgrade could not access its local files."
        )
        print(json.dumps({"status": "failed", "message": message}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
