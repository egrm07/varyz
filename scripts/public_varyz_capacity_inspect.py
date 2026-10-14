"""Read-only progress and integrity checks of this experiment's synthetic files."""

import argparse
import json
import re
import subprocess
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--wait-for-sustained", action="store_true")
    modes.add_argument("--progress-only", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not re.fullmatch(r"varyz-capacity-[a-f0-9]{12}", args.run_id):
        raise SystemExit("Expected this experiment's exact temporary container name")
    code = """
import json,sqlite3,sys,time
from pathlib import Path
path=Path('/tmp')/sys.argv[1]/'data/cloud.db'
if sys.argv[2]=='progress':
 print((path.parent/'load-results.json').read_text())
 raise SystemExit(0)
if sys.argv[2]=='wait':
 for _ in range(480):
  output=path.parent/'load-results.json'
  if output.exists() and any(p['phase']=='sustained-3' for p in json.loads(output.read_text())):
   break
  time.sleep(5)
 else: raise RuntimeError('Sustained phase completion not observed within 40 minutes')
db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=15)
db.execute('BEGIN')
checks=dict(quick_check=db.execute('PRAGMA quick_check').fetchone()[0],
 foreign_key_violations=len(db.execute('PRAGMA foreign_key_check').fetchall()),
 database_bytes=path.stat().st_size,
 accounts=db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],
 follows=db.execute('SELECT COUNT(*) FROM follows').fetchone()[0],
 publications=db.execute('SELECT COUNT(*) FROM public_varyz').fetchone()[0],
 publication_revision_range=list(db.execute(
  'SELECT MIN(revision),MAX(revision) FROM public_varyz').fetchone()),
 uses=dict(db.execute('SELECT kind,SUM(n) FROM public_uses GROUP BY kind').fetchall()),
 sync_records=db.execute("SELECT COUNT(*) FROM records WHERE account='syncer'").fetchone()[0],
 archive_documents=db.execute('SELECT COUNT(*) FROM archive_documents').fetchone()[0],
 vector_chunks=db.execute(
  'SELECT COUNT(*) FROM archive_chunks WHERE vector IS NOT NULL').fetchone()[0])
print(json.dumps(checks))
"""
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
        "python3 -c 'import sys;exec(sys.stdin.read())' "
        + args.run_id
        + (" progress" if args.progress_only else " wait" if args.wait_for_sustained else " now"),
    ]
    result = subprocess.run(
        command,
        input=code,
        text=True,
        capture_output=True,
        timeout=2450 if args.wait_for_sustained else 45,
    )
    if result.returncode:
        raise SystemExit(result.stderr[-1500:])
    value = json.loads(result.stdout)
    if not args.progress_only:
        value["run_id"] = args.run_id
    default = (
        ".tmp/public-varyz-capacity-progress.json"
        if args.progress_only
        else ".tmp/public-varyz-capacity-integrity.json"
    )
    output = Path(__file__).resolve().parents[1] / (args.output or default)
    output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if args.progress_only:
        print(
            json.dumps(
                [
                    {
                        k: p[k]
                        for k in (
                            "phase",
                            "completed",
                            "errors",
                            "dropped",
                            "p95_ms",
                            "p99_ms",
                            "cumulative_successful_followers",
                        )
                    }
                    for p in value
                ]
            )
        )
    else:
        print(json.dumps(value))
