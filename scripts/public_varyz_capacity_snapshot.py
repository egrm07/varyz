"""Retain only the exact source uploaded to this disposable candidate, never /probe."""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(run_id):
    assert re.fullmatch(r"varyz-capacity-[a-f0-9]{12}", run_id)
    output = ROOT / ".tmp/public-varyz-maximum-tested-source.tar"
    command = [
        "ssh",
        "-i",
        str(Path.home() / ".ssh/varyz_cloud_ed25519"),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "root@2.31.3.96",
        f"docker cp {run_id}:/opt/capacity/. -",
    ]
    with output.open("wb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, timeout=45)
    assert result.returncode == 0, result.stderr.decode()[-1000:]
    with tarfile.open(output) as archive:
        files = {}
        for item in archive.getmembers():
            assert not item.issym() and not item.islnk()
            name = item.name.removeprefix("./")
            assert not Path(name).is_absolute() and ".." not in Path(name).parts
            assert name in (".", "src", "scripts") or name.startswith(("src/", "scripts/"))
            if item.isfile():
                reader = archive.extractfile(item)
                assert reader is not None
                files[name] = reader.read()
        ordered = sorted(Path(n) for n in files if n.startswith("src/"))
        ordered += [
            Path("scripts") / n
            for n in ("public_varyz_capacity_server.py", "public_varyz_capacity_load.py")
        ]
        assert len(ordered) == len(files)
        digest = hashlib.sha256()
        for path in ordered:
            name = path.as_posix()
            digest.update(name.encode() + b"\0" + files[name])
    value = dict(
        run_id=run_id,
        source_sha256=digest.hexdigest(),
        files=len(files),
        archive_bytes=output.stat().st_size,
    )
    output.with_suffix(".json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    main(parser.parse_args().run_id)
