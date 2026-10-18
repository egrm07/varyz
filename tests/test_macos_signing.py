"""Integrity checks for native-library signing and payload rehashing."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "mac_signing", Path(__file__).parents[1] / "scripts/macos/sign.py"
)
signing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signing)


def test_signed_wheel_has_valid_record_and_preserved_permissions(tmp_path, monkeypatch):
    wheel = tmp_path / "native.whl"
    native = zipfile.ZipInfo("example/native.so")
    native.external_attr = 0o100755 << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(native, bytes.fromhex("feedfacf") + b"original")
        archive.writestr("example/__init__.py", b"unchanged")
        archive.writestr("example.dist-info/RECORD", b"old-record")

    def sign(path, identity, keychain):
        assert identity == "synthetic-identity"
        path.write_bytes(path.read_bytes() + b"signature")

    monkeypatch.setattr(signing, "sign_file", sign)
    assert signing.sign_wheel(wheel, "synthetic-identity", tmp_path) == 1
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read(native.filename).endswith(b"signature")
        assert archive.read("example/__init__.py") == b"unchanged"
        assert archive.getinfo(native.filename).external_attr == native.external_attr
        rows = list(csv.reader(io.StringIO(archive.read("example.dist-info/RECORD").decode())))
        for name, digest, size in rows:
            if name.endswith("/RECORD"):
                assert digest == size == ""
            else:
                data = archive.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                assert digest == "sha256=" + expected.decode()
                assert int(size) == len(data)


def test_failed_wheel_signing_preserves_original_archive(tmp_path, monkeypatch):
    wheel = tmp_path / "native.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("native.so", bytes.fromhex("feedfacf"))
        archive.writestr("example.dist-info/RECORD", b"")
    original = wheel.read_bytes()

    def fail(*args):
        raise RuntimeError("synthetic signing failure")

    monkeypatch.setattr(signing, "sign_file", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        signing.sign_wheel(wheel, "unused", tmp_path)
    assert wheel.read_bytes() == original


def test_manifest_tracks_signed_bytes_without_hashing_itself(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    binary = payload / "python"
    binary.write_bytes(b"before")
    path = payload / "payload.json"
    path.write_text(json.dumps({"identity": "varyz-native-macos-v1", "version": "0.1.0"}))
    before = signing.refresh_manifest(payload)
    binary.write_bytes(b"after-signing")
    after = signing.refresh_manifest(payload)
    assert after["build_id"] != before["build_id"]
    assert after["files"] == {"python": hashlib.sha256(b"after-signing").hexdigest()}
    assert signing.refresh_manifest(payload) == after


def test_python_verification_cannot_mutate_a_sealed_app(tmp_path, monkeypatch):
    apps = tmp_path / "stage/root/Applications"
    setup = apps / "Varyz Setup.app"
    remover = apps / "Varyz Uninstall.app"
    for app in (setup, remover):
        (app / "Contents/MacOS").mkdir(parents=True)
    payload = setup / "Contents/Resources/payload"
    (payload / "runtime/bin").mkdir(parents=True)
    (payload / "runtime/bin/python3").write_bytes(b"synthetic interpreter")
    (payload / "wheelhouse").mkdir()
    (payload / "native.py").write_text("# synthetic payload check\n")
    (payload / "payload.json").write_text(json.dumps({
        "identity": "varyz-native-macos-v1", "version": "0.1.0", "architecture": "arm64",
    }))
    output = tmp_path / "signed.pkg"
    monkeypatch.setattr(signing, "sys", SimpleNamespace(platform="darwin", argv=[
        "sign.py", "--stage", str(tmp_path / "stage"), "--keychain", str(tmp_path / "key"),
        "--application-identity", "synthetic", "--installer-identity", "synthetic",
        "--output", str(output),
    ]))
    monkeypatch.setattr(signing.argparse._sys, "argv", signing.sys.argv)
    sealed = {}
    verified_bundles = []

    def snapshot(app):
        return {p.relative_to(app).as_posix(): p.read_bytes()
                for p in app.rglob("*") if p.is_file()}

    def command(args, **kwargs):
        if args[0] == "clang":
            Path(args[-1]).write_bytes(b"native launcher")
        elif args[0] == str(payload / "runtime/bin/python3"):
            if kwargs.get("env", {}).get("PYTHONDONTWRITEBYTECODE") != "1":
                (payload / "new-cache.pyc").write_bytes(b"interpreter cache")
        elif args[0] == "codesign" and Path(args[-1]).suffix == ".app":
            app = Path(args[-1])
            if "--sign" in args:
                sealed[app.name] = snapshot(app)
            else:
                assert snapshot(app) == sealed[app.name], "Signed app resources changed"
                verified_bundles.append(app)
        elif args[0] == "pkgbuild":
            output.write_bytes(b"synthetic package")
        elif args[:2] == ["pkgutil", "--expand-full"]:
            shutil.copytree(apps, Path(args[-1]) / "Payload/Applications")

    monkeypatch.setattr(signing.subprocess, "run", command)
    signing.main()
    assert len(verified_bundles) == 4  # Both staged and actual packaged apps.
    assert not (payload / "new-cache.pyc").exists()
