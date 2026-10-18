"""Assemble a macOS PKG with Apple's tools; never fake a PKG on another OS."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def minimum_macos(wheels: Path, arch: str) -> tuple[int, int]:
    minimum = (13, 0)
    for wheel in wheels.glob("*.whl"):
        versions = [
            (int(major), int(minor))
            for major, minor, machine in re.findall(
                r"macosx_(\d+)_(\d+)_(arm64|x86_64|universal2)", wheel.name
            )
            if machine in (arch, "universal2")
        ]
        if versions:
            minimum = max(minimum, min(versions))
    return minimum


def bundle_info(name: str, version: str, minimum: str) -> dict:
    return {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": "com.varyzai." + ("uninstall" if "Uninstall" in name else "setup"),
        "CFBundleExecutable": "varyz-launch",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSUIElement": True,
        "LSMinimumSystemVersion": minimum,
    }


def launcher(removing: bool) -> str:
    option = " --uninstall" if removing else ""
    return (
        "#!/bin/sh\nset -eu\n"
        'resources=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)\n'
        'exec "$resources/payload/runtime/bin/python3" -I "$resources/launch.py"' + option + "\n"
    )


def assemble(stage: Path, runtime: Path, uv: Path, wheels: Path, arch: str) -> dict:
    if arch not in ("arm64", "x86_64"):
        raise ValueError("Unsupported macOS architecture.")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if stage.exists():
        raise ValueError("Use a new staging directory; release payloads are immutable.")
    if not (runtime / "bin/python3").is_file() or not uv.is_file():
        raise ValueError("A standalone macOS Python and uv executable are required.")
    if not any(wheels.glob("*.whl")):
        raise ValueError("The hash-locked architecture-specific wheelhouse is required.")
    minimum = minimum_macos(wheels, arch)
    minimum_text = f"{minimum[0]}.{minimum[1]}"
    apps = stage / "root/Applications"
    apps.mkdir(parents=True)
    contents = apps / "Varyz Setup.app/Contents"
    resources = contents / "Resources"
    payload = resources / "payload"
    payload.mkdir(parents=True)
    shutil.copytree(
        runtime,
        payload / "runtime",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"),
    )
    shutil.copy2(uv, payload / "uv")
    shutil.copy2(ROOT / "scripts/windows/native.py", payload / "native.py")
    shutil.copy2(ROOT / "src/varyz/release_upgrade.py", payload / "release_upgrade.py")
    for directory in ("package", "source", "wheelhouse"):
        (payload / directory).mkdir()
    wheel = ROOT / f"dist/varyzai-{version}-py3-none-any.whl"
    shutil.copy2(wheel, payload / "package" / wheel.name)
    shutil.copy2(ROOT / f"dist/varyzai-{version}.tar.gz", payload / "source")
    shutil.copytree(
        SOURCE,
        payload / "source/macos-installer",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for item in wheels.glob("*.whl"):
        shutil.copy2(item, payload / "wheelhouse" / item.name)
    for name in ("LICENSE", "NOTICE", "LICENSE.additional-terms"):
        shutil.copy2(ROOT / name, payload / name)
    for name in ("UV-LICENSE-MIT", "UV-LICENSE-APACHE"):
        shutil.copy2(uv.with_name(name), payload / name)
    if any(
        p.is_symlink() and not p.resolve().is_relative_to(payload.resolve())
        for p in payload.rglob("*")
    ):
        raise ValueError("Runtime symlinks must stay inside the packaged payload.")
    files = {
        p.relative_to(payload).as_posix(): sha256(p)
        for p in sorted(payload.rglob("*"))
        if p.is_file()
    }
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {
        "identity": "varyz-native-macos-v1",
        "version": version,
        "architecture": arch,
        "minimum_macos": minimum_text,
        "build_id": f"{version}-{fingerprint[:16]}",
        "wheel": "package/" + wheel.name,
        "files": files,
    }
    (payload / "payload.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(SOURCE / "launch.py", resources / "launch.py")
    (contents / "MacOS").mkdir()
    executable = contents / "MacOS/varyz-launch"
    executable.write_text(launcher(False), encoding="utf-8")
    executable.chmod(0o755)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(bundle_info("Varyz Setup", version, minimum_text))
    )
    # The remover reuses the setup app's runtime rather than duplicating the whole payload.
    remover = apps / "Varyz Uninstall.app/Contents"
    (remover / "MacOS").mkdir(parents=True)
    (remover / "Info.plist").write_bytes(
        plistlib.dumps(bundle_info("Varyz Uninstall", version, minimum_text))
    )
    script = remover / "MacOS/varyz-launch"
    script.write_text(
        '#!/bin/sh\nset -eu\nresources="/Applications/Varyz Setup.app/Contents/Resources"\n'
        'exec "$resources/payload/runtime/bin/python3" -I "$resources/launch.py" --uninstall\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    scripts = stage / "scripts"
    scripts.mkdir()
    preinstall = scripts / "preinstall"
    preinstall.write_text(
        "#!/bin/sh\nset -eu\n"
        "major=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d . -f 1)\n"
        "minor=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d . -f 2)\n"
        f'if [ "$major" -lt {minimum[0]} ] || '
        f'{{ [ "$major" -eq {minimum[0]} ] && [ "$minor" -lt {minimum[1]} ]; }}; then\n'
        f'  echo "Varyz requires macOS {minimum_text} or later." >&2\n  exit 1\nfi\n'
        'if [ "${3:-/}" != "/" ]; then\n'
        '  echo "Install Varyz on the running system volume." >&2\n  exit 1\nfi\n'
        f'if [ "$(uname -m)" != "{arch}" ]; then\n'
        '  echo "Choose the Varyz installer for this Mac architecture." >&2\n  exit 1\nfi\n'
        'if /usr/bin/pgrep -f "/Applications/Varyz Setup.app/Contents/Resources" >/dev/null; then\n'
        '  echo "Close Varyz Setup before replacing it." >&2\n  exit 1\nfi\n',
        encoding="utf-8",
    )
    preinstall.chmod(0o755)
    postinstall = scripts / "postinstall"
    postinstall.write_text(
        "#!/bin/sh\nset -eu\n"
        "user=$(/usr/bin/stat -f %Su /dev/console)\n"
        'case "$user" in root|loginwindow|_mbsetupuser|"") exit 0;; esac\n'
        'uid=$(/usr/bin/id -u "$user")\n'
        '/bin/launchctl asuser "$uid" /usr/bin/sudo -H -u "$user" '
        '/usr/bin/open -a "/Applications/Varyz Setup.app"\n',
        encoding="utf-8",
    )
    postinstall.chmod(0o755)
    (stage / "components.plist").write_bytes(
        plistlib.dumps(
            [
                {
                    "RootRelativeBundlePath": "Applications/" + name + ".app",
                    "BundleIsRelocatable": False,
                    "BundleOverwriteAction": "upgrade",
                }
                for name in ("Varyz Setup", "Varyz Uninstall")
            ]
        )
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin" or not shutil.which("pkgbuild"):
        raise SystemExit("Native PKG assembly requires macOS with Apple pkgbuild.")
    arch = platform.machine()
    if arch not in ("arm64", "x86_64"):
        raise SystemExit("Unsupported macOS architecture.")
    manifest = assemble(
        args.stage.resolve(),
        args.runtime.resolve(),
        args.uv.resolve(),
        args.wheelhouse.resolve(),
        arch,
    )
    artifact = ROOT / f"dist/VaryzSetup-{manifest['version']}-macos-{arch}.pkg"
    subprocess.run(
        [
            "pkgbuild",
            "--root",
            str(args.stage.resolve() / "root"),
            "--scripts",
            str(args.stage.resolve() / "scripts"),
            "--identifier",
            # Keep upgrades in /Applications even if an older app was moved.
            "com.varyzai.installer." + arch,
            "--version",
            manifest["version"],
            "--install-location",
            "/",
            "--ownership",
            "recommended",
            "--component-plist",
            str(args.stage.resolve() / "components.plist"),
            str(artifact),
        ],
        check=True,
    )
    subprocess.run(["pkgutil", "--check-signature", str(artifact)], check=False)
    (artifact.with_suffix(".sha256")).write_text(
        sha256(artifact) + "  " + artifact.name + "\n", encoding="utf-8"
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
