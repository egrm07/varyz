"""Atomic publication survives transient Windows locks and preserves errors."""

from pathlib import Path

import pytest

from varyz import filesystem


def locked(code: int) -> OSError:
    error = OSError("directory is temporarily locked")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_publication_retries_transient_windows_locks(tmp_path, monkeypatch, code):
    source, destination = tmp_path / "staging", tmp_path / "ready"
    source.mkdir()
    (source / "verified.bin").write_bytes(b"complete")
    replace = filesystem.os.replace
    attempts = []
    waits = []

    def briefly_locked(left, right):
        attempts.append((left, right))
        if len(attempts) < 3:
            assert not destination.exists()
            raise locked(code)
        replace(left, right)

    monkeypatch.setattr(filesystem.os, "replace", briefly_locked)
    monkeypatch.setattr(filesystem.time, "sleep", waits.append)
    filesystem.publish_directory(source, destination)
    assert (destination / "verified.bin").read_bytes() == b"complete"
    assert not source.exists()
    assert waits == [0.05, 0.1]


@pytest.mark.parametrize("code, attempts", [(5, 6), (32, 6), (33, 6), (3, 1)])
def test_publication_preserves_staging_and_surfaces_persistent_errors(
    tmp_path: Path, monkeypatch, code: int, attempts: int
):
    source, destination = tmp_path / "staging", tmp_path / "ready"
    source.mkdir()
    (source / "verified.bin").write_bytes(b"complete")
    calls = []

    def always_locked(left, right):
        calls.append((left, right))
        raise locked(code)

    monkeypatch.setattr(filesystem.os, "replace", always_locked)
    monkeypatch.setattr(filesystem.time, "sleep", lambda _: None)
    with pytest.raises(OSError):
        filesystem.publish_directory(source, destination)
    assert len(calls) == attempts
    assert (source / "verified.bin").read_bytes() == b"complete"
    assert not destination.exists()
