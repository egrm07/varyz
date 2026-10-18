"""Build an unsigned acceptance EXE on a disposable Windows Actions runner."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def run(*args: str) -> str:
    return subprocess.check_output(list(args), text=True).strip()


def main() -> None:
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use only a disposable GitHub Windows runner")
    if os.environ.get("RUNNER_OS") != "Windows":
        raise SystemExit("Windows runner required")
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    folder = root / ".build-cache/native-ci"
    if folder.exists():
        raise SystemExit("Use a fresh acceptance workspace")
    folder.mkdir(parents=True)
    compiler = Path(os.environ["PROGRAMFILES(X86)"]) / "Inno Setup 6/ISCC.exe"
    if not compiler.is_file():
        raise SystemExit("Official runner image lacks the required Inno Setup compiler")
    uv = shutil.which("uv")
    if not uv or not run(uv, "--version").startswith("uv 0.11.2 "):
        raise SystemExit("The reviewed uv version is required")
    runtime = Path(run(uv, "python", "find", "--system", "3.12")).parent
    shutil.copyfile(uv, folder / "uv.exe")
    hashes = {
        "MIT": "860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c",
        "APACHE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    }
    for kind, expected in hashes.items():
        url = f"https://raw.githubusercontent.com/astral-sh/uv/0.11.2/LICENSE-{kind}"
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise SystemExit("Bundled uv licence integrity failed")
        (folder / f"UV-LICENSE-{kind}").write_bytes(content)
    requirements = folder / "requirements.txt"
    run(
        uv,
        "export",
        "--frozen",
        "--no-dev",
        "--no-default-groups",
        "--no-emit-project",
        "--output-file",
        str(requirements),
    )
    wheels = folder / "wheelhouse"
    wheels.mkdir()
    run(
        uv,
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--with",
        "pip",
        "python",
        "-m",
        "pip",
        "download",
        "--require-hashes",
        "--only-binary=:all:",
        "--dest",
        str(wheels),
        "-r",
        str(requirements),
    )
    packages = folder / "packages"
    run(uv, "build", "--no-sources", "--out-dir", str(packages))
    output = root / "dist/native-ci"
    run(
        sys.executable,
        "scripts/windows/build.py",
        "--runtime",
        str(runtime),
        "--uv",
        str(folder / "uv.exe"),
        "--wheelhouse",
        str(wheels),
        "--stage",
        str(folder / "stage"),
        "--compiler",
        str(compiler),
        "--package-dir",
        str(packages),
        "--output-dir",
        str(output),
    )
    run(
        sys.executable,
        "scripts/release_manifest.py",
        "--package-dir",
        str(output),
        "--output",
        str(output / "native-release-provenance.json"),
    )


if __name__ == "__main__":
    main()
