"""Build a Windows x64 native installer from locked, allowlisted local inputs.

Dependencies are downloaded separately with pip --require-hashes from uv export.
This script makes no network calls and never installs Varyz on the build machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage(
    destination: Path, runtime: Path, uv: Path, wheelhouse: Path, package_dir: Path | None = None
) -> dict:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    package_dir = package_dir or ROOT / "dist"
    wheel = package_dir / f"varyzai-{version}-py3-none-any.whl"
    source = package_dir / f"varyzai-{version}.tar.gz"
    if not wheel.is_file() or not source.is_file():
        raise ValueError("Build the wheel and corresponding source archive first: uv build")
    for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        if not (runtime / name).is_file():
            raise ValueError("Use a complete standalone Windows Python runtime")
    if not uv.is_file() or not any(wheelhouse.glob("*.whl")):
        raise ValueError("Bundled uv and the locked dependency wheelhouse are required")
    if destination.exists():
        raise ValueError("Use a new staging folder; retained release payloads are immutable")
    destination.mkdir(parents=True)
    shutil.copytree(
        runtime,
        destination / "runtime",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "site-packages",
            "Scripts",
        ),
    )
    shutil.copy2(uv, destination / "uv.exe")
    shutil.copy2(SOURCE / "native.py", destination / "native.py")
    (destination / "source/installer").mkdir(parents=True)
    for name in ("build.py", "varyz.iss"):
        shutil.copy2(SOURCE / name, destination / "source/installer" / name)
    shutil.copy2(ROOT / "src/varyz/release_upgrade.py", destination / "release_upgrade.py")
    (destination / "package").mkdir()
    shutil.copy2(wheel, destination / "package" / wheel.name)
    shutil.copy2(source, destination / "source" / source.name)
    (destination / "wheelhouse").mkdir()
    for item in sorted(wheelhouse.glob("*.whl")):
        with zipfile.ZipFile(item) as archive:
            if not any(name.endswith(".dist-info/METADATA") for name in archive.namelist()):
                raise ValueError(f"Invalid dependency wheel: {item.name}")
        shutil.copy2(item, destination / "wheelhouse" / item.name)
    for name in project["license-files"]:
        shutil.copy2(ROOT / name, destination / name)
    shutil.copy2(ROOT / "docs/NATIVE_INSTALLER.md", destination / "NATIVE_INSTALLER.md")
    # uv's MIT and Apache licences must be supplied alongside the compiler inputs.
    for name in ("UV-LICENSE-MIT", "UV-LICENSE-APACHE"):
        licence = uv.with_name(name)
        if not licence.is_file():
            raise ValueError(f"Missing bundled uv licence: {name}")
        shutil.copy2(licence, destination / name)
    from PIL import Image

    with Image.open(SOURCE / "assets/icon.png") as icon:
        icon.save(
            destination / "varyz.ico",
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (128, 128)],
        )
    files = {
        path.relative_to(destination).as_posix(): digest(path)
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    content = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {
        "identity": "varyz-native-windows-v1",
        "version": version,
        "build_id": f"{version}-{content[:16]}",
        "wheel": f"package/{wheel.name}",
        "files": files,
    }
    (destination / "payload.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    manifest = stage(
        args.stage.resolve(), args.runtime.resolve(), args.uv.resolve(), args.wheelhouse.resolve(),
        args.package_dir.resolve(),
    )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"VaryzSetup-{manifest['version']}-windows-x64.exe"
    if artifact.exists():
        raise ValueError("Use a new output folder; retained installers are immutable")
    subprocess.run(
        [
            str(args.compiler.resolve()),
            "/Qp",
            f"/DPayload={args.stage.resolve()}",
            f"/DAppVersion={manifest['version']}",
            f"/DBuildId={manifest['build_id']}",
            f"/DOutput={output}",
            str(SOURCE / "varyz.iss"),
        ],
        check=True,
    )
    checksum = {artifact.name: digest(artifact)}
    (output / "native-release-checksums.json").write_text(
        json.dumps(checksum, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "installer": str(artifact),
                "bytes": artifact.stat().st_size,
                "sha256": checksum[artifact.name],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
