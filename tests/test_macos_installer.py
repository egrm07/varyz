"""Check Mac package staging and safety without executing a Mac app or installer."""

from __future__ import annotations

import importlib.util
import json
import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("mac_builder", ROOT / "scripts/macos/build.py")
native = load("mac_worker_test", ROOT / "scripts/windows/native.py")
launch = load("mac_launch_test", ROOT / "scripts/macos/launch.py")


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / "source"
    files = {
        "pyproject.toml": '[project]\nversion = "0.1.0"\n',
        "scripts/windows/native.py": "worker",
        "src/varyz/release_upgrade.py": "upgrade",
        "dist/varyzai-0.1.0-py3-none-any.whl": "wheel",
        "dist/varyzai-0.1.0.tar.gz": "source",
        "LICENSE": "license",
        "NOTICE": "notice",
        "LICENSE.additional-terms": "additional",
        ".varyz/config.toml": "private fixture must never be packaged",
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    runtime = tmp_path / "python"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3").write_bytes(b"fake Mac runtime")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("uv", "UV-LICENSE-MIT", "UV-LICENSE-APACHE"):
        (tools / name).write_text(name, encoding="utf-8")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "dependency.whl").write_bytes(b"dependency")
    monkeypatch.setattr(builder, "ROOT", root)
    return tmp_path / "stage", runtime, tools / "uv", wheels


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
def test_staged_mac_payload_verifies_and_refuses_tampering(inputs, monkeypatch, arch):
    stage, runtime, uv, wheels = inputs
    manifest = builder.assemble(stage, runtime, uv, wheels, arch)
    payload = stage / "root/Applications/Varyz Setup.app/Contents/Resources/payload"
    monkeypatch.setattr(native, "IDENTITY", "varyz-native-macos-v1")
    monkeypatch.setattr(native, "UV_NAME", "uv")
    monkeypatch.setattr(native, "PYTHON_NAME", "runtime/bin/python3")
    monkeypatch.setattr(native, "WINDOWS", False)
    assert native.check_payload(payload) == manifest
    assert not any("config.toml" in name for name in manifest["files"])
    (payload / "wheelhouse/dependency.whl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        native.check_payload(payload)


def test_package_prevents_relocation_and_root_onboarding(inputs):
    stage, runtime, uv, wheels = inputs
    builder.assemble(stage, runtime, uv, wheels, "arm64")
    components = plistlib.loads((stage / "components.plist").read_bytes())
    assert len(components) == 2
    assert all(item["BundleIsRelocatable"] is False for item in components)
    contents = stage / "root/Applications/Varyz Setup.app/Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["LSMinimumSystemVersion"] == "13.0"
    assert (contents / "MacOS" / info["CFBundleExecutable"]).is_file()
    script = (stage / "scripts/postinstall").read_text()
    assert 'sudo -H -u "$user"' in script
    assert 'root|loginwindow|_mbsetupuser|""' in script
    assert "kill" not in (stage / "scripts/preinstall").read_text()
    assert '"$major" -lt 13' in (stage / "scripts/preinstall").read_text()
    assert '"${3:-/}" != "/"' in (stage / "scripts/preinstall").read_text()


def test_refuses_existing_stage_and_unknown_architecture(inputs):
    stage, runtime, uv, wheels = inputs
    with pytest.raises(ValueError, match="architecture"):
        builder.assemble(stage, runtime, uv, wheels, "other")
    builder.assemble(stage, runtime, uv, wheels, "arm64")
    before = json.loads(
        (
            stage / "root/Applications/Varyz Setup.app/Contents/Resources/payload/payload.json"
        ).read_text()
    )
    with pytest.raises(ValueError, match="immutable"):
        builder.assemble(stage, runtime, uv, wheels, "arm64")
    assert before["architecture"] == "arm64"


def test_root_launch_refused_before_import_or_install(monkeypatch):
    monkeypatch.setattr(launch.os, "getuid", lambda: 0, raising=False)
    with pytest.raises(SystemExit, match="not as root"):
        launch.main()


def test_setup_child_uses_retained_runtime_without_foreign_python_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    retained = tmp_path / "retained"
    child_env = {"VARYZ_CONFIG": str(tmp_path / "config.toml")}
    worker = SimpleNamespace(install=lambda app: None,
                             read_state=lambda app: {"payload": str(retained)},
                             child_environment=lambda: child_env)
    loader = SimpleNamespace(exec_module=lambda module: None)
    monkeypatch.setattr(launch.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(launch.importlib.util, "spec_from_file_location",
                        lambda *args: SimpleNamespace(loader=loader))
    monkeypatch.setattr(launch.importlib.util, "module_from_spec", lambda spec: worker)
    monkeypatch.setattr(launch.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launch.sys, "argv", ["launch.py"])
    calls = []
    monkeypatch.setattr(launch.subprocess, "call",
                        lambda args, **kwargs: calls.append((args, kwargs)) or 0)
    assert launch.main() == 0
    args, kwargs = calls[0]
    assert args[:3] == [str(retained / "runtime/bin/python3"), "-I",
                       str(retained / "native.py")]
    assert kwargs["env"] == child_env


def test_minimum_macos_uses_compatible_wheel_tags(tmp_path):
    for name in (
        "numpy-2.5.0-cp312-cp312-macosx_14_0_x86_64.whl",
        "lib-1-py3-none-macosx_15_0_arm64.whl",
        "other-1-py3-none-macosx_13_0_x86_64.macosx_14_0_x86_64.whl",
    ):
        (tmp_path / name).touch()
    assert builder.minimum_macos(tmp_path, "x86_64") == (14, 0)
    assert builder.minimum_macos(tmp_path, "arm64") == (15, 0)
