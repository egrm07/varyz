"""Roll out only task-coordination edits over the currently deployed Cloud image."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from coordination_rollout import payload  # noqa: E402

REMOTE = r'''
import json, pathlib, subprocess, time, urllib.request, uuid
data = PATCH_DATA
root = pathlib.Path('/srv/varyz/src')
package = root / 'src/varyz'
runtime = '/usr/local/lib/python3.12/site-packages/varyz/'
planned = {}
for name, replacements in data['replacements'].items():
    path = package / name
    old = path.read_bytes()
    running = subprocess.check_output(['docker', 'exec', 'varyz-cloud-cloud-1',
                                       'cat', runtime + name])
    assert running == old, 'Deployed source/runtime differ; no overwrite'
    text = old.decode('utf-8').replace('\r\n', '\n')
    for before, after in replacements:
        if after in text:
            continue
        assert text.count(before) == 1, 'Deployment anchor changed: ' + name + ' ' + repr(before)
        text = text.replace(before, after, 1)
    if text != old.decode('utf-8'):
        planned[name] = text
for name, text in data['additions'].items():
    path = package / name
    if path.exists():
        assert path.read_text(encoding='utf-8') == text, 'Different routing module exists'
    planned[name] = text
for name, text in planned.items():
    compile(text, name, 'exec')
release = pathlib.Path('/srv/varyz/coordination-releases') / str(uuid.uuid4())
release.mkdir(parents=True, mode=0o700)
old_files = {name: (package / name).read_bytes() if (package / name).exists() else None
             for name in planned}
for name, old in old_files.items():
    if old is not None:
        backup = release / 'backup' / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(old)
command = ['docker', 'compose', '-p', 'varyz-cloud', '--env-file',
           '/etc/varyz-cloud/cloud.env', '-f', str(root / 'deploy/cloud/compose.yaml'),
           '-f', str(root / 'deploy/cloud/compose.archive.yaml')]
old_image = subprocess.check_output(['docker', 'inspect', 'varyz-cloud-cloud-1',
                                    '--format={{.Image}}'], text=True).strip()
base_tag = 'varyz-cloud:coordination-base-' + release.name
candidate_tag = 'varyz-cloud:coordination-candidate-' + release.name
def run(args):
    result = subprocess.run(args, capture_output=True, timeout=600)
    if result.returncode:
        (release / 'operation.log').write_bytes(result.stdout + result.stderr)
        raise RuntimeError('Container operation failed; protected server log')
try:
    dockerfile = ['FROM ' + base_tag]
    for index, (name, text) in enumerate(planned.items()):
        candidate = release / ('patch-' + str(index) + '.py')
        candidate.write_text(text, encoding='utf-8')
        dockerfile.extend(['COPY ' + candidate.name + ' ' + runtime + name,
                           'COPY ' + candidate.name + ' /opt/varyz/src/varyz/' + name])
    (release / 'Dockerfile').write_text('\n'.join(dockerfile) + '\n', encoding='utf-8')
    run(['docker', 'tag', old_image, base_tag])
    run(['docker', 'build', '--network=none', '-t', candidate_tag, str(release)])
    # Exercise the patched HTTP MCP in an isolated account/database inside
    # the candidate container. No production account, token or archive is used.
    probe = r"""
import json, tempfile
from fastapi.testclient import TestClient
from varyz.cloud.app import Settings, create_app
with tempfile.TemporaryDirectory() as folder:
    app=create_app(Settings(database=folder+'/probe.sqlite', public_url='http://localhost'))
    store=app.state.store
    with store.connection() as db:
        db.execute('INSERT INTO accounts(id,email,password,status,email_verified) '
                   'VALUES(?,?,?,?,1)',
                   ('probe','probe@example.test','not-used','active'))
    token=store.credential('probe','access',3600,'varyz-local')
    client=TestClient(app,base_url='http://localhost')
    def rpc(name,args):
        response=client.post('/mcp',headers={'Authorization':'Bearer '+token},json={
            'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':name,'arguments':args}})
        assert response.status_code==200
        result=response.json()['result']
        assert not result.get('isError'), result
        return json.loads(result['content'][0]['text'])
    task={'project':'demo','conversation_id':'coordination-probe'}
    assert rpc('route_task',{**task,'action':'select','backend':'local'})['status']=='selected'
    assert rpc('ask',{**task,'question':'Which colour?'})['status']=='delegated'
    assert rpc('route_task',{**task,'action':'select','backend':'cloud',
        'expected_revision':1,'reason':'explicit_handoff'})['revision']==2
    delegated = rpc('ask',{**task,'question':'Which colour?','decision_kind':'delegated'})
    assert delegated['status'] == 'not_known' and delegated.get('agent_decides'), delegated
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) AS n FROM questions').fetchone()['n'] == 0
"""
    run(['docker', 'run', '--rm', '--network=none', candidate_tag, 'python', '-c', probe])
    for name, text in planned.items():
        path = package / name
        assert (path.read_bytes() if path.exists() else None) == old_files[name]
        path.write_text(text, encoding='utf-8')
    run(['docker', 'tag', candidate_tag, 'varyz-cloud:local'])
    run(command + ['up', '-d', '--no-deps', '--no-build', 'cloud'])
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5) as response:
                assert response.status == 200
            message = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'}).encode()
            request = urllib.request.Request('http://127.0.0.1:8080/mcp/plugin', data=message,
                headers={'Host':'cloud.varyzai.com','Content-Type':'application/json'})
            with urllib.request.urlopen(request, timeout=5) as response:
                tools = json.load(response)['result']['tools']
            assert 'route_task' in {tool['name'] for tool in tools}
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
except BaseException:
    for name, old in old_files.items():
        path = package / name
        if old is not None:
            path.write_bytes(old)
        elif path.exists():
            path.unlink()
    run(['docker','tag',old_image,'varyz-cloud:local'])
    run(command+['up','-d','--no-deps','--no-build','cloud'])
    raise
print(json.dumps({'healthy': True, 'routing_tool': True, 'candidate_mcp_verified': True,
                  'backup': str(release), 'patched_files': len(planned)}))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = payload("cloud")
    if not args.apply:
        print(json.dumps({"prepared": True, "files": len(data["replacements"]) + 1}))
        return 0
    script = REMOTE.replace("PATCH_DATA", repr(data), 1)
    result = subprocess.run([
        "ssh", "-i", str(Path.home() / ".ssh/varyz_cloud_ed25519"),
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "root@2.31.3.96", "python3 -c 'import sys; exec(sys.stdin.read())'",
    ], input=script.encode("utf-8"), capture_output=True, timeout=900)
    if result.returncode:
        diagnostic = ROOT / "data/coordination-rollout-error.log"
        diagnostic.write_bytes(result.stderr)
        print("Rollout did not complete; diagnostic retained locally.")
        return 1
    print(json.dumps(json.loads(result.stdout)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
