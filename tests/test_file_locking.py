"""Behavioral checks for the cross-platform process-lock primitive."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from varyz.db.connection import FileLock, LockHeldError, WriteLock

_TRY_LOCK = """
import sys
from pathlib import Path
from varyz.db.connection import FileLock

lock = FileLock(Path(sys.argv[1]), shared=len(sys.argv) > 2 and sys.argv[2] == "shared")
acquired = lock.acquire()
print("acquired" if acquired else "held")
if acquired:
    lock.release()
"""


def _try_lock_in_child(path: Path, *, shared: bool = False) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(path), "shared" if shared else "exclusive"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def test_exclusive_lock_rejects_same_process_and_child_then_releases(tmp_path):
    data_dir = tmp_path / "archive"
    with WriteLock(data_dir) as held:
        with pytest.raises(LockHeldError), WriteLock(data_dir):
            pass
        assert _try_lock_in_child(held.path) == "held"

    assert _try_lock_in_child(held.path) == "acquired"


def test_closing_an_unrelated_descriptor_does_not_release_the_lock(tmp_path):
    path = tmp_path / "archive" / "varyz.lock"
    with WriteLock(path.parent):
        with path.open("rb") as unrelated:
            unrelated.seek(1)
            unrelated.read()
        assert _try_lock_in_child(path) == "held"


def test_shared_holders_keep_exclusive_children_out_until_the_last_release(tmp_path):
    path = tmp_path / "managed-server.users.lock"
    first = FileLock(path, shared=True)
    second = FileLock(path, shared=True)
    assert first.acquire()
    assert second.acquire()
    assert _try_lock_in_child(path, shared=True) == "acquired"

    first.release()
    assert _try_lock_in_child(path) == "held"
    second.release()
    assert _try_lock_in_child(path) == "acquired"


def test_exclusive_release_lock_blocks_shared_clients_in_other_processes(tmp_path):
    lock = FileLock(tmp_path / "release.lock")
    assert lock.acquire()
    try:
        assert _try_lock_in_child(lock.path, shared=True) == "held"
    finally:
        lock.release()
    assert _try_lock_in_child(lock.path, shared=True) == "acquired"


def test_forked_child_drops_its_descriptor_without_releasing_the_parent(tmp_path):
    if not hasattr(os, "fork"):
        return
    path = tmp_path / "fork.lock"
    parent = FileLock(path)
    assert parent.acquire()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        parent.release()
        child = FileLock(path)
        acquired = child.acquire()
        os.write(write_fd, b"acquired" if acquired else b"held")
        if acquired:
            child.release()
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 16) == b"held"
        _, status = os.waitpid(pid, 0)
        assert status == 0
    finally:
        os.close(read_fd)
        parent.release()
