"""Bundle built wheel/source and an explicit installer/plugin allowlist.

First run uv build --out-dir dist, then python scripts/plugin/bundle_local.py.
No recursive repository packaging or private archive/config collection.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path

from build import ROOT, generated


def bundle(destination: Path | None = None, package_dir: Path | None = None) -> Path:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    package_dir = package_dir or ROOT / "dist"
    wheel = package_dir / f"varyzai-{version}-py3-none-any.whl"
    source = package_dir / f"varyzai-{version}.tar.gz"
    build_config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    exclusions = build_config["tool"]["uv"]["build-backend"]["source-exclude"]
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.parts[0]
                not in {
                    "varyz",
                    f"varyzai-{version}.dist-info",
                }
            ):
                raise ValueError("Unexpected wheel member")
            if "src/" + name in exclusions:
                raise ValueError("Held-back module in wheel")
    # Guard the source distribution's boundaries before including it.
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError("Unsafe source archive member")
            if path.parts[0] != f"varyzai-{version}":
                raise ValueError("Unexpected source archive root")
            relative = path.parts[1:]
            if "/".join(relative) in exclusions:
                raise ValueError("Held-back module in source archive")
            if relative and relative[0] not in {
                "src",
                "PKG-INFO",
                "pyproject.toml",
                "README.md",
                *project["license-files"],
            }:
                raise ValueError(f"Unexpected source archive member: {member.name}")
    files: dict[str, bytes] = {
        "package/" + wheel.name: wheel.read_bytes(),
        "source/" + source.name: source.read_bytes(),
        "SETUP.md": (ROOT / "docs/LOCAL_SETUP.md").read_bytes(),
        "scripts/release_upgrade.py": (ROOT / "src/varyz/release_upgrade.py").read_bytes(),
    }
    for name in project["license-files"]:
        files[name] = (ROOT / name).read_bytes()
    # Generated map, not directory scanning: unrelated local files never enter the ZIP.
    for path, data in generated().items():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(
            (
                "plugins/codex/",
                "plugins/claude-code/",
                "plugins/claude-cloud/",
                "plugins/.agents/",
                "plugins/.claude-plugin/",
            )
        ):
            if relative.endswith("/install/install.ps1"):
                data = data.replace(
                    b'$ErrorActionPreference = "Stop"',
                    (
                        '$bundledWheel = Join-Path $PSScriptRoot "package/' + wheel.name + '"\n'
                        "if (-not $PackageSource -and (Test-Path -LiteralPath $bundledWheel)) "
                        "{ $PackageSource = [System.IO.Path]::GetFullPath($bundledWheel) }\n"
                        '$ReleaseUpgradeScript = Join-Path $PSScriptRoot "release_upgrade.py"\n'
                        f'$ReleaseSha256 = "{hashlib.sha256(wheel.read_bytes()).hexdigest()}"\n'
                        '$ErrorActionPreference = "Stop"'
                    ).encode(),
                )
            elif relative.endswith("/install/install.sh"):
                data = data.replace(
                    b"repository=git+",
                    (
                        'bundled_wheel="$(dirname "$0")/package/' + wheel.name + '"\n'
                        'if [ -z "${VARYZ_PACKAGE_SOURCE:-}" ] && [ -f "$bundled_wheel" ]; then\n'
                        "  VARYZ_PACKAGE_SOURCE=$bundled_wheel; export VARYZ_PACKAGE_SOURCE\nfi\n"
                        'VARYZ_RELEASE_UPGRADE_SCRIPT="$(dirname "$0")/release_upgrade.py"\n'
                        f'VARYZ_RELEASE_SHA256="{hashlib.sha256(wheel.read_bytes()).hexdigest()}"\n'
                        "export VARYZ_RELEASE_UPGRADE_SCRIPT VARYZ_RELEASE_SHA256\n"
                        "repository=git+"
                    ).encode(),
                )
            files[relative] = data
    for client in ("codex", "claude-code"):
        # Plugin managers copy the package into their own cache. Keep its installer
        # wheel inside that package, not at a sibling release path that disappears.
        files[f"plugins/{client}/install/package/{wheel.name}"] = wheel.read_bytes()
        files[f"plugins/{client}/install/release_upgrade.py"] = (
            ROOT / "src/varyz/release_upgrade.py"
        ).read_bytes()
    for name in ("install.ps1", "install.sh"):
        files["scripts/" + name] = (ROOT / "scripts" / name).read_bytes()
    files["install.ps1"] = (
        'param([switch]$DryRun, [switch]$Web, [string]$Recover = "")\n'
        f'$wheel = Join-Path $PSScriptRoot "package/{wheel.name}"\n'
        '& (Join-Path $PSScriptRoot "scripts/install.ps1") '
        "-PackageSource $wheel -DryRun:$DryRun -Web:$Web "
        '-ReleaseUpgradeScript (Join-Path $PSScriptRoot "scripts/release_upgrade.py") '
        f'-ReleaseSha256 "{hashlib.sha256(wheel.read_bytes()).hexdigest()}" '
        "-ReleaseRecovery $Recover\n"
    ).encode()
    files["install.sh"] = (
        "#!/bin/sh\nset -eu\n"
        'bundle_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'VARYZ_RELEASE_UPGRADE_SCRIPT="$bundle_root/scripts/release_upgrade.py"\n'
        f'VARYZ_RELEASE_SHA256="{hashlib.sha256(wheel.read_bytes()).hexdigest()}"\n'
        "export VARYZ_RELEASE_UPGRADE_SCRIPT VARYZ_RELEASE_SHA256\n"
        f'exec sh "$bundle_root/scripts/install.sh" --package '
        f'"$bundle_root/package/{wheel.name}" "$@"\n'
    ).encode()
    files["Install Varyz.cmd"] = (
        b"@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "
        b'"%~dp0install.ps1" -Web\r\nif errorlevel 1 pause\r\n'
    )
    files["Install Varyz.command"] = b'#!/bin/sh\nexec sh "$(dirname "$0")/install.sh" --web\n'
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())
    ).encode()
    target = destination or ROOT / f"dist/varyz-local-{version}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (
                0o100755 if name.endswith((".sh", ".command")) else 0o100644
            ) << 16
            archive.writestr(info, data)
    checksums = {
        target.name: hashlib.sha256(target.read_bytes()).hexdigest(),
        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
        source.name: hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (target.parent / "local-release-checksums.json").write_text(
        json.dumps(checksums, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(bundle(args.output, args.package_dir))
