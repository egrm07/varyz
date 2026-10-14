"""Permission-denial tests cannot leave an archive behind."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from tests.conftest import _windows_reinherits_on_change


def _assert_denied(path: Path) -> None:
    with pytest.raises(PermissionError):
        path.read_text(encoding="utf-8")


def test_unreadable_path_restores_read_and_delete_access(tmp_path, unreadable_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    marker = archive / "varyz.db"
    marker.write_text("offline fixture", encoding="utf-8")

    with unreadable_path(archive):
        _assert_denied(marker)

    assert marker.read_text(encoding="utf-8") == "offline fixture"
    shutil.rmtree(archive)
    assert not archive.exists()


def test_unreadable_path_restores_when_the_body_raises(tmp_path, unreadable_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    marker = archive / "varyz.db"
    marker.write_text("offline fixture", encoding="utf-8")

    with pytest.raises(RuntimeError, match="body failed"), unreadable_path(archive):
        _assert_denied(marker)
        raise RuntimeError("body failed")

    assert marker.read_text(encoding="utf-8") == "offline fixture"
    shutil.rmtree(archive)
    assert not archive.exists()


def _snapshot(tmp_path: Path, *entries: str) -> Path:
    # The shape `icacls /save /t` writes: a name line, then its DACL.
    snapshot = tmp_path / "acl.txt"
    snapshot.write_bytes(("\r\n".join(entries) + "\r\n").encode("utf-16-le"))
    return snapshot


def test_a_dacl_windows_would_rewrite_is_recognised(tmp_path):
    # As GitHub's Windows runner saved the fixture's archive before this was
    # recognised: explicit copies of the parent's entries, none inherited.
    runner = _snapshot(
        tmp_path,
        "archive",
        "D:(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)",
        "archive\\varyz.db",
        "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)",
    )
    assert _windows_reinherits_on_change(runner)


def test_inherited_and_protected_dacls_are_restorable(tmp_path):
    inherited = _snapshot(
        tmp_path,
        "archive",
        "D:AI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)",
        "archive\\varyz.db",
        "D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;OW)",
    )
    assert not _windows_reinherits_on_change(inherited)
    protected = _snapshot(tmp_path, "archive", "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)")
    assert not _windows_reinherits_on_change(protected)


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def test_permission_revocation_is_centralized_in_conftest() -> None:
    """The next permission test must use ``unreadable_path``, not improvise."""
    tests_dir = Path(__file__).parent
    forbidden_calls = {
        "chmod",
        "fchmod",
        "SetFileSecurityW",
        "SetNamedSecurityInfoW",
    }
    forbidden_commands = {"attrib", "cacls", "chmod", "icacls", "setfacl"}
    violations: list[str] = []

    for source_path in tests_dir.rglob("*.py"):
        if source_path.name == "conftest.py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node.func)
            if call_name in forbidden_calls:
                violations.append(f"{source_path.name}:{node.lineno} calls {call_name}")
            for argument in (*node.args, *[item.value for item in node.keywords]):
                for literal in ast.walk(argument):
                    if not isinstance(literal, ast.Constant) or not isinstance(
                        literal.value, str
                    ):
                        continue
                    words = literal.value.strip().lower().split(maxsplit=1)
                    if not words:
                        continue
                    command = Path(words[0]).stem
                    if command in forbidden_commands:
                        violations.append(
                            f"{source_path.name}:{node.lineno} invokes {literal.value!r}"
                        )

    assert violations == []
