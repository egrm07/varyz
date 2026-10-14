"""SQLite connection management: WAL, sqlite-vec, migrations, write lock."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import BinaryIO

import sqlite_vec

from varyz.domain import iso, utc_now
from varyz.legacy import lock_path

MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


class LockHeldError(RuntimeError):
    """Another varyz process holds the write lock."""


class NotAnArchiveError(RuntimeError):
    """The file at the archive path is not a database varyz can open."""


class _ReleaseConnection(sqlite3.Connection):
    """Keep uv-tool release replacement out while an archive connection is alive."""

    release_lock: FileLock | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.release_lock is not None:
                self.release_lock.release()
                self.release_lock = None

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def connect(db_path: Path) -> sqlite3.Connection:
    release_lock = None
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        release_lock = FileLock(prefix.parent / ".varyz-release.lock", shared=True)
        if not release_lock.acquire():
            raise LockHeldError(
                "Varyz is being upgraded. Retry after the release installer finishes."
            )
    try:
        if release_lock is not None:
            for state_path in (db_path.parent / "autosync/releases").glob("*/state.json"):
                state = json.loads(state_path.read_text())
                if state.get("phase") in {"installing", "committing"} and (
                    Path(state["archive"]).resolve() == db_path.resolve()
                ):
                    raise LockHeldError(
                        "A Varyz release upgrade was interrupted. Use its retained backup "
                        "with the release installer's recovery option before reopening the archive."
                    )
        return _connect(db_path, release_lock)
    except BaseException:
        if release_lock is not None:
            release_lock.release()
        raise


def _connect(db_path: Path, release_lock: FileLock | None) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, factory=_ReleaseConnection)
    conn.release_lock = release_lock
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except sqlite3.DatabaseError as exc:
        conn.close()
        # A half-copied file, a sync conflict, or a path pointed at something
        # that was never an archive: sqlite says "file is not a database" and
        # every command answered with forty lines of traceback wrapped around
        # it. The path is the whole answer, and only this function knows it.
        if "not a database" not in str(exc):
            raise
        raise NotAnArchiveError(
            f"{db_path} is not a varyz archive; sqlite cannot read it. "
            "Point --config at the right one, or restore a copy taken with varyz backup."
        ) from exc
    return conn


def _iter_migrations() -> list[tuple[int, str, str]]:
    pkg = resources.files("varyz.db") / "migrations"
    found = []
    for item in pkg.iterdir():
        m = MIGRATION_RE.match(item.name)
        if m:
            found.append((int(m.group(1)), item.name, item.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in order. Returns names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, name, sql in _iter_migrations():
        if version in applied:
            continue
        script = (
            f"BEGIN;\n{sql}\n"
            f"INSERT INTO schema_migrations (version, applied_at)"
            f" VALUES ({version}, '{iso(utc_now())}');\nCOMMIT;"
        )
        conn.executescript(script)
        done.append(name)
    return done


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@dataclass
class _HeldFileLock:
    handle: BinaryIO
    shared: bool
    users: int = 1


_FILE_LOCK_GUARD = threading.Lock()
_HELD_FILE_LOCKS: dict[str, _HeldFileLock] = {}


def _lock_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _windows_lock(handle: BinaryIO, *, unlock: bool = False, shared: bool = False) -> None:
    """MSVCRT's read-lock constants are exclusive on Windows; use the native API."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
    overlap = Overlapped()  # Byte zero, matching the release installer's exclusive lock.
    if unlock:
        function = kernel.UnlockFileEx
        function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, ctypes.POINTER(Overlapped)]
        args = (native, 0, 1, 0, ctypes.byref(overlap))
    else:
        function = kernel.LockFileEx
        function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        args = (native, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlap))
    function.restype = wintypes.BOOL
    if not function(*args):
        raise ctypes.WinError(ctypes.get_last_error())


def _lock_handle(handle: BinaryIO, *, shared: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        _windows_lock(handle, shared=shared)
        return
    import fcntl

    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)


def _unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        _windows_lock(handle, unlock=True)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Non-blocking cross-process byte lock with process-local semantics.

    POSIX lock APIs do not all reject another acquisition by the same process.
    The registry makes an exclusive lock non-reentrant on every platform and
    reference-counts shared holders so one local release cannot expose another.
    """

    def __init__(self, path: Path, *, shared: bool = False):
        self.path = path
        self.shared = shared
        self._key: str | None = None
        self._pid: int | None = None

    @property
    def handle(self) -> BinaryIO:
        if self._key is None:
            raise RuntimeError("file lock is not held")
        return _HELD_FILE_LOCKS[self._key].handle

    def acquire(self) -> bool:
        if self._key is not None and self._pid == os.getpid():
            return True
        self._key = None
        self._pid = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = _lock_key(self.path)
        with _FILE_LOCK_GUARD:
            held = _HELD_FILE_LOCKS.get(key)
            if held is not None:
                if not self.shared or not held.shared:
                    return False
                held.users += 1
                self._key = key
                self._pid = os.getpid()
                return True

            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                _lock_handle(handle, shared=self.shared)
            except OSError:
                handle.close()
                return False
            _HELD_FILE_LOCKS[key] = _HeldFileLock(handle, self.shared)
            self._key = key
            self._pid = os.getpid()
            return True

    def release(self) -> None:
        key = self._key
        if key is None:
            return
        with _FILE_LOCK_GUARD:
            held = _HELD_FILE_LOCKS.get(key)
            self._key = None
            self._pid = None
            if held is None:
                return
            if held.users > 1:
                held.users -= 1
                return
            _HELD_FILE_LOCKS.pop(key, None)
            with contextlib.suppress(OSError):
                _unlock_handle(held.handle)
            held.handle.close()


def _close_inherited_file_locks() -> None:
    """Drop inherited descriptors without unlocking the parent's file description."""
    for held in _HELD_FILE_LOCKS.values():
        with contextlib.suppress(OSError):
            held.handle.close()
    _HELD_FILE_LOCKS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_file_locks)


class WriteLock:
    """Cross-process exclusive lock so a scheduled sync and an interactive
    command can't interleave writes. Readers don't take it (WAL handles them)."""

    def __init__(self, data_dir: Path):
        self.path = lock_path(data_dir)
        self._lock: FileLock | None = None
        self._fh = None

    def __enter__(self) -> WriteLock:
        lock = FileLock(self.path)
        if not lock.acquire():
            raise LockHeldError(
                "Another varyz process is writing (sync already running?)"
            )
        self._lock = lock
        self._fh = lock.handle
        return self

    def __exit__(self, *exc_info) -> None:
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self._fh = None
