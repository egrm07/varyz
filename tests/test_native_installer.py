"""Native installer integrity, ownership and preflight: no live user installation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from varyz import release_upgrade as upgrade

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/windows/native.py"
SPEC = importlib.util.spec_from_file_location("native_installer", SCRIPT)
assert SPEC and SPEC.loader
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def payload(folder: Path) -> dict:
    files = (
        native.UV_NAME,
        native.PYTHON_NAME,
        "runtime/pythonw.exe",
        "native.py",
        "release_upgrade.py",
        "package/varyzai-0.1.0-py3-none-any.whl",
    )
    for name in files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    manifest = {
        "identity": native.IDENTITY,
        "version": "0.1.0",
        "build_id": "0.1.0-" + "a" * 16,
        "wheel": files[-1],
        "files": {name: native.sha256(folder / name) for name in files},
    }
    (folder / "payload.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_payload_tampering_refuses_install(tmp_path):
    payload(tmp_path)
    (tmp_path / native.UV_NAME).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        native.check_payload(tmp_path)


@pytest.mark.parametrize("build_id", ["../other", "C:/other", "0.1.0"])
def test_runtime_identity_cannot_escape_installation(tmp_path, build_id):
    manifest = payload(tmp_path)
    manifest["build_id"] = build_id
    (tmp_path / "payload.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        native.check_payload(tmp_path)


def test_payload_path_traversal_is_refused(tmp_path):
    folder = tmp_path / "payload"
    folder.mkdir()
    manifest = payload(folder)
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    manifest["files"]["../outside"] = native.sha256(outside)
    (folder / "payload.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe"):
        native.check_payload(folder)


def test_busy_installation_is_refused_before_copying(tmp_path, monkeypatch):
    source = tmp_path / "payload"
    source.mkdir()
    payload(source)
    monkeypatch.setattr(native, "HERE", source)
    monkeypatch.setattr(native, "execute", lambda args: str(tmp_path / "tools"))
    monkeypatch.setattr(native, "load_upgrade", lambda: SimpleNamespace(running=lambda *a: True))
    with pytest.raises(ValueError, match="Nothing was replaced"):
        native.install(tmp_path / "app")
    assert not (tmp_path / "app").exists()


def owned_install(tmp_path, monkeypatch):
    app = tmp_path / "app"
    runtime = app / "versions/release"
    runtime.mkdir(parents=True)
    environment = tmp_path / "tools/varyzai"
    environment.mkdir(parents=True)
    receipt = environment / "uv-receipt.toml"
    receipt.write_text("[tool]\nentrypoints = []\n", encoding="utf-8")
    config = tmp_path / "archive/config.toml"
    config.parent.mkdir()
    config.write_text("preserve me", encoding="utf-8")
    state = {
        "identity": native.IDENTITY,
        "app": str(app),
        "payload": str(runtime),
        "environment": str(environment),
        "config": str(config),
        "receipt_sha256": native.sha256(receipt),
    }
    (app / native.STATE).write_text(json.dumps(state), encoding="utf-8")
    commands = []

    def execute(args):
        commands.append(args)
        return str(tmp_path / "tools")

    monkeypatch.setattr(native, "execute", execute)
    monkeypatch.setattr(
        native,
        "load_upgrade",
        lambda: SimpleNamespace(
            running=lambda *a: False,
            python_in=lambda env: env / "Scripts/python.exe",
        ),
    )
    return app, receipt, config, commands


def test_uninstall_check_never_unregisters_or_deletes(tmp_path, monkeypatch):
    app, receipt, config, commands = owned_install(tmp_path, monkeypatch)
    assert native.uninstall_check(app)["status"] == "ready_to_remove"
    assert all("uninstall" not in command for command in commands)
    assert receipt.exists() and config.read_text() == "preserve me"


def test_changed_tool_receipt_blocks_removal(tmp_path, monkeypatch):
    app, receipt, config, commands = owned_install(tmp_path, monkeypatch)
    receipt.write_text("a different installation", encoding="utf-8")
    with pytest.raises(ValueError, match="outside this installer"):
        native.uninstall(app)
    assert all("uninstall" not in command for command in commands)
    assert config.exists()


def test_uninstall_unregisters_integrations_before_uv_and_retains_archive(tmp_path, monkeypatch):
    app, receipt, config, commands = owned_install(tmp_path, monkeypatch)
    assert native.uninstall(app)["status"] == "uninstalled"
    assert commands[-2][1:4] == ["-m", "varyz", "uninstall"]
    assert commands[-1][1:] == ["tool", "uninstall", "varyzai"]
    assert config.read_text() == "preserve me"


def test_native_uninstall_only_applies_after_the_confirmation():
    recipe = (SCRIPT.parent / "varyz.iss").read_text(encoding="utf-8")
    preflight = recipe.split("function InitializeUninstall(): Boolean;", 1)[1].split(
        "procedure CurUninstallStepChanged", 1
    )[0]
    assert '" uninstall-check --app ' in preflight
    assert '" uninstall --app ' not in preflight
    assert "CurUninstallStep = usUninstall" in recipe


def test_config_override_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("VARYZ_CONFIG", str(tmp_path / "custom.toml"))
    assert native.default_config() == tmp_path / "custom.toml"


def test_child_python_does_not_inherit_foreign_runtime(monkeypatch, tmp_path):
    import os
    import sys

    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "missing-python"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "untrusted-modules"))
    monkeypatch.setenv("VARYZ_CONFIG", str(tmp_path / "custom.toml"))
    output = native.execute([
        sys._base_executable, "-c",
        "import json,os; print(json.dumps({k:os.environ.get(k) for k in "
        "['PYTHONHOME','PYTHONPATH','VARYZ_CONFIG']}))",
    ])
    inherited = json.loads(output)
    assert inherited == {"PYTHONHOME": None, "PYTHONPATH": None,
                         "VARYZ_CONFIG": str(tmp_path / "custom.toml")}
    assert os.environ["PYTHONHOME"] == str(tmp_path / "missing-python")


def test_native_wheelhouse_receipt_requires_verified_complete_local_payload(tmp_path):
    retained = tmp_path / ("0.1.0-" + "b" * 16)
    wheels = retained / "wheelhouse"
    wheels.mkdir(parents=True)
    dependency = wheels / "dependency.whl"
    dependency.write_bytes(b"dependency")
    manifest = {
        "identity": native.IDENTITY,
        "build_id": retained.name,
        "files": {"wheelhouse/dependency.whl": native.sha256(dependency)},
    }
    (retained / "payload.json").write_text(json.dumps(manifest), encoding="utf-8")
    tool = {"options": {"find-links": [wheels.as_uri()]}}
    upgrade.validate_native_options(tool, wheels)
    with pytest.raises(ValueError, match="Custom uv tool options"):
        upgrade.validate_native_options(tool, None)
    dependency.unlink()
    with pytest.raises(ValueError, match="incomplete"):
        upgrade.validate_native_options(tool, wheels)


def test_remote_dependency_receipt_is_never_accepted(tmp_path):
    with pytest.raises(ValueError, match="must be local"):
        upgrade.validate_native_options(
            {"options": {"find-links": ["https://untrusted.example/wheels"]}}, tmp_path
        )
