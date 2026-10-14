"""Prepare an isolated sharing candidate from the live image; preserve a rollback image."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/shared-rollout"
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


_ROUTE = """                    "name": "route_task",
                    "description": ROUTE_DESCRIPTION,
                    "inputSchema": json.loads(json.dumps(ROUTE_SCHEMA)),
                }
            )
"""
_RULES = """        elif name in ("remember_owner_rule", "remember_owner_workflow"):
"""
_DIGEST = """                    if history_access:
                        value["limitation"] = check["coverage"]
        return value
"""
COLLABORATION_HUNKS = (
    (
        _ROUTE,
        _ROUTE
        + """            if settings.shared_varyz_enabled:
                from .sharing_models import Collaboration

                result["tools"].append(
                    {
                        "name": "collaboration",
                        "description": COLLABORATION_DESCRIPTION,
                        "inputSchema": Collaboration.model_json_schema(),
                    }
                )
""",
    ),
    (
        _RULES,
        """        elif name == "collaboration":
            from pydantic import ValidationError

            from .sharing_models import Collaboration
            from .sharing_service import SharingError

            try:
                data = Collaboration.model_validate(args).model_dump()
                value = app.state.sharing.collaborate(owner["id"], data)
            except ValidationError:
                raise HTTPException(422, "Check the collaboration fields") from None
            except SharingError as error:
                raise HTTPException(error.status, error.message) from None
"""
        + _RULES,
    ),
    (
        _DIGEST,
        _DIGEST.removesuffix("        return value\n")
        + """                    shared = app.state.sharing.owner_digest(owner["id"], project)
                    if shared:
                        value["collaboration"] = shared
        return value
""",
    ),
)


def remote(code):
    result = subprocess.run(SSH, input=code.encode(), capture_output=True, timeout=240)
    if result.returncode:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "diagnostic.txt").write_bytes(result.stderr)
        raise RuntimeError("Remote operation failed; diagnostic in task scratch directory")
    return result.stdout


def inspect():
    OUT.mkdir(parents=True, exist_ok=True)
    value = json.loads(
        remote("""import subprocess,json,base64
container='varyz-cloud-cloud-1'
package='/usr/local/lib/python3.12/site-packages/varyz/cloud/'
files={name:base64.b64encode(subprocess.check_output(['docker','exec',container,'cat',package+name])).decode()
       for name in ['app.py','store.py','postgres.py','ui.py','connections.py']}
print(json.dumps(files))
""")
    )
    for name, data in value.items():
        target = OUT / "before" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
    print("Live source captured; no live data or configuration changed.")


def runtime():
    print(
        remote("""import subprocess,json
print(subprocess.check_output(['docker','ps','--format','{{.Names}} {{.Image}}']).decode())
code='''import json
from varyz.cloud.app import Settings,create_app
app=create_app(Settings.from_env())
with app.state.store.connection() as db:
 rows=db.execute("SELECT username,status,email_verified FROM accounts "
                 "WHERE username IN ('egr','joemama')").fetchall()
 print(json.dumps([dict(r) for r in rows]))
'''
print(subprocess.check_output(['docker','exec','varyz-cloud-cloud-1','python','-c',code]).decode())
""").decode()
    )


def prepare():
    files = {}
    for name in ("app.py", "store.py", "postgres.py"):
        files[name] = (ROOT / "src/varyz/cloud" / name).read_bytes()
    # The local checkout contains independent owner-MCP work. Keep its live version
    # in this release; sharing registers its separate endpoint after that section.
    current_app = files["app.py"].decode()
    live_app = (OUT / "before/app.py").read_text(encoding="utf-8")
    owner_start = '    @app.post("/mcp/plugin")'
    owner_end = "    social = SocialAuth("
    owner = live_app[live_app.index(owner_start) : live_app.index(owner_end)]
    # Add only the owner's collaboration tool and start_task digest to that live section.
    for before, after in COLLABORATION_HUNKS:
        if after not in owner:
            assert owner.count(before) == 1, before[:70]
            owner = owner.replace(before, after, 1)
    files["app.py"] = (
        current_app[: current_app.index(owner_start)]
        + owner
        + current_app[current_app.index(owner_end) :]
    ).encode()
    # Apply only sharing navigation to the live UI; preserve its formatting and other work.
    ui = (OUT / "before/ui.py").read_text(encoding="utf-8")
    replacements = [
        (
            "    next_questions: str | None = None,\n",
            "    next_questions: str | None = None,\n    sharing_enabled: bool = False,\n",
        ),
        (
            '    main += "</section>"\n    if archive_status',
            '    main += "</section>"\n    if sharing_enabled:\n'
            '        main += \'<p><a href="/account/shares/new">'
            "Share selected project preferences</a></p>'\n"
            "    if archive_status",
        ),
        (
            '    nav += "</nav>"\n',
            "    if sharing_enabled:\n"
            '        nav += \'<a href="/account/shared">Shared with me</a>'
            '<a href="/account/questions">Questions</a>\'\n'
            '    nav += "</nav>"\n',
        ),
    ]
    if "    sharing_enabled: bool = False," not in ui:
        for before, after in replacements:
            assert ui.count(before) == 1, before
            ui = ui.replace(before, after, 1)
    files["ui.py"] = ui.encode()
    connections = (OUT / "before/connections.py").read_text(encoding="utf-8")
    if '"varyz.shared":' not in connections:
        anchor = (
            '            "varyz.plugin": "Read and update preferences; read approved history",\n'
        )
        assert connections.count(anchor) == 1
        connections = connections.replace(
            anchor,
            anchor
            + '            "varyz.shared": "Read shared preferences and ask their owners",\n',
        )
    files["connections.py"] = connections.encode()
    for path in (ROOT / "src/varyz/cloud").glob("sharing*.py"):
        files[path.name] = path.read_bytes()
    candidate = OUT / "candidate"
    candidate.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (candidate / name).write_bytes(value)
    manifest = {
        "id": "shared-" + uuid.uuid4().hex[:12],
        "files": {name: base64.b64encode(value).decode() for name, value in files.items()},
        "expected": {
            name: base64.b64encode((OUT / "before" / name).read_bytes()).decode()
            for name in ("app.py", "store.py", "postgres.py", "ui.py", "connections.py")
        },
        "probe": base64.b64encode((ROOT / "scripts/shared_varyz_probe.py").read_bytes()).decode(),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    print(
        "Candidate prepared: sharing modules, scoped authentication, transactions and navigation."
    )


def candidate():
    manifest = json.loads((OUT / "manifest.json").read_text())
    code = "data=json.loads(" + repr(json.dumps(manifest)) + ")\n"
    code = (
        "import json\n"
        + code
        + r"""
import base64,secrets,subprocess,time
from pathlib import Path
container='varyz-cloud-cloud-1'
package='/usr/local/lib/python3.12/site-packages/varyz/cloud/'
release=Path('/opt/varyz-shared-releases')/data['id']
release.mkdir(parents=True,mode=0o700,exist_ok=True)
def run(command,timeout=180):
 result=subprocess.run(command,capture_output=True,timeout=timeout)
 if result.returncode:
  (release/'operation.log').write_bytes(result.stdout+result.stderr)
  raise RuntimeError('Candidate check failed; protected diagnostic retained')
 return result.stdout
for name,value in data['expected'].items():
 assert run(['docker','exec',container,'cat',package+name])==base64.b64decode(value),name
image=run(['docker','inspect',container,'--format={{.Image}}']).decode().strip()
base='varyz-cloud:'+data['id']+'-base'
image_name='varyz-cloud:'+data['id']
run(['docker','tag',image,base])
lines=['FROM '+base]
for name,value in data['files'].items():
 target=release/'payload'/name
 target.parent.mkdir(parents=True,exist_ok=True)
 target.write_bytes(base64.b64decode(value))
 lines+=['COPY payload/'+name+' '+package+name,
         'COPY payload/'+name+' /opt/varyz/src/varyz/cloud/'+name]
(release/'probe.py').write_bytes(base64.b64decode(data['probe']))
lines+=['COPY probe.py /tmp/shared-probe.py']
(release/'Dockerfile').write_text(chr(10).join(lines)+chr(10))
run(['docker','build','--network=none','-t',image_name,str(release)])
sqlite=run(['docker','run','--rm','--network=none','-e','VARYZ_PROBE_TMPDIR=/tmp',
 '--entrypoint','python',image_name,
 '/tmp/shared-probe.py']).decode()
if subprocess.run(['docker','image','inspect','postgres:17'],capture_output=True).returncode:
 run(['docker','pull','postgres:17'],timeout=180)
pg_name=data['id']+'-postgres'
password=secrets.token_urlsafe(24)
run(['docker','run','-d','--rm','--network=none','--name',pg_name,
 '-e','POSTGRES_PASSWORD='+password,'-e','POSTGRES_USER=shared_test',
 '-e','POSTGRES_DB=shared_test','postgres:17'])
try:
 for attempt in range(40):
  ready=subprocess.run(['docker','exec',pg_name,'pg_isready','-U','shared_test'],capture_output=True)
  if ready.returncode==0: break
  time.sleep(0.5)
 else: raise RuntimeError('Disposable PostgreSQL did not start')
 postgres=run(['docker','run','--rm','--network=container:'+pg_name,
  '-e','VARYZ_PROBE_POSTGRES=postgresql://shared_test:'+password+'@127.0.0.1/shared_test',
  '--entrypoint','python',image_name,'/tmp/shared-probe.py']).decode()
finally:
 run(['docker','stop','--time','10',pg_name])
result={'id':data['id'],'image':image_name,'base':base,
        'sqlite':json.loads(sqlite),'postgresql':json.loads(postgres),'live_changed':False}
(release/'candidate.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
"""
    )
    value = json.loads(remote(code))
    (OUT / "candidate.json").write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


def diagnose():
    manifest = json.loads((OUT / "manifest.json").read_text())
    code = "from pathlib import Path\np=Path('/opt/varyz-shared-releases')/" + repr(manifest["id"])
    code += "/'operation.log'\nprint(p.read_text()[-8000:])\n"
    print(remote(code).decode())


def apply():
    manifest = json.loads((OUT / "manifest.json").read_text())
    candidate_result = json.loads((OUT / "candidate.json").read_text())
    if candidate_result["id"] != manifest["id"]:
        raise RuntimeError("Candidate results do not match the prepared release")
    code = "data=" + repr({"id": manifest["id"], "expected": manifest["expected"]}) + "\n"
    code += r'''
import base64,json,re,subprocess,time,urllib.request
from pathlib import Path
release=Path('/opt/varyz-shared-releases')/data['id']
result=json.loads((release/'candidate.json').read_text())
assert all(result[b]['checks'] and all(result[b]['checks'].values())
           for b in ['sqlite','postgresql'])
container='varyz-cloud-cloud-1'
info=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
labels=info['Config']['Labels']
configs=labels['com.docker.compose.project.config_files'].split(',')
primary=Path(configs[0])
assert str(primary)=='/srv/varyz/src/deploy/cloud/compose.yaml'
for name,value in data['expected'].items():
 actual=subprocess.check_output(['docker','exec',container,'cat',
  '/usr/local/lib/python3.12/site-packages/varyz/cloud/'+name])
 assert actual==base64.b64decode(value),'Live source changed: '+name
before=primary.read_bytes()
updated=before.replace(bytes([13,10]),bytes([10]))
image_line=('    image: '+info['Config']['Image']).encode()
assert image_line in updated
updated=updated.replace(image_line,('    image: '+result['image']).encode(),1)
assert updated.count(b'    environment:\n')==1
settings={
 'SHARED_VARYZ_ENABLED':'true', 'SHARED_VARYZ_ACCOUNTS':'egr,joemama',
 'SHARED_VARYZ_CHATGPT_PLUGIN_URL':
 'https://chatgpt.com/plugins/plugins_6ac82ff6995c8191a12c507ae9e30b03'}
for name,value in settings.items():
 pattern=('^      '+name+':.*$').encode()
 line=('      '+name+': "'+value+'"').encode()
 if re.search(pattern,updated,re.M): updated=re.sub(pattern,line,updated,flags=re.M)
 else: updated=updated.replace(b'    environment:\n',b'    environment:\n'+line+b'\n',1)
(release/'compose.before.yaml').write_bytes(before)
backup_code="""import sqlite3
from pathlib import Path
from varyz.cloud.app import Settings
Path('/tmp/shared-before.sqlite').unlink(missing_ok=True)
database=Path(Settings.from_env().database)
backup_path=database.parent/'.shared-before.sqlite'
assert backup_path.resolve()!=database.resolve()
backup_path.unlink(missing_ok=True)
source=sqlite3.connect(str(database))
target=sqlite3.connect(str(backup_path))
source.backup(target);target.close();source.close()
print(str(backup_path))
"""
backup=subprocess.run(['docker','exec',container,'python','-c',backup_code],capture_output=True)
if backup.returncode:
 (release/'backup-error.log').write_bytes(backup.stderr)
 raise RuntimeError('Backup failed: '+backup.stderr.decode()[-1000:])
backup_path=backup.stdout.decode().strip()
assert backup_path.endswith('/.shared-before.sqlite')
subprocess.run(['docker','cp',container+':'+backup_path,str(release/'database.before.sqlite')],
               check=True,capture_output=True)
subprocess.run(['docker','exec',container,'rm',backup_path],check=True,capture_output=True)
(release/'database.before.sqlite').chmod(0o600)
compose=['docker','compose','--project-name','varyz-cloud','--env-file','/etc/varyz-cloud/cloud.env']
for config in configs: compose+=['-f',config]
def up():
 operation=subprocess.run(compose+['up','-d','--no-build','cloud'],capture_output=True,timeout=90)
 (release/'deployment.log').write_bytes(operation.stdout+operation.stderr)
 operation.check_returncode()
try:
 primary.write_bytes(updated)
 up()
 for attempt in range(40):
  try:
   url='https://cloud.varyzai.com/.well-known/oauth-protected-resource/mcp/shared'
   with urllib.request.urlopen(url,timeout=5) as response:
    metadata=json.load(response)
   if metadata['scopes_supported']==['varyz.shared']: break
  except Exception: pass
  time.sleep(1)
 else: raise RuntimeError('Shared resource health check failed')
except Exception:
 primary.write_bytes(before)
 up()
 raise
print(json.dumps({'id':data['id'],'image':result['image'],'enabled_accounts':['egr','joemama'],
 'database_backup':True,'rollback_compose':str(release/'compose.before.yaml'),'live_changed':True}))
'''
    value = json.loads(remote(code))
    (OUT / "deployment.json").write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


def live_test():
    probe = (ROOT / "scripts/shared_varyz_live_probe.py").read_text()
    code = "probe=" + repr(probe) + "\n"
    code += "import subprocess\n"
    code += (
        "result=subprocess.run(['docker','exec','-i','varyz-cloud-cloud-1','python','-c',"
        "'import sys;exec(sys.stdin.read())'],input=probe.encode(),"
        "capture_output=True,timeout=120)\n"
        "if result.returncode: raise RuntimeError(result.stderr.decode()[-2000:])\n"
        "print(result.stdout.decode())\n"
    )
    value = json.loads(remote(code))
    (OUT / "live-acceptance.json").write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


def native_status():
    pilot = json.loads((OUT / "live-acceptance.json").read_text())
    probe = "membership=" + repr(pilot["demo_membership_id"]) + "\n"
    probe += """import json,time
from varyz.cloud.app import Settings,create_app
from varyz.cloud.store import digest
app=create_app(Settings.from_env())
with app.state.store.connection() as db:
 rows=db.execute("SELECT c.client,c.challenge FROM credentials c "
  "JOIN credential_grants g ON g.hash=c.hash JOIN accounts a ON a.id=c.account "
  "WHERE a.username='egr' AND c.kind='access' AND c.expires>? AND g.scope='varyz.shared'",
  (time.time(),)).fetchall()
 connections={digest(r['client']+':'+r['challenge']) for r in rows}
 bindings=db.execute("SELECT connection,task,revision,publication FROM shared_task_bindings "
  "WHERE membership=?",(membership,)).fetchall()
 active=[dict(b) for b in bindings if b['connection'] in connections]
 print(json.dumps({'active_native_grants':len(rows),'demo_task_bindings':[
  {k:b[k] for k in ('task','revision','publication')} for b in active]}))
"""
    code = "probe=" + repr(probe) + "\nimport subprocess\n"
    code += (
        "r=subprocess.run(['docker','exec','-i','varyz-cloud-cloud-1','python','-c',"
        "'import sys;exec(sys.stdin.read())'],input=probe.encode(),"
        "capture_output=True,check=True)\n"
        "print(r.stdout.decode())\n"
    )
    value = json.loads(remote(code))
    (OUT / "native-status.json").write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


def setup_test():
    pilot = json.loads((OUT / "live-acceptance.json").read_text())
    probe = (ROOT / "scripts/shared_varyz_setup_live_probe.py").read_text()
    probe += "\nmain(" + repr(pilot["demo_membership_id"]) + ")\n"
    code = "probe=" + repr(probe) + "\nimport subprocess\n"
    code += (
        "r=subprocess.run(['docker','exec','-i','varyz-cloud-cloud-1','python','-c',"
        "'import sys;exec(sys.stdin.read())'],input=probe.encode(),"
        "capture_output=True,check=True)\nprint(r.stdout.decode())\n"
    )
    # The probe's standalone guard must not execute inside the remote container.
    code = code.replace(
        "'import sys;exec(sys.stdin.read())'",
        '\'import sys;exec(sys.stdin.read(),{"__name__":"pilot_probe"})\'',
    )
    value = json.loads(remote(code))
    (OUT / "setup-acceptance.json").write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--live-test", action="store_true")
    parser.add_argument("--native-status", action="store_true")
    parser.add_argument("--setup-test", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        inspect()
    if args.runtime:
        runtime()
    if args.prepare:
        prepare()
    if args.candidate:
        candidate()
    if args.diagnose:
        diagnose()
    if args.apply:
        apply()
    if args.live_test:
        live_test()
    if args.native_status:
        native_status()
    if args.setup_test:
        setup_test()
