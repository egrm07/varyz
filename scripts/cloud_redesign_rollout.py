"""Prepare/deploy the reviewed Cloud UI against live code, retaining image rollback."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/cloud-redesign-rollout"
if "--out" in sys.argv:
    OUT = Path(sys.argv[sys.argv.index("--out") + 1]).resolve()
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


def remote(code):
    result = subprocess.run(SSH, input=code.encode(), capture_output=True, timeout=400)
    if result.returncode:
        (OUT / "diagnostic.txt").write_bytes(result.stderr)
        raise RuntimeError("Remote operation failed; diagnostic retained in task scratch directory")
    return result.stdout


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if "--apply" not in sys.argv:
        original = json.loads(
            remote("""import subprocess,json,base64
names=['app.py','ui.py','store.py','postgres.py','computer_connect.js','social.py','payment_wait.js']
values={name:base64.b64encode(subprocess.check_output(['docker','exec',
 'varyz-cloud-cloud-1','cat','/usr/local/lib/python3.12/site-packages/varyz/cloud/'+name])).decode()
 for name in names}
extra=subprocess.run(['docker','exec','varyz-cloud-cloud-1','cat',
 '/usr/local/lib/python3.12/site-packages/varyz/cloud/session_reports.py'],capture_output=True)
if extra.returncode==0: values['session_reports.py']=base64.b64encode(extra.stdout).decode()
print(json.dumps(values))
""")
        )
        candidate = {}
        live = base64.b64decode(original["app.py"]).decode().replace("\r\n", "\n")
        local = (ROOT / "src/varyz/cloud/app.py").read_text(encoding="utf-8")
        for start, end in [
            ("    def page(content,", '    @app.get("/computer-connect.js")'),
            ('    @app.get("/")', '    @app.post("/account/action")'),
            ("    def auth_form(", '    @app.get("/signup")'),
        ]:
            a, b = live.index(start), live.index(end, live.index(start))
            c, d = local.index(start), local.index(end, local.index(start))
            live = live[:a] + local[c:d] + live[b:]
        anchor = "            elif oauth_close_nonce:"
        if 'elif dashboard_nonce := getattr(request.state, "dashboard_nonce", None):' not in live:
            assert live.count(anchor) == 1
            live = live.replace(
                anchor,
                '            elif dashboard_nonce := '
                'getattr(request.state, "dashboard_nonce", None):\n'
                "                policy += f\"; script-src 'nonce-{dashboard_nonce}'; "
                "connect-src 'self'\"\n" + anchor,
                1,
            )
        candidate["app.py"] = live
        for name in (
            "ui.py", "computer_connect.js", "dashboard.js", "dashboard.py", "payment_wait.js"
        ):
            candidate[name] = (ROOT / "src/varyz/cloud" / name).read_text(encoding="utf-8")
        # Preserve provider protocol and credentials; change only available sign-in controls.
        social_live = base64.b64decode(original["social.py"]).decode().replace("\r\n", "\n")
        social_local = (ROOT / "src/varyz/cloud/social.py").read_text(encoding="utf-8")
        a, b = social_live.index("    def buttons("), social_live.index("    def configured(")
        c, d = social_local.index("    def buttons("), social_local.index("    def configured(")
        candidate["social.py"] = social_live[:a] + social_local[c:d] + social_live[b:]
        if "session_reports.py" in original:
            report_live = (
                base64.b64decode(original["session_reports.py"]).decode().replace("\r\n", "\n")
            )
            report_local = (ROOT / "src/varyz/cloud/session_reports.py").read_text(encoding="utf-8")
            a, b = report_live.index("    def status("), report_live.index("    def manifest(")
            c, d = report_local.index("    def status("), report_local.index("    def manifest(")
            candidate["session_reports.py"] = report_live[:a] + report_local[c:d] + report_live[b:]
        # Add only the index to the deployed store implementations.
        for name in ("store.py", "postgres.py"):
            value = base64.b64decode(original[name]).decode().replace("\r\n", "\n")
            before = "CREATE INDEX IF NOT EXISTS records_dashboard_scope"
            if before not in value:
                source = (ROOT / "src/varyz/cloud" / name).read_text(encoding="utf-8")
                start = source.index(before)
                end = source.index(";", start) + 1
                anchor = (
                    "CREATE INDEX IF NOT EXISTS changes_account_cursor ON changes(account,cursor);"
                )
                assert value.count(anchor) == 1
                value = value.replace(anchor, anchor + "\n" + source[start:end], 1)
            candidate[name] = value
        for name, value in candidate.items():
            if name.endswith(".py"):
                compile(value, name, "exec")
            (OUT / ("candidate-" + name)).write_text(value, encoding="utf-8", newline="\n")
        (OUT / "original.json").write_text(json.dumps(original))
        (OUT / "payload.json").write_text(
            json.dumps(
                {
                    name: base64.b64encode(value.encode()).decode()
                    for name, value in candidate.items()
                }
            )
        )
        print("Prepared Cloud redesign against running server, preserving unrelated changes.")
        return
    payload = (OUT / "payload.json").read_text()
    expected = (OUT / "original.json").read_text()
    code = r"""
import base64,json,pathlib,subprocess,time,urllib.request,uuid
files=json.loads(PAYLOAD)
expected=json.loads(EXPECTED)
container='varyz-cloud-cloud-1'
package='/usr/local/lib/python3.12/site-packages/varyz/cloud/'
root=pathlib.Path('/srv/varyz/src')
release=pathlib.Path('/srv/varyz/connection-releases')/('redesign-'+str(uuid.uuid4()))
release.mkdir(parents=True,mode=0o700)
def run(command):
 result=subprocess.run(command,capture_output=True,timeout=180)
 if result.returncode:
  (release/'operation.log').write_bytes(result.stdout+result.stderr)
  raise RuntimeError('Deployment failed; protected diagnostic retained')
 return result.stdout
for name,value in expected.items():
 assert run(['docker','exec',container,'cat',package+name])==base64.b64decode(value), name
image=run(['docker','inspect',container,'--format={{.Image}}']).decode().strip()
base='varyz-cloud:redesign-base-'+release.name
candidate='varyz-cloud:'+release.name
previous={}
for name,value in files.items():
 path=root/'src/varyz/cloud'/name
 previous[name]=path.read_bytes() if path.exists() else None
 if previous[name] is not None: (release/('before-'+name)).write_bytes(previous[name])
 (release/name).write_bytes(base64.b64decode(value))
(release/'Dockerfile').write_text('FROM '+base+'\n'+''.join('COPY '+name+' '+package+name+
 '\nCOPY '+name+' /opt/varyz/src/varyz/cloud/'+name+'\n' for name in files))
compose=['docker','compose','-p','varyz-cloud','--env-file','/etc/varyz-cloud/cloud.env',
 '-f',str(root/'deploy/cloud/compose.yaml'),'-f',str(root/'deploy/cloud/compose.archive.yaml')]
run(['docker','tag',image,base])
run(['docker','build','--network=none','-t',candidate,str(release)])
probe=pathlib.Path(release/'probe.py')
probe.write_bytes(base64.b64decode(PROBE))
run(['docker','run','--rm','--network=none','-v',str(probe)+':/tmp/redesign-probe.py:ro',
 '--entrypoint','python',candidate,'/tmp/redesign-probe.py'])
try:
 for name,value in files.items(): (root/'src/varyz/cloud'/name).write_bytes(base64.b64decode(value))
 run(['docker','tag',candidate,'varyz-cloud:local'])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 for attempt in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    assert response.status==200
   for name,value in files.items():
    assert run(['docker','exec',container,'cat',package+name])==base64.b64decode(value)
   break
  except Exception:
   if attempt==29: raise
   time.sleep(1)
except BaseException:
 for name,value in previous.items():
  path=root/'src/varyz/cloud'/name
  if value is None: path.unlink(missing_ok=True)
  else: path.write_bytes(value)
 run(['docker','tag',image,'varyz-cloud:local'])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 raise
print(json.dumps({'healthy':True,'backup':str(release)}))
"""
    probe = (ROOT / "scripts/cloud_redesign_probe.py").read_bytes()
    code = code.replace("PAYLOAD", repr(payload), 1).replace("EXPECTED", repr(expected), 1)
    code = code.replace("PROBE", repr(base64.b64encode(probe).decode()), 1)
    result = json.loads(remote(code))
    result["files"] = {
        name: hashlib.sha256(base64.b64decode(value)).hexdigest()
        for name, value in json.loads(payload).items()
    }
    (OUT / "receipt.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"healthy": result["healthy"], "rollback": result["backup"]}))


if __name__ == "__main__":
    main()
