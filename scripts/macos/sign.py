"""Sign a staged Mac candidate; notarization remains a separate required gate.

Run only on a Mac with the approved identities in a temporary keychain. Never
publish an output solely because this command succeeded: require notarization,
stapling and clean native acceptance on these exact bytes before distribution.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MACHO = {bytes.fromhex(x) for x in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca",
    "cafebabf", "bfbafeca",
)}

LAUNCHER = r'''#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(void) {
    char raw[PATH_MAX], binary[PATH_MAX], resources[PATH_MAX];
    char python[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(raw);
    if (_NSGetExecutablePath(raw, &size) || !realpath(raw, binary)) return 1;
    char *slash = strrchr(binary, '/');
    if (!slash) return 1;
    *slash = 0;
#ifdef VARYZ_UNINSTALL
    if (snprintf(resources, sizeof(resources),
        "/Applications/Varyz Setup.app/Contents/Resources") >= sizeof(resources)) return 1;
#else
    if (snprintf(resources, sizeof(resources), "%s/../Resources", binary)
        >= sizeof(resources)) return 1;
#endif
    if (snprintf(python, sizeof(python), "%s/payload/runtime/bin/python3", resources)
        >= sizeof(python)) return 1;
    if (snprintf(script, sizeof(script), "%s/launch.py", resources) >= sizeof(script)) return 1;
#ifdef VARYZ_UNINSTALL
    char *args[] = {python, "-I", script, "--uninstall", NULL};
#else
    char *args[] = {python, "-I", script, NULL};
#endif
    execv(python, args);
    perror("Varyz launcher");
    return 1;
}
'''


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO


def sign_file(path: Path, identity: str, keychain: Path) -> None:
    # Permissions are restored after codesign rewrites the file.
    mode = path.stat().st_mode & 0o777
    path.chmod(mode | 0o200)
    try:
        subprocess.run([
            "codesign", "--force", "--sign", identity, "--keychain", str(keychain),
            "--options", "runtime", "--timestamp", str(path),
        ], check=True)
        subprocess.run(["codesign", "--verify", "--strict", str(path)], check=True)
    finally:
        path.chmod(mode)


def wheel_record(files: dict[str, bytes], record: str) -> bytes:
    """Rebuild RECORD after signing embedded native libraries, per the wheel spec."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        if name == record:
            writer.writerow([name, "", ""])
        else:
            data = files[name]
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            writer.writerow([name, "sha256=" + encoded.decode(), str(len(data))])
    return output.getvalue().encode()


def sign_wheel(wheel: Path, identity: str, keychain: Path) -> int:
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        files = {entry.filename: archive.read(entry) for entry in entries if not entry.is_dir()}
    native = [name for name, data in files.items() if data[:4] in MACHO]
    if not native:
        return 0
    records = [name for name in files if name.endswith(".dist-info/RECORD")]
    if len(records) != 1 or any(name.endswith(("RECORD.jws", "RECORD.p7s")) for name in files):
        raise ValueError("Cannot rewrite an ambiguous or externally signed wheel")
    with tempfile.TemporaryDirectory(prefix="varyz-sign-wheel-") as temporary:
        root = Path(temporary)
        for name in native:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise ValueError("Unsafe wheel member")
            path = root.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
            sign_file(path, identity, keychain)
            files[name] = path.read_bytes()
        files[records[0]] = wheel_record(files, records[0])
        rewritten = root / "signed.whl"
        with zipfile.ZipFile(rewritten, "w") as archive:
            for entry in entries:
                archive.writestr(entry, b"" if entry.is_dir() else files[entry.filename])
        # Keep the original wheel until every member has signed successfully.
        with rewritten.open("rb") as source, wheel.open("wb") as target:
            shutil.copyfileobj(source, target)
    return len(native)


def refresh_manifest(payload: Path) -> dict:
    manifest_path = payload / "payload.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("identity") != "varyz-native-macos-v1":
        raise ValueError("Not a staged Varyz Mac payload")
    files = {
        path.relative_to(payload).as_posix(): digest(path)
        for path in sorted(payload.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest.update(files=files, build_id=f"{manifest['version']}-{fingerprint[:16]}")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--keychain", type=Path, required=True)
    parser.add_argument("--application-identity", required=True)
    parser.add_argument("--installer-identity", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Signing requires Apple's native macOS tools")
    if args.output.exists():
        parser.error("Use a new output path; retained candidates are immutable")
    apps = args.stage / "root/Applications"
    setup = apps / "Varyz Setup.app"
    payload = setup / "Contents/Resources/payload"
    if not (payload / "payload.json").is_file():
        parser.error("The complete staged payload is required")
    with tempfile.TemporaryDirectory(prefix="varyz-launcher-") as temporary:
        source = Path(temporary) / "launcher.c"
        source.write_text(LAUNCHER)
        for name, removing in (("Varyz Setup.app", False), ("Varyz Uninstall.app", True)):
            binary = apps / name / "Contents/MacOS/varyz-launch"
            subprocess.run([
                "clang", "-Os", "-mmacosx-version-min=13.0", *(
                    ["-DVARYZ_UNINSTALL"] if removing else []
                ), str(source), "-o", str(binary),
            ], check=True)
    binaries = 0
    for path in sorted(payload.rglob("*")):
        if is_macho(path):
            sign_file(path, args.application_identity, args.keychain)
            binaries += 1
    libraries = sum(sign_wheel(wheel, args.application_identity, args.keychain)
                    for wheel in sorted((payload / "wheelhouse").glob("*.whl")))
    manifest = refresh_manifest(payload)
    # Python can create __pycache__ files even during a read-only payload check.
    # Check before sealing the outer bundle and forbid bytecode writes so that
    # the payload manifest and signed resource envelope describe the same files.
    subprocess.run(
        [str(payload / "runtime/bin/python3"), str(payload / "native.py"), "verify"],
        check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    for app in (setup, apps / "Varyz Uninstall.app"):
        subprocess.run([
            "codesign", "--force", "--sign", args.application_identity,
            "--keychain", str(args.keychain), "--options", "runtime", "--timestamp", str(app),
        ], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "pkgbuild", "--root", str(args.stage / "root"),
        "--scripts", str(args.stage / "scripts"),
        "--identifier", "com.varyzai.installer." + manifest["architecture"],
        "--version", manifest["version"], "--install-location", "/",
        "--ownership", "recommended", "--component-plist", str(args.stage / "components.plist"),
        "--sign", args.installer_identity, "--keychain", str(args.keychain), str(args.output),
    ], check=True)
    subprocess.run(["pkgutil", "--check-signature", str(args.output)], check=True)
    # Validate the actual packaged bundles, not just the staging directory.
    with tempfile.TemporaryDirectory(prefix="varyz-signed-package-") as temporary:
        expanded = Path(temporary) / "expanded"
        subprocess.run(["pkgutil", "--expand-full", str(args.output), str(expanded)], check=True)
        for name in ("Varyz Setup.app", "Varyz Uninstall.app"):
            packaged = list(expanded.rglob(name))
            if len(packaged) != 1:
                raise ValueError("The signed package must contain exactly one " + name)
            subprocess.run([
                "codesign", "--verify", "--deep", "--strict", str(packaged[0]),
            ], check=True)
    receipt = {"artifact": args.output.name, "sha256": digest(args.output),
               "build_id": manifest["build_id"], "signed_runtime_binaries": binaries,
               "signed_wheel_libraries": libraries, "notarized": False,
               "distribution_ready": False}
    args.output.with_suffix(".signing.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
