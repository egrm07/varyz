"""Exercise the built wheel in a fresh environment with declared dependencies only.

The checkout remains the working directory, but neither its editable install nor
its optional Cloud/development dependencies are on the smoke interpreter's path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only cached dependencies")
    parser.add_argument("--package-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    package_dir = args.package_dir.resolve()
    wheels = list(package_dir.glob("varyzai-*.whl"))
    sources = list(package_dir.glob("varyzai-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit(
            "Package directory must contain exactly one release wheel and source archive"
        )
    with tempfile.TemporaryDirectory(prefix="varyz-release-wheel-") as temporary:
        folder = Path(temporary)
        environment = folder / "environment"
        subprocess.run(["uv", "venv", str(environment), "--python", "3.12"], check=True)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = ["uv", "pip", "install", "--python", str(python), str(wheels[0])]
        if args.offline:
            command.append("--offline")
        subprocess.run(command, check=True)
        # Windows can expose PATH with two differently-cased keys; keep one.
        env = {key: value for key, value in os.environ.items() if key.lower() != "path"}
        env.pop("PYTHONPATH", None)
        env["PATH"] = str(scripts) + os.pathsep + os.environ.get("PATH", "")
        env["UV_TOOL_DIR"] = str(folder / "tools")
        env["UV_TOOL_BIN_DIR"] = str(folder / "bin")
        env["PYTHONNOUSERSITE"] = "1"
        subprocess.run(
            [str(python), str(ROOT / "scripts/ci_clean_machine_smoke.py")],
            cwd=ROOT,
            env=env,
            check=True,
        )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    checksums = {}
    for package in [*wheels, *sources]:
        with package.open("rb") as stream:
            checksums[package.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True
    ).strip())
    (package_dir / "release-provenance.json").write_text(
        json.dumps({"revision": revision, "working_tree_dirty": dirty,
                    "packages": checksums}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
