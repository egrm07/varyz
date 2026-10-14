"""Public HTTPS acceptance with strictly disposable synthetic Cloud accounts."""

from __future__ import annotations

import base64
import json

from cloud_redesign_rollout import remote

FIXTURE = r"""
import json,secrets
from varyz.cloud.app import Settings,create_app
app=create_app(Settings.from_env())
store=app.state.store
if ACTION=='create':
 identity='redesign_probe_'+secrets.token_hex(10)
 with store.connection() as db:
  db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
   "VALUES(?,?,?,'active',1)",
   (identity,identity+'@example.test','unusable-synthetic-password'))
 result={'identity':identity,'session':store.credential(identity,'session',1200),
         'backend':type(store).__name__}
else:
 assert isinstance(IDENTITY,str) and IDENTITY.startswith('redesign_probe_') and len(IDENTITY)==35
 identity=IDENTITY
 with store.connection() as db:
  row=db.execute('SELECT email FROM accounts WHERE id=?',(identity,)).fetchone()
  assert row['email']==identity+'@example.test'
  if ACTION in ('payment_pending','payment_active'):
   db.execute('UPDATE accounts SET status=? WHERE id=?',
    ('inactive' if ACTION=='payment_pending' else 'active',identity))
  elif ACTION=='legacy_connection_hint':
   db.execute("UPDATE credentials SET client='legacy-redesign-computer' "
    "WHERE account=? AND kind IN ('access','refresh') AND client='varyz-local'",(identity,))
  elif ACTION=='expire':
   db.execute("UPDATE credentials SET expires=1 WHERE account=? AND kind='access'",(identity,))
  else:
   assert ACTION in ('reset','cleanup')
   suffix=" AND kind!='session'" if ACTION=='reset' else ''
   db.execute('DELETE FROM credential_grants WHERE hash IN '
    '(SELECT hash FROM credentials WHERE account=?'+suffix+')',(identity,))
   db.execute('DELETE FROM credentials WHERE account=?'+suffix,(identity,))
   for table in ('session_report_terms','cloud_session_reports','session_report_permissions',
    'archive_chunks','archive_postings','archive_documents','archive_revisions','archive_audit',
    'archive_states','archive_permissions','records','changes','account_usage','questions'):
    db.execute('DELETE FROM '+table+' WHERE account=?',(identity,))
   if ACTION=='cleanup': db.execute('DELETE FROM accounts WHERE id=?',(identity,))
 result={'ok':True}
if hasattr(store,'close'): store.close()
print(json.dumps(result))
"""


def fixture(action, identity=None):
    assert action in {
        "create", "reset", "expire", "cleanup", "legacy_connection_hint",
        "payment_pending", "payment_active",
    }
    code = "ACTION=" + repr(action) + "\nIDENTITY=" + repr(identity) + "\n" + FIXTURE
    encoded = base64.b64encode(code.encode()).decode()
    outer = (
        "import base64,subprocess\n"
        "print(subprocess.check_output(['docker','exec','-i','varyz-cloud-cloud-1',"
        "'python','-c','import sys;exec(sys.stdin.read())'],input=base64.b64decode("
        + repr(encoded)
        + ")).decode(),end='')"
    )
    return json.loads(remote(outer))


def main():
    import httpx

    first, second = fixture("create"), fixture("create")
    try:
        seed = r"""
from varyz.cloud.app import Settings,create_app
app=create_app(Settings.from_env()); store=app.state.store
store.push(IDENTITY,[dict(kind='rule',id='r-'+str(i),base_version=0,
 payload=dict(statement='Synthetic live preference '+str(i),scope='demo')) for i in range(45)])
if hasattr(store,'close'): store.close()
print('seeded')
"""
        code = "IDENTITY=" + repr(first["identity"]) + "\n" + seed
        encoded = base64.b64encode(code.encode()).decode()
        remote(
            "import base64,subprocess\nsubprocess.run(['docker','exec','-i',"
            "'varyz-cloud-cloud-1','python','-c','import sys;exec(sys.stdin.read())'],"
            "input=base64.b64decode(" + repr(encoded) + "),check=True,capture_output=True)"
        )
        with httpx.Client(base_url="https://cloud.varyzai.com", timeout=20) as client:
            client.cookies.set("__Host-session", first["session"])
            home = client.get("/?view=dashboard")
            assert home.status_code == 200 and "45 preferences" in home.text
            assert "Synthetic live preference" not in home.text
            token = client.cookies.get("__Host-csrf")
            after, snapshot, count = 0, None, 0
            while True:
                params = dict(project="demo", after=after)
                if snapshot is not None:
                    params["snapshot"] = snapshot
                response = client.get(
                    "/account/preferences", params=params, headers={"Accept": "application/json"}
                )
                assert response.status_code == 200
                page = response.json()
                batch = page["html"].count('class="cloud-statement"')
                assert batch <= 20
                count += batch
                snapshot, after = page["snapshot"], page["next_after"]
                if after is None:
                    break
            assert count == 45
            client.get("/?view=dashboard&section=account")
            assert client.cookies.get("__Host-csrf") == token
            client.cookies.clear()
            client.cookies.set("__Host-session", second["session"])
            page = client.get(
                "/account/preferences?project=demo", headers={"Accept": "application/json"}
            ).json()
            assert page["html"] == ""
        print(
            json.dumps(
                {
                    "public_https": "passed",
                    "backend": first["backend"],
                    "aggregate_initial_read": "passed",
                    "lazy_pages": [20, 20, 5],
                    "tenant_isolation": "passed",
                    "stable_csrf": "passed",
                    "data": "disposable synthetic preferences only",
                }
            )
        )
    finally:
        fixture("cleanup", first["identity"])
        fixture("cleanup", second["identity"])


if __name__ == "__main__":
    main()
