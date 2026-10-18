"""Distribution allowlist excludes loose secrets and rejects unsafe source archives."""

import hashlib
import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def builder(tmp_path, monkeypatch, source_member="src/varyz/__init__.py"):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/plugin"))
    function = runpy.run_path(str(ROOT / "scripts/plugin/bundle_local.py"))["bundle"]
    monkeypatch.setitem(function.__globals__, "ROOT", tmp_path)
    for directory in ("dist", "docs", "scripts", "src/varyz"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "src/varyz/release_upgrade.py").write_text("# fixture runner")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion="0.1.0"\nlicense-files=["LICENSE"]\n'
        '[tool.uv.build-backend]\nsource-exclude=["src/varyz/held_back.py"]\n'
    )
    (tmp_path / "LICENSE").write_text("Test fixture license")
    (tmp_path / "docs/LOCAL_SETUP.md").write_text("Fixture setup")
    (tmp_path / "scripts/install.ps1").write_text(
        '$ErrorActionPreference = "Stop"\n'
        'Write-Host "Varyz is installed; updating it from where it was installed."'
    )
    (tmp_path / "scripts/install.sh").write_text(
        "repository=git+https://private.test\n"
        'echo "Varyz is installed; updating it from where it was installed."'
    )
    with zipfile.ZipFile(tmp_path / "dist/varyzai-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("varyz/__init__.py", "# Fixture package")
    with tarfile.open(tmp_path / "dist/varyzai-0.1.0.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("varyzai-0.1.0/" + source_member)
        member.size = 7
        archive.addfile(member, io.BytesIO(b"fixture"))
    generated = {
        tmp_path / "plugins/codex/install/install.ps1": (
            tmp_path / "scripts/install.ps1"
        ).read_bytes(),
        tmp_path / "plugins/claude-code/install/install.sh": (
            tmp_path / "scripts/install.sh"
        ).read_bytes(),
        tmp_path / "plugins/claude-cloud/.mcp.json": b'{"mcpServers": {}}',
        tmp_path / "plugins/chatgpt/secret.env": b"EXCLUDED",
    }
    monkeypatch.setitem(function.__globals__, "generated", lambda: generated)
    (tmp_path / "secret.env").write_text("DO NOT DISTRIBUTE")
    return function


def test_bundle_content_checksums_and_cached_plugin_wheel(tmp_path, monkeypatch):
    target = builder(tmp_path, monkeypatch)()
    with zipfile.ZipFile(target) as archive:
        assert not any("secret.env" in p for p in archive.namelist())
        for line in archive.read("SHA256SUMS").decode().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        wheel = archive.read("package/varyzai-0.1.0-py3-none-any.whl")
        for client in ("codex", "claude-code"):
            assert (
                archive.read(f"plugins/{client}/install/package/varyzai-0.1.0-py3-none-any.whl")
                == wheel
            )
        assert archive.read("scripts/release_upgrade.py") == b"# fixture runner"
        assert "plugins/claude-cloud/.mcp.json" in archive.namelist()
        assert b"-ReleaseSha256" in archive.read("install.ps1")
        assert b'Join-Path $PSScriptRoot "package/' in archive.read("install.ps1")


@pytest.mark.parametrize(
    "member", ["../secret.env", "private-config.toml", "src/varyz/held_back.py"]
)
def test_reject_source_escape_unknown_file_and_held_back_module(tmp_path, monkeypatch, member):
    with pytest.raises(ValueError):
        builder(tmp_path, monkeypatch, member)()


def test_bundle_can_use_fresh_packages_without_overwriting_other_release_artifacts(
    tmp_path, monkeypatch
):
    function = builder(tmp_path, monkeypatch)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for name in ("varyzai-0.1.0-py3-none-any.whl", "varyzai-0.1.0.tar.gz"):
        (fresh / name).write_bytes((tmp_path / "dist" / name).read_bytes())
    target = function(tmp_path / "fresh/plugin-bundle.zip", fresh)
    assert target.exists()
    assert not (tmp_path / "dist/varyz-local-0.1.0.zip").exists()
