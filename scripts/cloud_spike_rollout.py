"""Surgical, reversible account-cap/backpressure rollout based on actual live source."""

import argparse
import base64
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/backpressure"
SSH = [
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
    "python3 -c 'import sys;exec(sys.stdin.read())'",
]
COMMON = r"""
import base64,hashlib,json,os,re,subprocess,time,urllib.request
from pathlib import Path
container='varyz-cloud-cloud-1'
package='/usr/local/lib/python3.12/site-packages/varyz/cloud/'
def run(*args,timeout=120):
 return subprocess.check_output(list(args),stderr=subprocess.PIPE,timeout=timeout)
def health():
 with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as r:
  assert r.status==200
info=json.loads(run('docker','inspect',container))[0]
"""


def remote(code, timeout=300):
    OUT.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        SSH, input=(COMMON + code).encode(), capture_output=True, timeout=timeout
    )
    if result.returncode:
        (OUT / "diagnostic.txt").write_bytes(result.stderr + result.stdout)
        raise RuntimeError("Remote check failed; see .tmp/backpressure/diagnostic.txt")
    return json.loads(result.stdout)


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2), encoding="utf-8")


def inspect():
    value = remote(r"""
loaded=run('docker','exec',container,'python','-c',
 "import varyz.cloud.app;print(varyz.cloud.app.__file__)").decode().strip()
assert loaded==package+'app.py',loaded
files={}
for name in ('app.py','store.py'):
 value=run('docker','exec',container,'cat',package+name)
 assert value==run('docker','exec',container,'cat','/opt/varyz/src/varyz/cloud/'+name)
 files[name]=base64.b64encode(value).decode()
health()
print(json.dumps({'image':info['Image'],'image_tag':info['Config']['Image'],
 'cmd':info['Config']['Cmd'],'files':files,
 'compose':info['Config']['Labels']['com.docker.compose.project.config_files'],
 'limits':{k:info['HostConfig'][k] for k in ('NanoCpus','Memory','MemorySwap','PidsLimit')},
 'environment_sha256':hashlib.sha256(json.dumps(sorted(info['Config']['Env'])).encode()).hexdigest()}))
""")
    save("baseline.json", value)
    print("Captured loaded live source and configuration hashes; no production changes")


def replace(value, before, after):
    assert value.count(before) == 1, before[:90]
    return value.replace(before, after, 1)


def prepare():
    baseline = json.loads((OUT / "baseline.json").read_text())
    files = {
        name: base64.b64decode(value).decode().replace("\r\n", "\n")
        for name, value in baseline["files"].items()
    }
    app = files["app.py"]
    app = replace(
        app,
        "from .billing import",
        "from .admission import Admission, Backpressure\nfrom .billing import",
    )
    app = replace(app, "max_accounts: int = 1000", "max_accounts: int = 10000")
    app = replace(
        app,
        'os.getenv("VARYZ_CLOUD_MAX_ACCOUNTS", "1000")',
        'os.getenv("VARYZ_CLOUD_MAX_ACCOUNTS", "10000")',
    )
    app = replace(
        app,
        "    async def guards(request, call_next):\n",
        "    async def guards(request, call_next):\n"
        '        if request.url.path == "/health":\n'
        "            return await call_next(request)\n",
    )
    for detail in ("Service rate limit", "Rate limit"):
        app = replace(
            app,
            f'return JSONResponse({{"detail": "{detail}"}}, status_code=429)',
            f'return JSONResponse({{"detail": "{detail}"}}, status_code=429,\n'
            '                    headers={"Retry-After": str(60 - int(time.time()) % 60),\n'
            '                             "X-Varyz-Retry-Safe": "1"})',
        )
    app = replace(
        app,
        "        deadline = loop.time() + wait\n",
        '        lease = request.scope.get("varyz_admission")\n'
        "        if lease is not None:\n"
        "            lease.release()\n"
        "        deadline = loop.time() + wait\n",
    )
    assert app.endswith("    return app\n")
    files["app.py"] = app.removesuffix("    return app\n") + (
        "    app.state.admission = Admission()\n"
        "    app.add_middleware(Backpressure, gate=app.state.admission)\n    return app\n"
    )
    files["store.py"] = replace(
        files["store.py"],
        'if db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] >= 10000:',
        'if not row and db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] >= 10000:',
    )
    files["admission.py"] = (ROOT / "src/varyz/cloud/admission.py").read_text()
    for name, value in files.items():
        compile(value, name, "exec")
        (OUT / ("candidate-" + name)).write_bytes(value.encode())
    manifest = {
        "id": "spike-" + uuid.uuid4().hex[:12],
        "baseline": baseline,
        "files": {name: base64.b64encode(value.encode()).decode() for name, value in files.items()},
        "hashes": {
            name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()
        },
        "probe": base64.b64encode((ROOT / "scripts/cloud_spike_probe.py").read_bytes()).decode(),
    }
    save("manifest.json", manifest)
    print(json.dumps({"id": manifest["id"], "hashes": manifest["hashes"]}))


def stage():
    data = json.loads((OUT / "manifest.json").read_text())
    value = remote(
        "data="
        + repr(data)
        + "\n"
        + r"""
assert info['Image']==data['baseline']['image'],'Live image changed'
for name,value in data['baseline']['files'].items():
 assert run('docker','exec',container,'cat',package+name)==base64.b64decode(value)
release=Path('/opt/varyz-spike-releases')/data['id']
release.mkdir(parents=True,mode=0o700)
image='varyz-cloud:'+data['id']
rollback=image+'-base'
run('docker','tag',info['Image'],rollback)
lines=['FROM '+rollback]
for name,value in data['files'].items():
 target=release/'payload'/name
 target.parent.mkdir(exist_ok=True)
 target.write_bytes(base64.b64decode(value))
 lines+=['COPY payload/'+name+' '+package+name,
         'COPY payload/'+name+' /opt/varyz/src/varyz/cloud/'+name]
cmd=['sh','-c','umask 077; exec python -m uvicorn varyz.cloud.app:create_app --factory '
 '--host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --limit-concurrency 128 '
 '--backlog 128 --timeout-keep-alive 2 --no-access-log --proxy-headers']
lines+=['CMD '+json.dumps(cmd)]
(release/'Dockerfile').write_text('\n'.join(lines)+'\n')
(release/'probe.py').write_bytes(base64.b64decode(data['probe']))
(release/'manifest.json').write_text(json.dumps(data,indent=2))
run('docker','build','--network=none','-t',image,str(release),timeout=180)
results=[]
for repetition,test_image in enumerate([rollback,image,image]):
 name=data['id']+'-test'+str(repetition)
 directory=release/name
 directory.mkdir(mode=0o700)
 os.chown(directory,10001,10001)
 log=(release/(name+'.log')).open('wb')
 args=['docker','run','--name',name,'--network=none','--read-only',
  '--cpus',str(info['HostConfig']['NanoCpus']/1e9),'--memory',str(info['HostConfig']['Memory']),
  '--memory-swap',str(info['HostConfig']['Memory']),'--pids-limit','128','--cap-drop=ALL',
  '--security-opt','no-new-privileges:true','--tmpfs','/tmp:size=32m,mode=1777',
  '-v',str(directory)+':/probe:rw','-v',str(release/'probe.py')+':/probe.py:ro',
  '--entrypoint','python',test_image,'/probe.py']
 process=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
 try:
  start=time.monotonic()
  while process.poll() is None:
   health()
   available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:')))*1024
   assert available>512*1024**2,'Host memory guard'
   assert time.monotonic()-start<120,'Stage deadline'
   time.sleep(1)
  assert process.returncode==0,'Stage failed; '+str(release/(name+'.log'))
  results.append(json.loads((directory/'result.json').read_text()))
 finally:
  if process.poll() is None:
   subprocess.run(['docker','stop','--time','5',name],capture_output=True,timeout=15)
   process.wait(timeout=15)
  log.close()
  subprocess.run(['docker','rm',name],check=True,capture_output=True)
health()
result={'id':data['id'],'image':image,'image_id':run('docker','image','inspect',image,
 '--format={{.Id}}').decode().strip(),'rollback':rollback,'results':results,
 'limits':data['baseline']['limits'],'live_changed':False,'tested':True}
(release/'stage.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
""",
        timeout=500,
    )
    save("stage.json", value)
    print(json.dumps(value))


def apply():
    data = json.loads((OUT / "manifest.json").read_text())
    stage_result = json.loads((OUT / "stage.json").read_text())
    assert stage_result["id"] == data["id"] and stage_result["tested"]
    value = remote(
        "data="
        + repr(data)
        + "\n"
        + r'''
release=Path('/opt/varyz-spike-releases')/data['id']
stage=json.loads((release/'stage.json').read_text())
assert stage['tested'] and len(stage['results'])==3
assert all(r['candidate'] and r['verified_sync_writes']==192 and
 r['database_integrity']=='ok' for r in stage['results'][1:])
assert run('docker','image','inspect',stage['image'],
 '--format={{.Id}}').decode().strip()==stage['image_id']
assert info['Image']==data['baseline']['image'],'Live image changed'
for name,value in data['baseline']['files'].items():
 assert run('docker','exec',container,'cat',package+name)==base64.b64decode(value)
configs=info['Config']['Labels']['com.docker.compose.project.config_files'].split(',')
primary=Path(configs[0])
assert str(primary)=='/srv/varyz/src/deploy/cloud/compose.yaml'
before=primary.read_bytes()
updated=before.replace(b'\r\n',b'\n')
old=('    image: '+info['Config']['Image']).encode()
assert updated.count(old)==1
updated=updated.replace(old,('    image: '+stage['image']).encode(),1)
pattern=b'^      VARYZ_CLOUD_MAX_ACCOUNTS:.*$'
assert len(re.findall(pattern,updated,re.M))==1
updated=re.sub(pattern,b'      VARYZ_CLOUD_MAX_ACCOUNTS: "10000"',updated,flags=re.M)
(release/'compose.before.yaml').write_bytes(before)
(release/'compose.after.yaml').write_bytes(updated)
backup_code="""import sqlite3
from pathlib import Path
from varyz.cloud.app import Settings
database=Path(Settings.from_env().database)
backup=database.parent/"BACKUP_NAME"
assert not backup.exists()
with sqlite3.connect(str(database)) as source, sqlite3.connect(str(backup)) as target:
 source.backup(target)
 assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
backup.chmod(0o600)
print(str(backup))
""".replace('BACKUP_NAME','.'+data['id']+'.before.sqlite')
backup_path=run('docker','exec',container,'python','-c',backup_code).decode().strip()
assert backup_path.endswith('/.'+data['id']+'.before.sqlite')
run('docker','cp',container+':'+backup_path,str(release/'database.before.sqlite'))
(release/'database.before.sqlite').chmod(0o600)
run('docker','exec',container,'rm',backup_path)
compose=['docker','compose','--project-name','varyz-cloud','--env-file','/etc/varyz-cloud/cloud.env']
for config in configs: compose+=['-f',config]
def up():
 operation=subprocess.run(compose+['up','-d','--no-build','cloud'],capture_output=True,timeout=90)
 (release/'deployment.log').write_bytes(operation.stdout+operation.stderr)
 operation.check_returncode()
def verify():
 current=json.loads(run('docker','inspect',container))[0]
 assert current['Image']==stage['image_id']
 assert current['HostConfig']['NanoCpus']==info['HostConfig']['NanoCpus']
 assert current['HostConfig']['Memory']==info['HostConfig']['Memory']
 old_env=dict(item.split('=',1) for item in info['Config']['Env'])
 new_env=dict(item.split('=',1) for item in current['Config']['Env'])
 assert new_env['VARYZ_CLOUD_MAX_ACCOUNTS']=='10000'
 old_env.pop('VARYZ_CLOUD_MAX_ACCOUNTS',None)
 new_env.pop('VARYZ_CLOUD_MAX_ACCOUNTS',None)
 assert old_env==new_env,'Unrelated environment changed'
 for name,sha in data['hashes'].items():
  actual=run('docker','exec',container,'cat',package+name)
  assert hashlib.sha256(actual).hexdigest()==sha
 code="""import json
from varyz.cloud.app import Settings,create_app
app=create_app(Settings.from_env())
s=app.state.settings
with app.state.store.connection() as db:
 count=db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
 check=db.execute('PRAGMA quick_check').fetchone()[0]
print(json.dumps({'max_accounts':s.max_accounts,'accounts':count,'integrity':check,
 'storage_limit':s.storage_limit,'change_limit':s.change_limit,'archive_limit':s.archive_limit,
 'public_creator_routes':any(r.path.startswith('/@') for r in app.routes),
 'admission':{k:getattr(app.state.admission,k) for k in
 ['workers','queue_limit','per_client','per_peer','client_workers','timeout']}}))
"""
 result=json.loads(run('docker','exec',container,'python','-c',code))
 assert result['max_accounts']==10000 and result['integrity']=='ok'
 assert not result['public_creator_routes']
 with urllib.request.urlopen('https://cloud.varyzai.com/health',timeout=5) as r:
  assert r.status==200
 url='https://cloud.varyzai.com/.well-known/oauth-protected-resource'
 with urllib.request.urlopen(url,timeout=5) as r:
  assert r.status==200 and r.headers.get('x-varyz-queue-ms') is not None
 return result
try:
 primary.write_bytes(updated)
 up()
 for attempt in range(40):
  try:
   health()
   break
  except Exception: time.sleep(1)
 else: raise RuntimeError('Readiness failed')
 verified=verify()
except Exception:
 primary.write_bytes(before)
 up()
 health()
 raise
result={'id':data['id'],'image':stage['image'],'image_id':stage['image_id'],
 'verified':verified,'rollback_image':stage['rollback'],
 'rollback_compose':str(release/'compose.before.yaml'),
 'database_backup':str(release/'database.before.sqlite'),
 'environment_preserved_except_account_cap':True,'source_hashes':data['hashes'],
 'deployed_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
(release/'deployment.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
'''
    )
    save("deployment.json", value)
    print(json.dumps(value))


def status():
    data = json.loads((OUT / "manifest.json").read_text())
    print(
        json.dumps(
            remote(
                "data="
                + repr({"id": data["id"]})
                + "\n"
                + r"""
release=Path('/opt/varyz-spike-releases')/data['id']
logs={p.name:p.read_text()[-2500:] for p in release.glob('*-test*.log')}
print(json.dumps({'logs':logs,'containers':run('docker','ps','--filter',
 'name='+data['id'],'--format','{{.Names}} {{.Status}}').decode()}))
"""
            )
        )
    )


def verify():
    data = json.loads((OUT / "manifest.json").read_text())
    value = remote(
        "data="
        + repr(data)
        + "\n"
        + r"""
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
release=Path('/opt/varyz-spike-releases')/data['id']
deployment=json.loads((release/'deployment.json').read_text())
assert info['Image']==deployment['image_id']
for k,v in data['baseline']['limits'].items():
 assert info['HostConfig'][k]==v,k
assert not info['State']['OOMKilled'] and info['RestartCount']==0
for name,expected in data['hashes'].items():
 assert hashlib.sha256(run('docker','exec',container,'cat',package+name)).hexdigest()==expected
samples=[]
for i in range(12):
 began=time.monotonic()
 with urllib.request.urlopen('https://cloud.varyzai.com/health',timeout=5) as r:
  assert r.status==200
 samples.append(round((time.monotonic()-began)*1000,2))
 time.sleep(1)
def probe(i):
 request=urllib.request.Request('http://127.0.0.1:8080/.well-known/oauth-protected-resource',
  headers={'Host':'cloud.varyzai.com'})
 try:
  response=urllib.request.urlopen(request,timeout=15)
 except HTTPError as error: response=error
 with response:
  assert response.status in (200,503)
  if response.status==503:
   assert response.headers.get('X-Varyz-Retry-Safe')=='1'
   assert int(response.headers['Retry-After'])>0
  else: assert response.headers.get('X-Varyz-Queue-Ms') is not None
  return {'status':response.status,'queue_ms':response.headers.get('X-Varyz-Queue-Ms'),
          'retry_after':response.headers.get('Retry-After')}
with ThreadPoolExecutor(max_workers=12) as pool: burst=list(pool.map(probe,range(12)))
health()
result={'id':data['id'],'image_id':info['Image'],'health_ms':samples,'benign_live_burst':burst,
 'restart_count':info['RestartCount'],'oom_killed':info['State']['OOMKilled'],
 'limits_preserved':True,'source_hashes_verified':True,
 'verified_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
(release/'verification.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
"""
    )
    save("verification.json", value)
    print(json.dumps(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["inspect", "prepare", "stage", "apply", "status", "verify"]
    )
    args = parser.parse_args()
    globals()[args.action]()
