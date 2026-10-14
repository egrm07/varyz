"""Publish the public OpenAI domain challenge through a narrowly scoped Caddy route."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
URL = "https://cloud.varyzai.com/.well-known/openai-apps-challenge"
REMOTE = r"""
import json, pathlib, subprocess, sys, time
data = json.load(sys.stdin)
config = pathlib.Path('/etc/caddy/Caddyfile')
old = config.read_text()
new = data['config']
block = (
    "    # Public publisher-ownership proof only; never put credentials in this folder.\n"
    "    handle /.well-known/openai-apps-challenge {\n"
    "        root * /var/lib/caddy/varyz-public\n        file_server\n    }\n"
    "    handle {\n        reverse_proxy 127.0.0.1:8080\n    }"
)
legacy = new.replace(block, "    reverse_proxy 127.0.0.1:8080")
previous_public_root = new.replace('/var/lib/caddy/varyz-public', '/etc/varyz-cloud/public')
if old not in (new, legacy, previous_public_root):
    raise SystemExit('Caddy configuration differs; stop rather than overwrite unrelated changes')
candidate = pathlib.Path('/etc/caddy/Caddyfile.plugin-candidate')
candidate.write_text(new)
subprocess.run(['caddy','validate','--config',str(candidate),'--adapter','caddyfile'], check=True)
proof = pathlib.Path('/var/lib/caddy/varyz-public/.well-known/openai-apps-challenge')
proof.parent.mkdir(parents=True, exist_ok=True)
proof.parent.parent.chmod(0o755)
proof.parent.chmod(0o755)
proof.write_text(data['token'])
proof.chmod(0o644)
backup = pathlib.Path('/etc/caddy/Caddyfile.plugin-backup-' + str(time.time_ns()))
backup.write_text(old)
config.write_text(new)
try:
    subprocess.run(['systemctl','reload','caddy'], check=True)
except BaseException:
    config.write_text(old)
    subprocess.run(['systemctl','reload','caddy'], check=True)
    raise
print('Caddy validated and reloaded; backup retained; public challenge only')
"""


def publish(token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", token):
        raise ValueError("Invalid public challenge format")
    data = json.dumps(
        {
            "token": token,
            "config": (ROOT / "deploy/cloud/Caddyfile.cloud").read_text(encoding="utf-8"),
        }
    )
    # The challenge is a public ownership proof, not an account or OAuth credential.
    runner = "exec(" + repr(REMOTE) + ")"
    subprocess.run(
        [
            "ssh",
            "-i",
            str(Path.home() / ".ssh/varyz_cloud_ed25519"),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "root@2.31.3.96",
            "python3 -c " + "'" + runner.replace("'", "'\"'\"'") + "'",
        ],
        input=data,
        text=True,
        check=True,
        timeout=45,
    )
    with httpx.Client(timeout=15) as client:
        response = client.get(URL)
        response.raise_for_status()
        if response.text != token:
            raise RuntimeError("Public challenge response does not match")
        client.get("https://cloud.varyzai.com/health").raise_for_status()
        denied = client.post(
            "https://cloud.varyzai.com/mcp/plugin",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        if denied.status_code != 401:
            raise RuntimeError("Unauthenticated MCP rejection failed")
    print("PASS public ownership challenge, Cloud health and unauthenticated MCP denial")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="Public publisher challenge from OpenAI")
    publish(parser.parse_args().token)
