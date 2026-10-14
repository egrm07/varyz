"""What Varyz was called before, and carrying an existing install across.

Until 2026-09-24 the project, its command and everything it left on a machine
were named "varys". A rename that only changed the code would strand all of it:
keys in the credential store under the old service, the archive in varys.db,
OAuth bundles in ~/.varys, VARYS_* variables in the owner's environment, and
MCP entries, hooks, routing skills and a login registration in other
applications' files that start a module which no longer exists.

Each of those is read here, in one place, so that an existing install keeps
working the moment the new code runs and the next `varyz install` (or
`varyz autosync install`) replaces what the old name registered. Nothing here
is ever written under the old name.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import MutableMapping
from pathlib import Path

LEGACY_NAME = "varys"

ENV_PREFIX = "VARYZ_"
LEGACY_ENV_PREFIX = "VARYS_"

DB_NAME = "varyz.db"
LEGACY_DB_NAME = "varys.db"
LEGACY_LOCK_NAME = "varys.lock"

HOME_DIR_NAME = ".varyz"
LEGACY_HOME_DIR_NAME = ".varys"

LEGACY_KEYRING_SERVICE = "varys"
LEGACY_SERVER_KEY = "varys"
LEGACY_SKILL_NAME = "varys-context"
LEGACY_HOOK_MARKER = "varys-hook-"
LEGACY_DAEMON_PREFIX = "VarysAutoSync"
LEGACY_REGISTRATION_MARKER = "Managed by Varys autosync"


def adopt_legacy_environment(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Give every VARYS_* variable its VARYZ_* name, unless that is already set.

    An exported variable outlives the rename in the owner's shell profile, and
    an MCP client entry written by the old installer passes VARYS_AGENT_TOKEN.
    The new name always wins; the old one is only read. Returns the names adopted.
    """
    env = os.environ if environ is None else environ
    adopted = []
    for name, value in list(env.items()):
        if not name.startswith(LEGACY_ENV_PREFIX):
            continue
        current = ENV_PREFIX + name[len(LEGACY_ENV_PREFIX) :]
        if current not in env:
            env[current] = value
            adopted.append(current)
    return adopted


def home_path(name: str) -> Path:
    """~/.varyz/<name>, moved there from ~/.varys/<name> the first time it is asked for.

    The OAuth bundles a sign-in wrote live there. When the move cannot happen --
    Windows refuses to rename a folder while a file in it is open -- the old
    folder is used in place, and the move is tried again next time.
    """
    home = Path.home()
    current = home / HOME_DIR_NAME / name
    legacy = home / LEGACY_HOME_DIR_NAME / name
    if current.exists() or not legacy.is_dir():
        return current
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(current)
    except OSError:
        return legacy
    with contextlib.suppress(OSError):
        legacy.parent.rmdir()  # only when nothing else was kept there
    return current


_ARCHIVES: dict[str, Path] = {}


def archive_path(data_dir: Path) -> Path:
    """The archive file in ``data_dir``, renaming varys.db to varyz.db once.

    The rename happens only when it cannot pull a database out from under
    anyone: the old write lock must be free, the WAL must checkpoint completely,
    and no other connection may still hold the file open (SQLite removes the
    -wal and -shm files when the last one closes). Otherwise the old file is
    used where it is, and the rename is tried again by the next process.
    """
    key = os.path.normcase(os.path.abspath(data_dir))
    known = _ARCHIVES.get(key)
    if known is not None and known.exists():
        return known
    resolved = _adopt_legacy_database(data_dir)
    _ARCHIVES[key] = resolved
    return resolved


def lock_path(data_dir: Path) -> Path:
    """The write lock beside the archive, named after it.

    While varys.db is still in use under its old name, a process of the old
    version may be writing to it and takes varys.lock; taking the same file
    keeps the two from interleaving writes.
    """
    return data_dir / f"{archive_path(data_dir).stem}.lock"


def _adopt_legacy_database(data_dir: Path) -> Path:
    current = data_dir / DB_NAME
    legacy = data_dir / LEGACY_DB_NAME
    if current.exists() or not legacy.is_file():
        return current

    from varyz.db.connection import FileLock

    lock = FileLock(data_dir / LEGACY_LOCK_NAME)
    if not lock.acquire():
        return legacy
    try:
        try:
            conn = sqlite3.connect(legacy, timeout=0)
            try:
                busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return legacy
        if busy:
            return legacy
        for suffix in ("-wal", "-shm"):
            if Path(f"{legacy}{suffix}").exists():
                return legacy
        try:
            legacy.rename(current)
        except OSError:
            return legacy
    finally:
        lock.release()
    with contextlib.suppress(OSError):
        (data_dir / LEGACY_LOCK_NAME).unlink()
    return current


def legacy_backup_database(folder: Path) -> Path | None:
    """The archive inside a backup folder written before the rename, if any."""
    candidate = folder / LEGACY_DB_NAME
    return candidate if candidate.is_file() and not (folder / DB_NAME).exists() else None
