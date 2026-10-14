"""Inspect and repair sharing configuration without replacing the running code."""

import argparse
import hashlib
import json
from pathlib import Path

from shared_varyz_rollout import remote

PROBE = '''
from varyz.cloud.app import Settings, create_app
from pathlib import Path
import hashlib, json, tempfile
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import varyz.cloud.ui as ui
s=Settings.from_env()
a=create_app(s)
assert s.shared_varyz_enabled
assert s.shared_varyz_accounts == ('egr','joemama')
with tempfile.TemporaryDirectory() as folder:
 fixture=create_app(Settings(database=folder+'/check.sqlite',
  public_url='https://fixture.test',shared_varyz_enabled=s.shared_varyz_enabled,
  shared_varyz_accounts=s.shared_varyz_accounts,
  archive_encryption_key=Fernet.generate_key().decode(),email_sender=lambda message:None))
 store=fixture.state.store
 with store.connection() as db:
  db.execute("INSERT INTO accounts(id,email,password,status,email_verified,username) "
             "VALUES('check','check@example.test','unused','active',1,'egr')")
 store.push('check',[dict(kind='rule',id='style',base_version=0,
                        payload=dict(statement='Use readable typography.',scope='sample'))])
 with TestClient(fixture,base_url='https://fixture.test') as browser:
  browser.cookies.set('__Host-session',store.credential('check','session',300))
  home=browser.get('/?view=dashboard')
  assert home.status_code==200
  assert 'href="/account/shared"' in home.text and 'href="/account/questions"' in home.text
  assert '/account/shares/new?project=sample' in home.text
  for path in ['/account/shared','/account/questions','/account/shares/new?project=sample']:
   response=browser.get(path)
   assert response.status_code==200, (path,response.status_code)
   assert 'Marcellus' in response.text and '#ffc23d' in response.text
print(json.dumps({'enabled':s.shared_varyz_enabled,'pilots':s.shared_varyz_accounts,
 'installed_browser_checks':True,
 'hashes':{name:hashlib.sha256(Path(ui.__file__).with_name(name).read_bytes()).hexdigest()
           for name in ['ui.py','sharing_ui.py']}}))
'''

RESTORE = r'''
import json, os, subprocess, time, urllib.request, uuid
from pathlib import Path
os.umask(0o077)
container='varyz-cloud-cloud-1'
info=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
configs=info['Config']['Labels']['com.docker.compose.project.config_files'].split(',')
assert configs[0]=='/srv/varyz/src/deploy/cloud/compose.yaml'
env=Path('/etc/varyz-cloud/cloud.env')
before=env.read_bytes()
release=Path('/srv/varyz/connection-releases')/('sharing-restore-'+str(uuid.uuid4()))
release.mkdir(parents=True, mode=0o700)
(release/'cloud.env.before').write_bytes(before)
settings={
 'SHARED_VARYZ_ENABLED':'true',
 'SHARED_VARYZ_ACCOUNTS':'egr,joemama',
 'SHARED_VARYZ_CHATGPT_PLUGIN_URL':
 'https://chatgpt.com/plugins/plugins_6ac82ff6995c8191a12c507ae9e30b03',
}
lines=[line for line in before.decode().splitlines() if line.split('=',1)[0] not in settings]
updated=('\n'.join(lines)+'\n'+''.join(k+'='+v+'\n' for k,v in settings.items())).encode()
compose=['docker','compose','-p','varyz-cloud','--env-file',str(env)]
for config in configs: compose+=['-f',config]
def run(args):
 result=subprocess.run(args,capture_output=True,timeout=120)
 if result.returncode:
  (release/'operation.log').write_bytes(result.stdout+result.stderr)
  raise RuntimeError('Sharing repair failed; protected server diagnostic retained')
 return result.stdout
def healthy():
 for attempt in range(40):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    if response.status==200: return
  except Exception: pass
  time.sleep(.5)
 raise RuntimeError('Cloud did not become healthy')
try:
 assert env.read_bytes()==before
 partial=env.with_suffix('.partial')
 partial.write_bytes(updated); partial.chmod(0o600); partial.replace(env)
 run(compose+['config','--quiet'])
 run(compose+['up','-d','--no-build','cloud'])
 healthy()
 current=json.loads(run(['docker','inspect',container]))[0]
 assert current['Image']==info['Image'], 'Unexpected image change'
 values=dict(item.split('=',1) for item in current['Config']['Env'])
 assert all(values.get(k)==v for k,v in settings.items())
 print(json.dumps({'restored':True,'healthy':True,'image_unchanged':True,
                   'persistent_environment':True,'rollback':str(release)}))
except Exception:
 env.write_bytes(before); env.chmod(0o600)
 run(compose+['up','-d','--no-build','cloud'])
 healthy()
 raise
'''

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        code = RESTORE
    else:
        code = 'import subprocess\ncode=' + repr(PROBE) + '\n'
        code += (
            "print(subprocess.check_output(['docker','exec','varyz-cloud-cloud-1',"
            "'python','-c',code]).decode())\n"
        )
    output = remote(code).decode()
    if not args.apply:
        result = json.loads(output)
        cloud = Path(__file__).resolve().parents[1] / 'src/varyz/cloud'
        assert result['hashes'] == {
            name: hashlib.sha256((cloud / name).read_bytes()).hexdigest()
            for name in ('ui.py', 'sharing_ui.py')
        }, 'Live styles differ from the visually reviewed source'
    print(output)
