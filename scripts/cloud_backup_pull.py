"""Pull encrypted recovery snapshots and verify an isolated restore on Windows."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx

HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(state):
    if (state / "PAUSED").exists():
        return {"status": "paused"}
    config = json.loads((state / "config.json").read_text())
    ssh = [
        "ssh",
        "-i",
        config["ssh_key"],
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "root@" + config["host"],
    ]
    metadata = json.loads(
        subprocess.check_output(
            ssh + ["cat /var/backups/varyz-cloud/latest.json"], timeout=30, **HIDDEN
        )
    )
    filename = metadata["filename"]
    if not re.fullmatch(r"cloud-\d{4}-\d{2}-\d{2}\.sqlite\.age", filename):
        raise ValueError("Invalid backup filename")
    folder = state / "snapshots"
    folder.mkdir(exist_ok=True)
    target = folder / filename
    if not target.exists() or file_hash(target) != metadata["sha256"]:
        partial = folder / (filename + ".partial")
        with partial.open("wb") as output:
            subprocess.run(
                ssh + ["cat /var/backups/varyz-cloud/" + filename],
                stdout=output,
                check=True,
                timeout=300,
                **HIDDEN,
            )
        if file_hash(partial) != metadata["sha256"]:
            raise ValueError("Encrypted backup checksum mismatch")
        partial.replace(target)
    key_backup = state / "archive-key.age"
    if metadata.get("archive_key_sha256"):
        with key_backup.open("wb") as output:
            subprocess.run(
                ssh + ["cat /var/backups/varyz-cloud/archive-key.age"],
                stdout=output,
                check=True,
                timeout=30,
                **HIDDEN,
            )
        if file_hash(key_backup) != metadata["archive_key_sha256"]:
            raise ValueError("Archive recovery key checksum mismatch")
    restored = state / "restore-check.sqlite"
    try:
        subprocess.run(
            [
                config["age"],
                "--decrypt",
                "--identity",
                str(state / "identity.txt"),
                "--output",
                str(restored),
                str(target),
            ],
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            **HIDDEN,
        )
        with closing(sqlite3.connect("file:" + restored.as_posix() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database integrity check failed")
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("accounts", "credentials", "records", "changes")
            }
            if metadata.get("archive_key_sha256"):
                from cryptography.fernet import Fernet

                recovery_key = subprocess.check_output(
                    [
                        config["age"],
                        "--decrypt",
                        "--identity",
                        str(state / "identity.txt"),
                        str(key_backup),
                    ],
                    timeout=30,
                    **HIDDEN,
                ).strip()
                cipher = Fernet(recovery_key)
                archive_table = db.execute(
                    "SELECT name FROM sqlite_master WHERE name='archive_documents'"
                ).fetchone()
                if archive_table:
                    row = db.execute(
                        "SELECT payload FROM archive_documents "
                        "WHERE payload LIKE 'encrypted:%' LIMIT 1"
                    ).fetchone()
                    if row:
                        json.loads(cipher.decrypt(row[0].removeprefix("encrypted:").encode()))
                del recovery_key
            if counts != metadata["counts"]:
                raise ValueError("Restored database counts do not match the snapshot")
    finally:
        restored.unlink(missing_ok=True)
    subprocess.run(
        ssh + ["umask 077; cat > /var/backups/varyz-cloud/" + filename + ".received"],
        input=metadata["sha256"].encode(),
        check=True,
        timeout=30,
        **HIDDEN,
    )
    pending = (
        subprocess.check_output(
            ssh
            + [
                "find /var/backups/varyz-cloud -maxdepth 1 "
                "-name 'cloud-????-??-??.sqlite.age' -printf '%f\\n'"
            ],
            timeout=30,
            **HIDDEN,
        )
        .decode()
        .splitlines()
    )
    for name in pending:
        if name == filename:
            continue
        if not re.fullmatch(r"cloud-\d{4}-\d{2}-\d{2}\.sqlite\.age", name):
            raise ValueError("Invalid pending snapshot filename")
        checksum = (
            subprocess.check_output(
                ssh + ["sha256sum /var/backups/varyz-cloud/" + name], timeout=30, **HIDDEN
            )
            .decode()
            .split()[0]
        )
        local = folder / name
        if not local.exists() or file_hash(local) != checksum:
            with local.open("wb") as output:
                subprocess.run(
                    ssh + ["cat /var/backups/varyz-cloud/" + name],
                    stdout=output,
                    check=True,
                    timeout=300,
                    **HIDDEN,
                )
        if file_hash(local) != checksum:
            raise ValueError("Pending snapshot checksum mismatch")
        try:
            subprocess.run(
                [
                    config["age"],
                    "--decrypt",
                    "--identity",
                    str(state / "identity.txt"),
                    "--output",
                    str(restored),
                    str(local),
                ],
                check=True,
                timeout=300,
                stdout=subprocess.DEVNULL,
                **HIDDEN,
            )
            with closing(
                sqlite3.connect("file:" + restored.as_posix() + "?mode=ro", uri=True)
            ) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Pending restore integrity check failed")
        finally:
            restored.unlink(missing_ok=True)
        subprocess.run(
            ssh + ["umask 077; cat > /var/backups/varyz-cloud/" + name + ".received"],
            input=checksum.encode(),
            check=True,
            timeout=30,
            **HIDDEN,
        )
    created = datetime.fromisoformat(metadata["created"])
    if (datetime.now(UTC) - created).total_seconds() > 48 * 3600:
        raise ValueError("Server backup is more than 48 hours old")
    response = httpx.get("https://cloud.varyzai.com/health", timeout=15)
    response.raise_for_status()
    for old in sorted(folder.glob("cloud-????-??-??.sqlite.age"), reverse=True)[30:]:
        if old.resolve().parent != folder.resolve():
            raise ValueError("Retention target escaped snapshot directory")
        old.unlink()
    return {
        "status": "ok",
        "restore_verified": True,
        "backup_created": metadata["created"],
        "cloud_https_healthy": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    args.state.mkdir(exist_ok=True)
    log = args.state / "runs.jsonl"
    with log.open("a") as file:
        file.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": "START"}) + "\n")
    try:
        result = run(args.state)
    except Exception as error:
        # Do not include exception messages that might quote data or credentials.
        result = {"status": "failed", "error_type": type(error).__name__}
    result.update(at=datetime.now(UTC).isoformat(), event="END")
    (args.state / "status.json").write_text(json.dumps(result, indent=2))
    with log.open("a") as file:
        file.write(json.dumps(result) + "\n")
    print(json.dumps(result))
    raise SystemExit(1 if result["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
