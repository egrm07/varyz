"""Apply only the plugin OAuth compatibility patch to an existing deployment."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def section(text, start, end):
    assert text.count(start) == 1
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def changes(onboarding=False):
    snapshot = (
        ".plugin-onboarding-remote-before.py" if onboarding else ".plugin-oauth-remote-before.py"
    )
    before = (ROOT / snapshot).read_text(encoding="utf-8-sig")
    after = (ROOT / "src/varyz/cloud/app.py").read_text(encoding="utf-8")
    if onboarding:
        ranges = [
            ("    async def mcp(request: Request):\n", '        result = {}\n'),
            ('            if combined:\n'
             '                history = archive_mcp(owner, message, True)["result"]["tools"]\n',
             "    def tool_error(identifier, text):\n"),
        ]
        return [(section(before, start, end), section(after, start, end))
                for start, end in ranges]
    ranges = [
        ('        computer_nonce = getattr(request.state, "computer_nonce", None)\n',
         "        if nonce:\n"),
        ("            if computer_nonce:\n",
         '        response.headers["Content-Security-Policy"] = policy\n'),
        ('        if resource == settings.public_url + "/mcp/plugin":\n',
         '        if data.get("scope") == "varyz.plugin" and resource !='),
        ("    def authorize_page(request: Request):\n",
         '        local = data["client_id"] == LOCAL_CLIENT\n'),
    ]
    return [(section(before, start, end), section(after, start, end))
            for start, end in ranges]


REMOTE = r'''
import json, pathlib, subprocess, time, urllib.error, urllib.request, uuid
patches = PATCHES
onboarding = ONBOARDING
root = pathlib.Path('/srv/varyz/src')
app = root / 'src/varyz/cloud/app.py'
old = app.read_bytes()
text = old.decode('utf-8')
for before, after in patches:
    if before == after:
        raise RuntimeError('No change in patch')
    if text.count(before) != 1:
        raise RuntimeError('Deployment anchor changed; no files changed')
    text = text.replace(before, after, 1)
compile(text, str(app), 'exec')
release = pathlib.Path('/srv/varyz/plugin-oauth-releases') / str(uuid.uuid4())
release.mkdir(parents=True, mode=0o700)
(release / 'app.py').write_bytes(old)
command = ['docker', 'compose', '-p', 'varyz-cloud', '--env-file',
           '/etc/varyz-cloud/cloud.env', '-f', str(root / 'deploy/cloud/compose.yaml'),
           '-f', str(root / 'deploy/cloud/compose.archive.yaml')]
old_image = subprocess.check_output(['docker', 'inspect', 'varyz-cloud-cloud-1',
                                    '--format={{.Image}}'], text=True).strip()
runtime_path = '/usr/local/lib/python3.12/site-packages/varyz/cloud/app.py'
runtime = subprocess.check_output(['docker', 'exec', 'varyz-cloud-cloud-1',
                                   'cat', runtime_path]).decode('utf-8')
assert runtime == old.decode('utf-8'), 'Running code differs; do not overwrite it'
base_tag = 'varyz-cloud:oauth-base-' + release.name
candidate_tag = 'varyz-cloud:oauth-candidate-' + release.name
def run(args):
    result = subprocess.run(args, capture_output=True, timeout=600)
    if result.returncode:
        (release / 'operation.log').write_bytes(result.stdout + result.stderr)
        raise RuntimeError('Container operation failed; protected log on server')
try:
    assert app.read_bytes() == old, 'Source changed during preparation'
    app.write_text(text, encoding='utf-8')
    # Reuse the deployed image's package and model cache. No dependencies,
    # environment secrets or database files enter this two-file image layer.
    (release / 'patched-app.py').write_text(text, encoding='utf-8')
    (release / 'Dockerfile').write_text(
        'FROM ' + base_tag + '\n'
        'COPY patched-app.py ' + runtime_path + '\n'
        'COPY patched-app.py /opt/varyz/src/varyz/cloud/app.py\n', encoding='utf-8')
    run(['docker', 'tag', old_image, base_tag])
    run(['docker', 'build', '--network=none', '-t', candidate_tag, str(release)])
    run(['docker', 'run', '--rm', '--network=none', candidate_tag, 'python', '-c',
         "import inspect, varyz.cloud.app; "
         "assert 'oauth_close_nonce' in inspect.getsource(varyz.cloud.app)"])
    run(['docker', 'tag', candidate_tag, 'varyz-cloud:local'])
    run(command + ['up', '-d', '--no-deps', '--no-build', 'cloud'])
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5) as response:
                assert response.status == 200
            try:
                request = urllib.request.Request(
                    'http://127.0.0.1:8080/oauth/authorize',
                    headers={'Host': 'cloud.varyzai.com'})
                urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as response:
                assert response.code == 400
                assert b'Varyz did not connect' in response.read()
                break
            raise RuntimeError('Missing OAuth validation')
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
    if onboarding:
        def rpc(method, params=None):
            body = json.dumps(dict(jsonrpc='2.0', id=1, method=method,
                                   params=params or {})).encode()
            request = urllib.request.Request('http://127.0.0.1:8080/mcp/plugin', data=body,
                headers={'Host': 'cloud.varyzai.com', 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)['result']
        tools = rpc('tools/list')['tools']
        assert len(tools) == 13
        assert all(tool['securitySchemes'] == [{'type': 'oauth2', 'scopes': ['varyz.plugin']}]
                   for tool in tools)
        denied = rpc('tools/call', dict(name='start_task', arguments={}))
        assert denied['isError'] and denied['_meta']['mcp/www_authenticate']
except BaseException:
    app.write_bytes(old)
    run(['docker', 'tag', old_image, 'varyz-cloud:local'])
    run(command + ['up', '-d', '--no-deps', '--no-build', 'cloud'])
    raise
print(json.dumps({'healthy': True, 'oauth_error_page': True,
                  'onboarding_verified': onboarding, 'backup': str(release)}))
'''


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--onboarding", action="store_true")
    args = parser.parse_args()
    patches = changes(args.onboarding)
    if not args.apply:
        print(f"Validated {len(patches)} targeted replacements; no deployment performed.")
        return 0
    script = REMOTE.replace("PATCHES", repr(patches), 1)
    script = script.replace("ONBOARDING", repr(args.onboarding), 1)
    result = subprocess.run(
        ["ssh", "-i", str(Path.home() / ".ssh/varyz_cloud_ed25519"),
         "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", "root@2.31.3.96",
         "python3 -c 'import sys; exec(sys.stdin.read())'"],
        input=script.encode("utf-8"), capture_output=True, timeout=900,
    )
    if result.returncode:
        print("Rollout failed; inspect protected server state. No raw server output relayed.")
        return 1
    print(json.dumps(json.loads(result.stdout)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
