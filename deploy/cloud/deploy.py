"""Ship the working tree to the cloud server and (re)start the service.

Usage, from the repository root:

    uv run python deploy/cloud/deploy.py --host 203.0.113.10 [--live] [--dry-run]

It copies only the files the Dockerfile needs (the same allowlist as
Dockerfile.dockerignore), builds the image on the server, writes the runtime
environment from Windows Credential Manager (service ``varyz-cloud``) straight
into a root-only file on the server, and starts Compose. Secret values travel
over SSH stdin; they are never printed, logged or written locally.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import keyring

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "varyz-cloud"
KEY = Path.home() / ".ssh" / "varyz_cloud_ed25519"
REMOTE = "/srv/varyz/src"
ENV_FILE = "/etc/varyz-cloud/cloud.env"
INCLUDE = [
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "LICENSE.additional-terms",
    "NOTICE",
    "src",
    "deploy/cloud",
]
SKIP = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if set(Path(info.name).parts) & SKIP else info


def bundle() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in INCLUDE:
            path = ROOT / name
            tar.add(path, arcname=name, filter=keep)
    return buffer.getvalue()


def environment(live: bool) -> str:
    catalog = json.loads((ROOT / "deploy/cloud/paddle-catalog.json").read_text(encoding="utf-8"))
    mode = "live" if live else "sandbox"
    prices = catalog[mode]
    suffix = "" if live else "_SANDBOX"
    secret = lambda name: keyring.get_password(SERVICE, name + suffix) or ""  # noqa: E731
    values = {
        "VARYZ_CLOUD_PUBLIC_URL": "https://cloud.varyzai.com",
        "VARYZ_PADDLE_SANDBOX": "false" if live else "true",
        "VARYZ_PADDLE_PRICE_IDS": f"{prices['monthly']['price_id']},{prices['annual']['price_id']}",
        "VARYZ_PADDLE_API_KEY": secret("VARYZ_PADDLE_API_KEY"),
        "VARYZ_PADDLE_CLIENT_TOKEN": secret("VARYZ_PADDLE_CLIENT_TOKEN"),
        "VARYZ_PADDLE_WEBHOOK_SECRET": secret("VARYZ_PADDLE_WEBHOOK_SECRET"),
    }
    for name in ("HOST", "PORT", "USERNAME", "PASSWORD", "FROM"):
        value = keyring.get_password(SERVICE, f"VARYZ_CLOUD_SMTP_{name}")
        if value:
            values[f"VARYZ_CLOUD_SMTP_{name}"] = value
    for provider in ("GOOGLE", "GITHUB"):
        for kind in ("CLIENT_ID", "CLIENT_SECRET"):
            name = f"VARYZ_CLOUD_{provider}_{kind}"
            value = keyring.get_password(SERVICE, name)
            if value:
                values[name] = value
    analytics_token = keyring.get_password(SERVICE, "VARYZ_ANALYTICS_ADMIN_TOKEN")
    if analytics_token:
        values["VARYZ_ANALYTICS_ENABLED"] = "true"
        values["VARYZ_ANALYTICS_ADMIN_TOKEN"] = analytics_token
    present = sorted(k for k, v in values.items() if v)
    missing = sorted(k for k, v in values.items() if not v)
    print(f"mode: {mode}; set: {', '.join(present)}")
    if missing:
        print(f"empty (feature stays off): {', '.join(missing)}")
    return "".join(f"{k}={v}\n" for k, v in values.items())


# Secrets generated on the server (the --archive step creates the archive key) exist
# nowhere in Credential Manager. Rewriting the environment must carry them over, or
# every archive already stored becomes unreadable and signed-in pages fail.
# Sharing settings are also managed on the server, outside Credential Manager.
# Preserve explicit false values as well as enabled pilot configuration.
SERVER_ONLY = (
    "VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY",
    "SHARED_VARYZ_ENABLED",
    "SHARED_VARYZ_ACCOUNTS",
    "SHARED_VARYZ_CHATGPT_PLUGIN_URL",
)
WRITE_ENV = f"""import os, sys
from pathlib import Path
os.umask(0o077)
path = Path({ENV_FILE!r})
new = sys.stdin.read()
old = path.read_text().splitlines() if path.exists() else []
given = {{line.split("=", 1)[0] for line in new.splitlines() if "=" in line}}
kept = [
    line for line in old
    if line.split("=", 1)[0] in {SERVER_ONLY!r} and line.split("=", 1)[0] not in given
    and line.split("=", 1)[1:] != [""]
]
partial = path.with_suffix(".partial")
partial.write_text(new + "".join(line + "\\n" for line in kept))
partial.chmod(0o600)
partial.replace(path)
print("server-only values kept: " + (", ".join(line.split("=", 1)[0] for line in kept) or "none"))
"""


def ssh(host: str, command: str, stdin: bytes | None = None) -> None:
    subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "StrictHostKeyChecking=yes", f"root@{host}", command],
        input=stdin,
        check=True,
    )


def runs_archive_search(host: str) -> bool:
    """Whether the image now in service was built with the optional archive search."""
    found = subprocess.run(
        [
            "ssh",
            "-i",
            str(KEY),
            "-o",
            "StrictHostKeyChecking=yes",
            f"root@{host}",
            "docker run --rm --entrypoint test varyz-cloud:local -d /opt/varyz/models",
        ],
        check=False,
    )
    return found.returncode == 0


def retention_override(host: str) -> str:
    """Preserve an explicitly activated server logging policy across deploys."""
    path = "/etc/varyz-cloud/compose.retention.yaml"
    found = subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "StrictHostKeyChecking=yes", f"root@{host}",
         f"test -s {path}"],
        check=False,
    )
    if found.returncode not in (0, 1):
        found.check_returncode()
    return f" -f {path}" if found.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--live", action="store_true", help="use live Paddle IDs and secrets")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; contact nothing")
    parser.add_argument(
        "--keep-env", action="store_true", help="retain existing server secrets/mode"
    )
    parser.add_argument(
        "--archive", action="store_true", help="build and verify cached semantic context"
    )
    parser.add_argument(
        "--stage-only", action="store_true", help="verify candidate without replacing live service"
    )
    args = parser.parse_args()

    archive = bundle()
    if args.keep_env and args.live:
        parser.error("--keep-env cannot be combined with a billing mode change")
    env = None if args.keep_env else environment(args.live)
    print(f"bundle: {len(archive) // 1024} KiB")
    if args.dry_run:
        return 0

    compose_file = f"{REMOTE}/deploy/cloud/compose.yaml"
    compose = f"docker compose -p varyz-cloud --env-file {ENV_FILE} -f {compose_file}"
    if args.archive:
        compose += f" -f {REMOTE}/deploy/cloud/compose.archive.yaml"
    host = args.host
    ssh(host, "cloud-init status --wait >/dev/null; test -d /etc/varyz-cloud")
    retention = retention_override(host)
    compose += retention
    if not args.archive and runs_archive_search(host):
        parser.error("the server runs the archive search build; deploy with --archive")
    if env is not None:
        script = base64.b64encode(WRITE_ENV.encode()).decode()
        ssh(host, f"python3 -c 'import base64; exec(base64.b64decode(\"{script}\"))'", env.encode())
    else:
        ssh(host, f"test -s {ENV_FILE}")
    new, old = f"{REMOTE}.new", f"{REMOTE}.old"
    ssh(
        host,
        f"test '{new}' = /srv/varyz/src.new && rm -rf {new} && "
        f"mkdir -p {new} && tar -xzf - -C {new}",
        archive,
    )
    ssh(
        host,
        f"docker build --pull --build-arg VARYZ_CLOUD_SEARCH={int(args.archive)} "
        f"-t varyz-cloud:candidate -f {new}/deploy/cloud/Dockerfile {new}",
    )
    if args.archive:
        ssh(
            host,
            "docker run --rm -i --network none --memory 1536m --cpus 1.5 "
            "--read-only --tmpfs /tmp:size=512m,mode=1777 "
            "--entrypoint python varyz-cloud:candidate -",
            (ROOT / "deploy/cloud/verify_archive.py").read_bytes(),
        )
    if args.stage_only:
        print("candidate tested; production unchanged")
        return 0
    ssh(
        host,
        f"install -m 700 {new}/deploy/cloud/backup.py /usr/local/lib/varyz-cloud/backup.py; "
        f"install -m 644 {new}/deploy/cloud/varyz-cloud-backup.service "
        "/etc/systemd/system/varyz-cloud-backup.service; systemctl daemon-reload",
    )
    ssh(
        host,
        "docker tag varyz-cloud:local varyz-cloud:rollback; "
        "systemctl start varyz-cloud-backup.service",
    )
    if args.archive:
        provision = r"""from pathlib import Path
import base64, secrets, os
os.umask(0o077)
p=Path('/etc/varyz-cloud/cloud.env')
s=p.read_text()
name='VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY'
if not any(line.startswith(name+'=') and line.split('=',1)[1] for line in s.splitlines()):
    s='\n'.join(line for line in s.splitlines() if not line.startswith(name+'='))+'\n'
    p.write_text(s+name+'='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()+'\n')
p.chmod(0o600)
"""
        ssh(host, "python3 -", provision.encode())
    ssh(
        host,
        f"test '{old}' = /srv/varyz/src.old && rm -rf {old}; "
        f"if [ -d {REMOTE} ]; then mv {REMOTE} {old}; fi; mv {new} {REMOTE}",
    )
    try:
        ssh(
            host,
            f"docker tag varyz-cloud:candidate varyz-cloud:local && {compose} up -d --no-build",
        )
        ssh(
            host,
            "for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8080/health "
            "&& exit 0; sleep 2; done; exit 1",
        )
    except subprocess.CalledProcessError:
        ssh(
            host,
            f"docker tag varyz-cloud:rollback varyz-cloud:local && "
            f"docker compose -p varyz-cloud --env-file {ENV_FILE} "
            f"-f {old}/deploy/cloud/compose.yaml{retention} up -d --no-build",
        )
        raise
    print("\nservice healthy on the server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
