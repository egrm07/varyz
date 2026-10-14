"""Deploy only the verified browser connection script, with image rollback."""

import base64
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = r'''
import base64,hashlib,json,pathlib,subprocess,time,urllib.request,uuid
payload=base64.b64decode(PAYLOAD)
root=pathlib.Path('/srv/varyz/src')
source=root/'src/varyz/cloud/computer_connect.js'
old=source.read_bytes()
container='varyz-cloud-cloud-1'
runtime='/usr/local/lib/python3.12/site-packages/varyz/cloud/computer_connect.js'
assert subprocess.check_output(['docker','exec',container,'cat',runtime]) == old
release=pathlib.Path('/srv/varyz/connection-releases')/str(uuid.uuid4())
release.mkdir(parents=True,mode=0o700)
(release/'previous.js').write_bytes(old)
(release/'computer_connect.js').write_bytes(payload)
image=subprocess.check_output(['docker','inspect',container,'--format={{.Image}}'],text=True).strip()
base='varyz-cloud:connection-base-'+release.name
candidate='varyz-cloud:connection-'+release.name
compose=['docker','compose','-p','varyz-cloud','--env-file','/etc/varyz-cloud/cloud.env',
 '-f',str(root/'deploy/cloud/compose.yaml'),'-f',str(root/'deploy/cloud/compose.archive.yaml')]
def run(command):
 result=subprocess.run(command,capture_output=True,timeout=300)
 if result.returncode:
  (release/'operation.log').write_bytes(result.stdout+result.stderr)
  raise RuntimeError('Deployment operation failed; protected log retained')
try:
 run(['docker','tag',image,base])
 (release/'Dockerfile').write_text('FROM '+base+'\nCOPY computer_connect.js '+runtime+
 '\nCOPY computer_connect.js /opt/varyz/src/varyz/cloud/computer_connect.js\n')
 run(['docker','build','--network=none','-t',candidate,str(release)])
 assert source.read_bytes()==old
 source.write_bytes(payload)
 run(['docker','tag',candidate,'varyz-cloud:local'])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 for attempt in range(30):
  try:
   request=urllib.request.Request('http://127.0.0.1:8080/computer-connect.js',
    headers={'Host':'cloud.varyzai.com'})
   with urllib.request.urlopen(request,timeout=3) as response:
    assert response.read()==payload
   with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    assert response.status==200
   break
  except Exception:
   if attempt==29: raise
   time.sleep(1)
except BaseException as error:
 (release/'verification-error.txt').write_text(type(error).__name__)
 source.write_bytes(old)
 run(['docker','tag',image,'varyz-cloud:local'])
 run(compose+['up','-d','--no-deps','--no-build','cloud'])
 raise
print(json.dumps({'healthy':True,'browser_sha256':hashlib.sha256(payload).hexdigest(),
 'backup':str(release)}))
'''


def main():
    payload = (ROOT / "src/varyz/cloud/computer_connect.js").read_text(encoding="utf-8")
    remote = REMOTE.replace("PAYLOAD", repr(base64.b64encode(payload.encode()).decode()), 1)
    result = subprocess.run(
        ["ssh", "-i", str(Path.home() / ".ssh/varyz_cloud_ed25519"),
         "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", "root@2.31.3.96",
         "python3 -c 'import sys; exec(sys.stdin.read())'"],
        input=remote.encode(), capture_output=True, timeout=400,
    )
    if result.returncode:
        raise SystemExit("Connection rollout failed; protected server log retained.")
    print(json.dumps(json.loads(result.stdout)))


if __name__ == "__main__":
    main()
