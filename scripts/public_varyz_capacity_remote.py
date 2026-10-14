"""Run a bounded synthetic candidate beside production; never mounts live data.

Uploads source code only. No pip downloads, public ports, production credentials,
configuration changes or paid infrastructure. Cleans up only its own resources.
"""

import argparse
import base64
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = r"""
import base64,io,json,os,shutil,subprocess,tarfile,time,urllib.request,uuid
from pathlib import Path
config=CONFIG
name='varyz-capacity-'+uuid.uuid4().hex[:12]
root=Path('/tmp')/name
root.mkdir(mode=0o755)
data=root/'data'
data.mkdir(mode=0o700)
os.chown(data,10001,10001)
def command(*args,**kwargs):
 return subprocess.check_output(list(args),text=True,timeout=kwargs.get('timeout',120))
def live_health():
 started=time.monotonic()
 with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
  assert response.status==200
 return round((time.monotonic()-started)*1000,3)
metadata=json.loads(command('docker','inspect','varyz-cloud-cloud-1'))[0]
image=metadata['Image']
limits=metadata['HostConfig']
live_mount=[m['Source'] for m in metadata['Mounts'] if m['Destination']=='/var/lib/varyz-cloud'][0]
same_disk=os.stat(live_mount).st_dev==os.stat(root).st_dev
assert same_disk,'Probe must use the production disk device, without mounting production data'
baseline=[live_health() for _ in range(3)]
result=dict(run_id=name,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
 source_sha256=config['source_sha256'],base_image=image,
 limits=dict(cpu=limits['NanoCpus']/1e9,memory_bytes=limits['Memory']),same_disk_device=same_disk,
 production_health_baseline_ms=baseline,
 scope='isolated synthetic candidate, loopback HTTP; generator shares candidate cgroup',
 samples=[],aborted=None)
process=None
try:
 with tarfile.open(fileobj=io.BytesIO(base64.b64decode(config['archive']))) as archive:
  for item in archive.getmembers():
   assert not item.issym() and not item.islnk() and '..' not in Path(item.name).parts
   assert not Path(item.name).is_absolute()
  archive.extractall(root)
 (root/'Dockerfile').write_text('FROM '+image+'\nCOPY src /opt/capacity/src\n'
  'COPY scripts /opt/capacity/scripts\n'
  'ENV PYTHONPATH=/opt/capacity/src:/opt/capacity/scripts PYTHONDONTWRITEBYTECODE=1\n'
  'WORKDIR /opt/capacity\nHEALTHCHECK NONE\n')
 command('docker','build','--network=none','-t',name,str(root),timeout=180)
 command('docker','run','-d','--name',name,'--network=none','--read-only',
  '--cpus',str(result['limits']['cpu']),'--memory',str(limits['Memory']),
  '--memory-swap',str(limits['Memory']),'--pids-limit','128','--cap-drop=ALL',
  '--security-opt','no-new-privileges:true','--tmpfs','/tmp:size=32m,mode=1777',
  '-v',str(data)+':/probe:rw',name,'python','-m','uvicorn',
  'public_varyz_capacity_server:app','--host','127.0.0.1','--port','8081',
  '--workers','1','--limit-concurrency','32','--backlog','64','--timeout-keep-alive','5',
  '--no-access-log','--proxy-headers','--forwarded-allow-ips','127.0.0.1')
 for i in range(300):
  if i%10==0:
   live_health()
   state=json.loads(command('docker','inspect',name))[0]['State']
   if not state['Running']:
    raise RuntimeError('Candidate exited during synthetic fixture preparation')
  if (data/'fixture.json').exists():
   try:
    command('docker','exec',name,'python','-c',"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=2)",timeout=5)
    break
   except subprocess.SubprocessError: pass
  if i==299: raise RuntimeError('Candidate readiness timeout')
  if i and i%60==0:
   print(json.dumps(dict(preparing_synthetic_fixture_seconds=i)),flush=True)
  time.sleep(1)
 result['fixture']=json.loads((data/'fixture.json').read_text())
 print(json.dumps(dict(ready=name,fixture=result['fixture'],limits=result['limits'])),flush=True)
 log=(root/'generator.log').open('w')
 generator=['docker','exec',name,'python','/opt/capacity/scripts/public_varyz_capacity_load.py',
  '--duration',str(config['duration']),'--step-seconds',str(config['step_seconds']),
  '--rate',str(config['rate']),'--ip-pool',str(config['ip_pool'])]
 if config['maximum']: generator.append('--maximum')
 result['plan']={k:config[k] for k in ['maximum','duration','step_seconds','rate','ip_pool']}
 process=subprocess.Popen(generator,stdout=log,stderr=subprocess.STDOUT)
 started=time.monotonic()
 failures=0
 last_output=0
 while process.poll() is None:
  try:
   health=live_health()
   failures=failures+1 if health>2000 else 0
  except Exception: health=None;failures+=1
  mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
  available=int(mem['MemAvailable'].split()[0])*1024
  code=("import json,pathlib; p=pathlib.Path('/sys/fs/cgroup'); "
   "print(json.dumps({n:(p/n).read_text() for n in "
   "['cpu.stat','memory.current','memory.peak','io.stat']}))")
  resource=json.loads(command('docker','exec',name,'python','-c',code,timeout=10))
  sample=dict(seconds=round(time.monotonic()-started,2),production_health_ms=health,
   host_available_bytes=available,**resource)
  result['samples'].append(sample)
  extra=(9*config['step_seconds']+1260 if config['maximum']
   else 6*config['step_seconds']+180)
  deadline=config['duration']+extra
  if failures>=3 or available<512*1024**2 or time.monotonic()-started>deadline:
   result['aborted']='Live health, host memory or duration safety guard'
   command('docker','stop','--time','10',name)
   break
  if time.monotonic()-started-last_output>=60:
   print(json.dumps(dict(sample_seconds=sample['seconds'],production_health_ms=health,
    candidate_memory_mib=round(int(resource['memory.current'])/1024**2,1),
    progress=(root/'generator.log').read_text()[-1800:])),flush=True)
   last_output=time.monotonic()-started
  time.sleep(5)
 process.wait(timeout=30)
 result['generator_exit_code']=process.returncode
 if process.returncode: result['generator_log_tail']=(root/'generator.log').read_text()[-3500:]
 if (data/'load-results.json').exists():
  result['phases']=json.loads((data/'load-results.json').read_text())
 if config['maximum'] and (data/'cloud.db').exists():
  import sqlite3
  with sqlite3.connect('file:'+str(data/'cloud.db')+'?mode=ro',uri=True) as db:
   result['integrity']=dict(quick_check=db.execute('PRAGMA quick_check').fetchone()[0],
    foreign_key_violations=len(db.execute('PRAGMA foreign_key_check').fetchall()),
    counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in
     ['accounts','follows','public_varyz','rate_limits','archive_documents','archive_chunks']},
    uses=dict(db.execute('SELECT kind,SUM(n) FROM public_uses GROUP BY kind').fetchall()),
    sync_records=db.execute("SELECT COUNT(*) FROM records WHERE account='syncer'").fetchone()[0])
   result['integrity']['database_bytes']=(data/'cloud.db').stat().st_size
except Exception as error:
 result['aborted']=type(error).__name__+': '+str(error)
 try: result['candidate_log_tail']=command('docker','logs','--tail','30',name)[-3500:]
 except Exception: pass
finally:
 subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
 subprocess.run(['docker','image','rm',name],capture_output=True,timeout=30)
 result['production_image_unchanged']=json.loads(command('docker','inspect','varyz-cloud-cloud-1'))[0]['Image']==image
 result['production_health_final_ms']=live_health()
 assert root.parent==Path('/tmp') and root.name==name and name.startswith('varyz-capacity-')
 shutil.rmtree(root)
 result['candidate_resources_removed']=True
print('RESULT_JSON='+json.dumps(result),flush=True)
"""


def main(args):
    files = sorted(
        p
        for p in (ROOT / "src").rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.is_symlink()
        and p.suffix in {".py", ".html", ".css", ".js", ".svg", ".woff2", ".txt", ".json"}
    )
    files += [
        ROOT / "scripts" / name
        for name in ("public_varyz_capacity_server.py", "public_varyz_capacity_load.py")
    ]
    stream = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            digest.update(relative.encode() + b"\0" + content)
            entry = tarfile.TarInfo(relative)
            entry.mode = 0o644
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    config = dict(
        archive=base64.b64encode(stream.getvalue()).decode(),
        source_sha256=digest.hexdigest(),
        duration=args.duration,
        step_seconds=args.step_seconds,
        rate=args.rate,
        maximum=args.maximum,
        ip_pool=args.ip_pool,
    )
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
        "python3 -c 'import sys;exec(sys.stdin.read())'",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(REMOTE.replace("CONFIG", repr(config), 1))
    process.stdin.close()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    value = None
    for line in process.stdout:
        if line.startswith("RESULT_JSON="):
            value = json.loads(line.removeprefix("RESULT_JSON="))
            output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in value.items() if k != "samples"}), flush=True)
        else:
            print(line.rstrip(), flush=True)
    if process.wait():
        raise SystemExit("Remote probe failed; inspect its output")
    if value is None or value.get("aborted") or value.get("generator_exit_code") != 0:
        raise SystemExit("Experiment did not complete; partial results were preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--step-seconds", type=int, default=60)
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--maximum", action="store_true")
    parser.add_argument("--ip-pool", type=int, default=0)
    parser.add_argument("--output", default=".tmp/public-varyz-capacity-remote.json")
    main(parser.parse_args())
