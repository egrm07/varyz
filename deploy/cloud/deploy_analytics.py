"""Surgical analytics rollout, preserving unrelated deployed code and secrets."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY = Path.home() / ".ssh" / "varyz_cloud_ed25519"
FILES = ("src/varyz/analytics_schema.py", "src/varyz/cloud/analytics.py")
REMOTE_SCRIPT = r"""
import hashlib, json, os, pathlib, secrets, shutil, subprocess, time, urllib.request, uuid
files = FILE_DATA
root = pathlib.Path('/srv/varyz/src')
env = pathlib.Path('/etc/varyz-cloud/cloud.env')
assert root.is_dir() and env.is_file(), 'Existing deployment is required'
app = root / 'src/varyz/cloud/app.py'
compose = root / 'deploy/cloud/compose.yaml'
app_text = app.read_text()
compose_text = compose.read_text()
def insert_once(text, anchor, replacement, marker):
    if marker in text:
        return text
    assert text.count(anchor) == 1, 'Deployment anchor changed: ' + marker
    return text.replace(anchor, replacement, 1)
app_text = insert_once(app_text, 'class Settings:\n',
    'class Settings:\n    analytics_enabled: bool = False\n'
    '    analytics_admin_token: str = ""\n',
    '    analytics_enabled: bool')
app_text = insert_once(app_text, '    def from_env(cls):\n        return cls(\n',
    '    def from_env(cls):\n        return cls(\n'
    '            analytics_enabled=os.getenv("VARYZ_ANALYTICS_ENABLED", "false")'
    '.lower() == "true",\n'
    '            analytics_admin_token=os.getenv("VARYZ_ANALYTICS_ADMIN_TOKEN", ""),\n',
    '            analytics_enabled=os.getenv')
app_text = insert_once(app_text, '    app.state.settings = settings\n',
    '    app.state.settings = settings\n    from .analytics import register_analytics\n'
    '    register_analytics(app, settings, store)\n',
    '    register_analytics(app, settings, store)')
compose_text = insert_once(compose_text, '    environment:\n',
    '    environment:\n      VARYZ_ANALYTICS_ENABLED: ${VARYZ_ANALYTICS_ENABLED:-false}\n'
    '      VARYZ_ANALYTICS_ADMIN_TOKEN: ${VARYZ_ANALYTICS_ADMIN_TOKEN:-}\n',
    '      VARYZ_ANALYTICS_ENABLED:')
compile(app_text, str(app), 'exec')
for name, text in files.items():
    assert name in ('src/varyz/analytics_schema.py', 'src/varyz/cloud/analytics.py')
    compile(text, name, 'exec')
folder = pathlib.Path('/srv/varyz/analytics-releases') / str(uuid.uuid4())
folder.mkdir(parents=True, mode=0o700)
targets = {root / name: text for name, text in files.items()}
targets.update({app: app_text, compose: compose_text})
old_files = {}
for path in targets:
    old_files[path] = path.read_bytes() if path.exists() else None
    if path.exists():
        backup = folder / path.relative_to(root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
old_env = env.read_bytes()
shutil.copy2(env, folder / 'cloud.env')
values = dict(line.split('=', 1) for line in old_env.decode().splitlines()
              if '=' in line and not line.lstrip().startswith('#'))
token = values.get('VARYZ_ANALYTICS_ADMIN_TOKEN') or secrets.token_urlsafe(48)
assert len(token) >= 32 and '\n' not in token
lines = [line for line in old_env.decode().splitlines()
         if not line.startswith(('VARYZ_ANALYTICS_ENABLED=', 'VARYZ_ANALYTICS_ADMIN_TOKEN='))]
lines += ['VARYZ_ANALYTICS_ENABLED=true', 'VARYZ_ANALYTICS_ADMIN_TOKEN=' + token]
command = ['docker', 'compose', '-p', 'varyz-cloud', '--env-file', str(env), '-f', str(compose)]
def run(*args):
    completed = subprocess.run(command + list(args), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=600)
    assert completed.returncode == 0, 'Container operation failed; rollout was rolled back'
try:
    for path, text in targets.items():
        path.write_text(text)
    env.write_text('\n'.join(lines) + '\n')
    os.chmod(env, 0o600)
    run('build', 'cloud')
    run('up', '-d', '--no-deps', 'cloud')
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5) as response:
                assert response.status == 200
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
except BaseException:
    for path, content in old_files.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    env.write_bytes(old_env)
    run('build', 'cloud')
    run('up', '-d', '--no-deps', 'cloud')
    raise
print(json.dumps({'token': token, 'backup': str(folder), 'healthy': True}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--apply", action="store_true", help="Deploy and store owner credentials")
    args = parser.parse_args()
    ipaddress.ip_address(args.host)
    files = {name: (ROOT / name).read_text(encoding="utf-8") for name in FILES}
    for name, content in files.items():
        compile(content, name, "exec")
    if not args.apply:
        print("Dry run: update two analytics modules, Cloud registration and Compose variables.")
        print("Preserve existing secrets/code/data; enable receiver; store owner token privately.")
        return 0
    import keyring

    script = REMOTE_SCRIPT.replace("FILE_DATA", repr(files), 1)
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(KEY),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "root@" + args.host,
            "python3 -c 'import sys; exec(sys.stdin.read())'",
        ],
        input=script.encode(),
        capture_output=True,
        timeout=1200,
    )
    if result.returncode:
        # Never relay raw subprocess output: environment/provider output can contain secrets.
        print(
            "Analytics rollout failed. Inspect protected server state; changed files were restored."
        )
        return 1
    data = json.loads(result.stdout)
    keyring.set_password("varyz-cloud", "VARYZ_ANALYTICS_ADMIN_TOKEN", data.pop("token"))
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
