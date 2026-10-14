"""Apply only the history preflight change to existing installations, with backups."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import uuid
from pathlib import Path

from coordination_rollout import apply_to

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".scratch/task-history-rollout"


def section(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


def payload(surface):
    changes = {}

    def text(name):
        return (ROOT / "src/varyz" / name).read_text(encoding="utf-8")

    def pair(name, before, after):
        changes.setdefault(name, []).append([before, after])

    questioning = text("resolution/questioning.py")
    pair("resolution/questioning.py", "QUESTION_POLICY = (\n",
         section(questioning, "HISTORY_POLICY = (\n", "QUESTION_POLICY = (\n")
         + "QUESTION_POLICY = (\n")
    anchor = ('    "Check the current request, recorded rules and decisions, '
              'and repository first. "\n')
    pair("resolution/questioning.py", anchor,
         anchor + '    + HISTORY_POLICY + " "\n    + ')

    if surface == "cloud":
        file = "cloud/app.py"
        source = text(file)
        before = '''                            "Read confirmed rules and workflows for a project.",
                            {
                                "type": "object",
                                "properties": {"project": {"type": "string"}},
                                "required": ["project"],
                            },'''
        pair(file, before, section(source,
             '                            "Read confirmed rules, workflows and relevant',
             '\n                        ),\n                        (\n'
             '                            "ask",'))
        before = ('            try:\n'
                  '                value = await asyncio.to_thread(call_tool, owner, name, args)\n')
        pair(file, before, section(source,
                                  '            try:\n                if combined and name',
                                  '            except HTTPException as error:'))
        pair(file, "    def call_tool(owner, name, args):\n",
             "    def call_tool(owner, name, args, *, history_access=False):\n")
        before = ('                    "limitation": "Compiled confirmed preferences only; '
                  'no archive access.",\n                }\n')
        pair(file, before, before + section(source,
             '                if name == "start_task":\n', '        return value\n'))
    else:
        file = "resolution/briefing.py"
        source = text(file)
        anchor = "from varyz.resolution.questioning import QUESTION_POLICY\n"
        pair(file, anchor, anchor + "from varyz.resolution.task_history import preflight\n")
        anchor = '        "kinds_of_work": [\n'
        pair(file, anchor, '        "history_check": preflight(task, project),\n' + anchor)
        anchor = '    job = briefing.get("background_job")\n'
        pair(file, anchor, section(source, '    history = briefing.get("history_check")\n',
                                  anchor) + anchor)
        file = "mcp_server.py"
        source = text(file)
        anchor = ('        if caller.capability_id is None:\n'
                  '            # An unregistered agent gets the smallest useful briefing, '
                  'as with advise.\n')
        pair(file, anchor, section(source,
             '        if result.get("status") == "delegated":\n', anchor) + anchor)
        file = "hooks.py"
        anchor = '        render_background_job(job) if job is not None else "",\n'
        pair(file, anchor, anchor +
             '        render_markdown({"project": project, '
             '"history_check": briefing["history_check"]},\n'
             '                        include_protocol=False),\n')
        # Skills are managed product files; update from the canonical source.
        changes["skills/varyz-context/SKILL.md"] = []

    return {
        "replacements": changes,
        "additions": {"resolution/task_history.py": text("resolution/task_history.py")},
    }


def ssh(code):
    result = subprocess.run([
        "ssh", "-i", str(Path.home() / ".ssh/varyz_cloud_ed25519"),
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "root@2.31.3.96", "python3 -c 'import sys;exec(sys.stdin.read())'",
    ], input=code.encode(), capture_output=True, timeout=400)
    if result.returncode:
        (OUT / "diagnostic.txt").write_bytes(result.stderr)
        raise RuntimeError("Remote operation failed; diagnostic retained in task scratch directory")
    return result.stdout


def prepare_cloud():
    data = payload("cloud")
    original = json.loads(ssh('''import subprocess,json,base64
names=['cloud/app.py','resolution/questioning.py']
print(json.dumps({n:base64.b64encode(subprocess.check_output(['docker','exec',
'varyz-cloud-cloud-1','cat','/usr/local/lib/python3.12/site-packages/varyz/'+n])).decode()
for n in names}))
'''))
    # Accept the first history deployment as a known predecessor too.
    live = base64.b64decode(original["cloud/app.py"]).decode().replace("\r\n", "\n")
    predecessor = '''            try:
                value = await asyncio.to_thread(
                    call_tool, owner, name, args,
                    history_access=bool(combined and request.state.archive_allowed),
                )
'''
    for pair in data["replacements"]["cloud/app.py"]:
        if pair[0].startswith("            try:\n") and predecessor in live:
            pair[0] = predecessor
    package = OUT / ("candidate-" + uuid.uuid4().hex)
    for name, value in original.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(value))
    apply_to(package, data, OUT / ("preparation-backup-" + uuid.uuid4().hex))
    files = {name: base64.b64encode((package / name).read_bytes()).decode()
             for name in set(data["replacements"]) | set(data["additions"])}
    (OUT / "prepared.json").write_text(json.dumps({"expected": original, "files": files}))
    print(json.dumps({"prepared": True, "files": list(files)}))


def deploy_cloud():
    prepared = json.loads((OUT / "prepared.json").read_text())
    code = r'''
import base64,json,pathlib,subprocess,time,urllib.request,uuid
data=json.loads(DATA)
root=pathlib.Path('/srv/varyz/src')
container='varyz-cloud-cloud-1'
package='/usr/local/lib/python3.12/site-packages/varyz/'
release=pathlib.Path('/srv/varyz/connection-releases')/('task-history-'+str(uuid.uuid4()))
release.mkdir(parents=True,mode=0o700)
def run(command):
 result=subprocess.run(command,capture_output=True,timeout=180)
 if result.returncode:
  (release/'operation.log').write_bytes(result.stdout+result.stderr)
  raise RuntimeError('Deployment failed; protected diagnostic retained')
 return result.stdout
for name,value in data['expected'].items():
 assert run(['docker','exec',container,'cat',package+name])==base64.b64decode(value),name
image=run(['docker','inspect',container,'--format={{.Image}}']).decode().strip()
base='varyz-cloud:history-base-'+release.name
candidate='varyz-cloud:'+release.name
previous={}
lines=['FROM '+base]
for name,value in data['files'].items():
 path=root/'src/varyz'/name
 previous[name]=path.read_bytes() if path.exists() else None
 if previous[name] is not None:
  saved=release/'before'/name
  saved.parent.mkdir(parents=True,exist_ok=True)
  saved.write_bytes(previous[name])
 target=release/'payload'/name
 target.parent.mkdir(parents=True,exist_ok=True)
 target.write_bytes(base64.b64decode(value))
 lines+=['COPY payload/'+name+' '+package+name,
         'COPY payload/'+name+' /opt/varyz/src/varyz/'+name]
(release/'Dockerfile').write_text('\n'.join(lines)+'\n')
compose=['docker','compose','-p','varyz-cloud','--env-file','/etc/varyz-cloud/cloud.env',
'-f',str(root/'deploy/cloud/compose.yaml'),'-f',str(root/'deploy/cloud/compose.archive.yaml')]
retention=pathlib.Path('/etc/varyz-cloud/compose.retention.yaml')
if retention.is_file(): compose+=['-f',str(retention)]
target_image=json.loads(run(compose+['config','--format','json']))['services']['cloud']['image']
assert isinstance(target_image,str) and target_image, 'Configured Cloud image is required'
run(['docker','tag',image,base])
run(['docker','build','--network=none','-t',candidate,str(release)])
probe=release/'probe.py'
probe.write_bytes(base64.b64decode(PROBE))
run(['docker','run','--rm','--network=none','-v',str(probe)+':/tmp/history-probe.py:ro',
'--entrypoint','python',candidate,'/tmp/history-probe.py'])
try:
 for name,value in data['files'].items():
  path=root/'src/varyz'/name
  assert (path.read_bytes() if path.exists() else None)==previous[name]
  path.parent.mkdir(parents=True,exist_ok=True)
  path.write_bytes(base64.b64decode(value))
 run(['docker','tag',candidate,target_image])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 for attempt in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    assert response.status==200
   for name,value in data['files'].items():
    assert run(['docker','exec',container,'cat',package+name])==base64.b64decode(value)
   break
  except Exception:
   if attempt==29: raise
   time.sleep(1)
except BaseException:
 for name,value in previous.items():
  path=root/'src/varyz'/name
  if value is None: path.unlink(missing_ok=True)
  else: path.write_bytes(value)
 run(['docker','tag',image,target_image])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 raise
print(json.dumps({'healthy':True,'backup':str(release),'image':candidate}))
'''
    code = code.replace("DATA", repr(json.dumps(prepared)), 1)
    code = code.replace("PROBE", repr(base64.b64encode(
        (ROOT / "scripts/task_history_probe.py").read_bytes()).decode()), 1)
    receipt = json.loads(ssh(code))
    (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=["local", "cloud"], required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.surface == "cloud":
        deploy_cloud() if args.apply else prepare_cloud()
    elif args.package and args.backup:
        data = payload("local")
        skill = args.package / "skills/varyz-context/SKILL.md"
        data["replacements"]["skills/varyz-context/SKILL.md"] = [[
            skill.read_text(encoding="utf-8"),
            (ROOT / "src/varyz/skills/varyz-context/SKILL.md").read_text(encoding="utf-8"),
        ]]
        print(json.dumps({"patched_files": apply_to(args.package, data, args.backup)}))
    else:
        parser.error("Local patches require --package and --backup")


if __name__ == "__main__":
    main()
