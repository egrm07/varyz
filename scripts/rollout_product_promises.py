"""Backed-up, compare-before-write local engine patch; leaves launchers and data intact."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "mcp_server.py",
    "hooks.py",
    "config.py",
    "session_progress.py",
    "resolution/knowledge.py",
    "resolution/principles.py",
    "resolution/trials.py",
    "resolution/briefing.py",
    "resolution/continuation.py",
    "resolution/verification.py",
    "cloud/app.py",
    "cloud/session_reports.py",
    "skills/varyz-context/SKILL.md",
)
PROFILES = {"full": FILES, "card": ("card/source.py",),
            "card-layout": ("card/layout.py",),
            "resume": ("mcp_server.py", "resolution/continuation.py"), "context": (
    "mcp_server.py", "resolution/continuation.py", "resolution/briefing_budget.py",
    # The prior app backport imports these modules. Include its dependency
    # closure so local Cloud startup cannot fail on a missing package module.
    "cloud/sharing_models.py", "cloud/sharing_routes.py", "cloud/sharing_service.py",
    "cloud/sharing_store.py", "cloud/sharing_ui.py",
)}


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def installed() -> Path:
    target = Path(os.environ["APPDATA"]) / "uv/tools/varyzai/Lib/site-packages/varyz"
    if not target.is_dir() or target.is_symlink() or target.is_junction():
        raise RuntimeError("Expected installed local engine is unavailable")
    return target.resolve()


def prepare(profile: str = "full") -> Path:
    target = installed()
    folder = ROOT / "data/promise-rollout" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    stage = folder / "runtime/varyz"
    shutil.copytree(target, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    rows = []
    for name in PROFILES[profile]:
        source = ROOT / "src/varyz" / name
        original = target / name
        if original.exists():
            backup = folder / "backup" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, backup)
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        rows.append({"file": name, "before": digest(original), "after": digest(destination)})
    manifest = folder / "manifest.json"
    records = list(target.parent.glob("varyzai-*.dist-info/RECORD"))
    if len(records) != 1:
        raise RuntimeError("Expected one installed distribution record")
    record_path = records[0]
    shutil.copy2(record_path, folder / "backup/RECORD")
    record_rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))))
    changed = {"varyz/" + row["file"]: row for row in rows}
    kept_rows = [row for row in record_rows if row[0] not in changed]
    for relative, row in changed.items():
        content = (stage / row["file"]).read_bytes()
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        kept_rows.append([relative, "sha256=" + encoded, str(len(content))])
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(kept_rows)
    patched_record = folder / "RECORD"
    patched_record.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    manifest.write_text(
        json.dumps(
            {
                "target": str(target),
                "files": rows,
                "status": "prepared",
                "profile": profile,
                "record": {
                    "path": str(record_path),
                    "before": digest(record_path),
                    "after": digest(patched_record),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"manifest": str(manifest), "runtime": str(stage.parent)}))
    return manifest


def apply(manifest: Path) -> None:
    folder = manifest.resolve().parent
    if not folder.is_relative_to((ROOT / "data/promise-rollout").resolve()):
        raise RuntimeError("Manifest must be inside the prepared workspace rollout")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    target = installed()
    if Path(value["target"]).resolve() != target or value["status"] != "prepared":
        raise RuntimeError("Unexpected target or rollout state")
    if [row["file"] for row in value["files"]] != list(PROFILES[value.get("profile", "full")]):
        raise RuntimeError("Unexpected rollout file list")
    record_path = Path(value["record"]["path"]).resolve()
    if record_path.parent.parent != target.parent or record_path.name != "RECORD":
        raise RuntimeError("Unexpected distribution record target")
    if digest(record_path) != value["record"]["before"]:
        raise RuntimeError("Distribution record changed since preparation")
    for row in value["files"]:
        if digest(target / row["file"]) != row["before"]:
            raise RuntimeError("Installation changed since preparation; prepare again")
        if digest(folder / "runtime/varyz" / row["file"]) != row["after"]:
            raise RuntimeError("Prepared source changed after verification")
    # No source deletion, launcher replacement, restart, or live database writes.
    replaced = []
    try:
        for row in value["files"]:
            destination = target / row["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".promise-update")
            shutil.copy2(folder / "runtime/varyz" / row["file"], temporary)
            os.replace(temporary, destination)
            replaced.append(row)
        shutil.copy2(folder / "RECORD", record_path)
    except Exception:
        for row in reversed(replaced):
            backup = folder / "backup" / row["file"]
            if backup.exists():
                shutil.copy2(backup, target / row["file"])
            elif row["before"] is None:
                (target / row["file"]).unlink(missing_ok=True)
        shutil.copy2(folder / "backup/RECORD", record_path)
        raise
    if any(digest(target / row["file"]) != row["after"] for row in value["files"]):
        raise RuntimeError("Installed file verification failed")
    value["status"] = "applied"
    value["applied_at"] = datetime.now(UTC).isoformat()
    manifest.write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"status": "applied", "manifest": str(manifest), "backup": str(folder / "backup")}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="full")
    args = parser.parse_args()
    apply(args.apply) if args.apply else prepare(args.profile)
