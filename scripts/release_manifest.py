"""Record commit and locked inputs beside private release candidate artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    package_dir = args.package_dir.resolve()
    artifacts = {
        path.relative_to(package_dir).as_posix(): digest(path)
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and path.name.endswith((".whl", ".tar.gz", ".pkg", ".exe", ".zip"))
    }
    if not artifacts:
        raise SystemExit("Build the release artifacts before recording provenance")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True
    ).strip())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"generated_at": datetime.now(UTC).isoformat(), "revision": revision,
             "working_tree_dirty": dirty, "lock_sha256": digest(ROOT / "uv.lock"),
             "artifacts": artifacts},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
