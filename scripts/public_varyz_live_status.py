"""Read counts and deployment gates only; no account content or credentials printed."""

import json
import subprocess
from pathlib import Path

REMOTE = r"""
import json,subprocess
container='varyz-cloud-cloud-1'
metadata=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
allowed={'SHARED_VARYZ_ENABLED','SHARED_VARYZ_ACCOUNTS','VARYZ_CLOUD_MAX_ACCOUNTS',
         'VARYZ_CLOUD_MAX_EMAILS_PER_DAY','VARYZ_CLOUD_MAX_REGISTERED_CLIENTS'}
settings={key:value for key,value in (line.split('=',1) for line in metadata['Config']['Env'])
          if key in allowed}
code='''import importlib.util,json
from varyz.cloud.app import Settings,create_app
settings=Settings.from_env()
app=create_app(settings)
with app.state.store.connection() as db:
 counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
         for table in ('accounts','follows','public_varyz')
         if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()}
print(json.dumps(dict(max_accounts=settings.max_accounts,counts=counts,
                     storage_limit=settings.storage_limit,change_limit=settings.change_limit,
                     archive_limit=settings.archive_limit,
                     archive_embedding_enabled=bool(settings.archive_embedding_cache),
                     following_module=importlib.util.find_spec('varyz.cloud.following') is not None,
                     profile_route=any(getattr(r,'path','')=='/@{username}' for r in app.routes))))
'''
runtime=json.loads(subprocess.check_output(['docker','exec',container,'python','-c',code]))
host=metadata['HostConfig']
print(json.dumps(dict(image=metadata['Config']['Image'],memory_bytes=host['Memory'],
                     nano_cpus=host['NanoCpus'],settings=settings,runtime=runtime)))
"""

if __name__ == "__main__":
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
    result = subprocess.run(command, input=REMOTE, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise SystemExit("Read-only live metadata check failed: " + result.stderr[-1500:])
    value = json.loads(result.stdout)
    output = Path(__file__).resolve().parents[1] / ".tmp/public-varyz-live-status.json"
    output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(value))
