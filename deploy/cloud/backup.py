"""Root-only consistent SQLite snapshots, encrypted to an off-host age identity."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

STATE = Path("/var/backups/varyz-cloud")
CONFIG = Path("/etc/varyz-cloud/backup.json")
PAUSED = Path("/etc/varyz-cloud/BACKUP_PAUSED")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    os.umask(0o077)
    print("VARYZ_BACKUP START " + datetime.now(UTC).isoformat(), flush=True)
    if PAUSED.exists():
        print("VARYZ_BACKUP PAUSED", flush=True)
        return
    config = json.loads(CONFIG.read_text())
    STATE.mkdir(mode=0o700, exist_ok=True)
    if shutil.disk_usage(STATE).free < 2 * Path(config["database"]).stat().st_size + 2 * 1024**3:
        raise RuntimeError("Backup disk safety floor reached")
    existing = sorted(STATE.glob("cloud-????-??-??.sqlite.age"), reverse=True)
    today = "cloud-" + datetime.now(UTC).date().isoformat() + ".sqlite.age"
    # Bound server disk even while the existing off-host receiver is unavailable.
    for old in existing[1:]:
        receipt = STATE / (old.name + ".received")
        if receipt.exists() and receipt.read_text().strip() == file_hash(old):
            old.unlink()
    if len(list(STATE.glob("cloud-????-??-??.sqlite.age"))) >= 2 and not (STATE / today).exists():
        raise RuntimeError("Off-host recovery verification is overdue; bounded backups retained")
    runtime = STATE / ".work"
    runtime.mkdir(mode=0o700, exist_ok=True)
    snapshot = runtime / "snapshot.sqlite"
    filename = "cloud-" + datetime.now(UTC).date().isoformat() + ".sqlite.age"
    target = STATE / filename
    partial = STATE / (filename + ".partial")
    try:
        with (
            closing(sqlite3.connect("file:" + config["database"] + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as backup,
        ):
            source.backup(backup, pages=256, sleep=0.01)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Snapshot integrity check failed")
            counts = {
                table: backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("accounts", "credentials", "records", "changes")
            }
        subprocess.run(
            [
                "/usr/local/bin/varyz-age",
                "--encrypt",
                "--recipient",
                config["recipient"],
                "--output",
                str(partial),
                str(snapshot),
            ],
            check=True,
        )
        partial.replace(target)
        environment = Path("/etc/varyz-cloud/cloud.env")
        key = (
            next(
                (
                    line.split("=", 1)[1]
                    for line in environment.read_text().splitlines()
                    if line.startswith("VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY=")
                ),
                "",
            )
            if environment.exists()
            else ""
        )
        if key:
            key_input = runtime / "archive-key"
            key_input.write_text(key)
            try:
                subprocess.run(
                    [
                        "/usr/local/bin/varyz-age",
                        "--encrypt",
                        "--recipient",
                        config["recipient"],
                        "--output",
                        str(STATE / "archive-key.age.partial"),
                        str(key_input),
                    ],
                    check=True,
                )
                (STATE / "archive-key.age.partial").replace(STATE / "archive-key.age")
            finally:
                key_input.unlink(missing_ok=True)
        metadata = {
            "filename": filename,
            "sha256": file_hash(target),
            "created": datetime.now(UTC).isoformat(),
            "counts": counts,
        }
        if key:
            metadata["archive_key_sha256"] = file_hash(STATE / "archive-key.age")
        temporary = STATE / "latest.json.partial"
        temporary.write_text(json.dumps(metadata))
        temporary.replace(STATE / "latest.json")
        # Keep two recent snapshots; remove older ones only after a verified
        # off-host restore acknowledged their exact encrypted checksum.
        for old in sorted(STATE.glob("cloud-????-??-??.sqlite.age"), reverse=True)[2:]:
            receipt = STATE / (old.name + ".received")
            if not receipt.exists() or receipt.read_text().strip() != file_hash(old):
                continue
            if old.resolve().parent != STATE.resolve():
                raise RuntimeError("Backup retention target escaped its directory")
            old.unlink()
        print("VARYZ_BACKUP SUCCESS " + filename, flush=True)
    finally:
        snapshot.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
