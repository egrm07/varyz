"""Write the dated report from recorded rollout and real HTTP acceptance results."""

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp/backpressure"
stage = json.loads((OUT / "stage.json").read_text())
deployment = json.loads((OUT / "deployment.json").read_text())
verification = json.loads((OUT / "verification.json").read_text())
assert stage["id"] == deployment["id"] == verification["id"]
report = ROOT / "docs/CLOUD_SPIKE_PROTECTION_2026-10-11"
result = {
    "deployment": deployment,
    "stage": stage,
    "verification": verification,
    "checks": {
        "initial_suite_passed": 116,
        "final_admission_suite_passed": 11,
                     "ruff": "passed for admission, app, store, client and new scripts/tests",
        "mypy": "passed for admission and client, follow-imports=skip",
    },
    "scope": "account cap and bounded server admission; no creator-feature launch",
}
report.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
rows = []
for index, run in enumerate(stage["results"]):
    label = "Before" if not run["candidate"] else f"Candidate {index}"
    bursts = "; ".join(
        f"{b['burst']}: "
        + ", ".join(f"{number} HTTP {code}" for code, number in b["codes"].items())
        for b in run["http"]
    )
    health = Counter(run["health_codes"])
    rows.append(
        f"| {label} | {bursts} | {run['verified_sync_writes']} | "
        f"{health[200]}/{sum(health.values())} | "
        f"{int(run['resources']['memory.peak']) / 1024**2:.2f} |"
    )
live_codes = dict(Counter(r["status"] for r in verification["benign_live_burst"]))
content = f"""# Cloud spike protection

> **Live on 2026-10-11** — account cap raised from **100 to 10,000**;
> bounded admission queues short usage spikes before database/body/endpoint work.
> Deployment: `{deployment["deployed_utc"]}`; release `{deployment["id"]}`.

## Program

1. Capture the actual loaded live source and verify its source copy matches.
2. Overlay only the account default, admission middleware, safe retry headers,
   idle-sync release and the rate-counter rollover repair. Preserve all other
   environment values, resources, data and feature gates.
3. Compare the old image with two independent candidate runs, each using a fresh
   synthetic SQLite database containing 10,000 accounts and real loopback HTTP.
4. Back up production SQLite online, retain the old image and Compose file,
   deploy the tested image, then check effective settings, hashes and public health.

## Limits and behavior

| Protection | Effective setting |
| --- | --- |
| Expensive requests running | 8 globally; 2 per credential/client identity |
| Requests waiting | 64 globally; 4 total running/waiting per identity |
| Source address bound | 16 running/waiting requests across identities |
| Queue wait deadline | 10 seconds |
| Queue full or deadline reached | HTTP 503 before work, `Retry-After: 2`, `X-Varyz-Retry-Safe: 1` |
| Existing rate quotas | Preserved; HTTP 429 now includes time until the next minute |
| Idle sync waits | Existing global limit 8, per-account limit 4; release admission slots |
| Protocol bounds | One Uvicorn worker, concurrency 128, backlog 128, keep-alive 2 seconds |
| Production resources | 1.5 CPU, 1,536 MiB RAM; existing swap/pid limits preserved |

The queue schedules credential identities round-robin and preserves order within
each identity. Authentication still runs normally after admission. Rotating fake
tokens cannot exceed the per-address bound. Multiple credentials belonging to one
account remain separate scheduling identities; the existing account-specific
sync-wait bounds still apply. Queued requests do not buffer application bodies.

`/health` bypasses admission and database rate counters. Health confirms process
availability; a separate normal metadata request verifies application routing.
Existing account storage (1 MiB), changes (10,000), archive (128 MiB), daily email
and client-registration quotas are preserved. The rate-key table remains bounded
at 10,000 keys; existing keys can now roll over at that ceiling.

## Results against baseline

The controlled handler waits 200 ms to exercise queue pressure reproducibly.
Each run then sends three bursts of 64 real authenticated `/api/sync` writes and
reads every accepted record back. This tests burst admission and recovery, not
semantic-search throughput. The load generator shares the candidate cgroup.
CPU and RAM match production; test swap is disabled, stricter than production.

| Run | Controlled bursts and responses | Verified sync writes | Health 200 / samples | Peak MiB |
| --- | --- | --- | --- | --- |
{chr(10).join(rows)}

Both candidates reached exactly 8 active and 64 queued requests, drained to zero,
retained correct SQLite/foreign-key integrity, and recorded zero OOM kills.
In each noisy-client check, 4 requests completed and 12 were safely rejected;
a different client completed. Eight idle long polls left zero active/queued
work slots occupied. All 192 real sync writes per candidate completed and were
read back without missing or duplicate records. Chance is not a meaningful
baseline for deterministic admission; the old image supplies the comparison.

Offline checks: **116 tests passed** initially (admission, rate-key capacity,
signup, service/security and live-sync logic). The final admission suite contains
**11 passing tests**, including the added busy-rate-store health regression.
Ruff passes for admission, app, store, client and new scripts/tests. Mypy passes for
admission and client with `--follow-imports=skip`; no whole-repository clean-check
claim is made for this checkout's unrelated changes.

## Live verification

Verified at `{verification["verified_utc"]}`: effective cap **10,000**, existing
accounts **{deployment["verified"]["accounts"]}**, SQLite quick-check **ok**,
deployed source hashes match the tested image, and all unrelated environment
values and resource limits match the original container. Public creator routes
remain absent and the existing two-account sharing pilot remains unchanged.

All **{len(verification["health_ms"])}** public health checks returned HTTP 200
(maximum **{max(verification["health_ms"]):.2f} ms**). A small anonymous metadata
burst returned `{live_codes}`; admitted requests exposed measured queue delay and
every rejected request carried the safe retry marker and two-second delay.
Container restarts **0**, OOM-killed **false**.

## Rollback and retained evidence

- Live image: `{deployment["image"]}` (`{deployment["image_id"]}`).
- Rollback image: `{deployment["rollback_image"]}`.
- Original Compose file: `{deployment["rollback_compose"]}`.
- Protected server-only SQLite backup: `{deployment["database_backup"]}` (0600).
- Restore the original Compose file and run the same Compose project/config
  stack with `up -d --no-build cloud` to roll back code/configuration. This release
  makes no schema migration; do not restore the database over newer user writes
  merely to roll back the image.
- Exact source hashes, all measurements and verification: this report's JSON twin.
- Deployment preparation, acceptance and rollout: `scripts/cloud_spike_rollout.py`
  and `scripts/cloud_spike_probe.py`. No test container remains running.

## Practical bounds

**10,000 is the account ceiling, not simultaneous processing capacity**. Sustained
overload beyond the finite queue returns retry responses; unbounded waiting would
itself risk exhaustion. Uvicorn can also return its own unmarked 503 if the outer
128-connection/task bound is reached. The queue covers short spikes; it does not
add CPU, storage, or protection against every infrastructure failure.

The client in this checkout now retries explicit rate rejections and marked
before-work 503s up to three times, honoring bounded Retry-After with exponential
backoff and jitter. It never automatically replays ambiguous network/storage
failures. This client code needs the normal client release to reach other installed
clients; existing users receive the live server queue and retry headers now.

The earlier sustained mixed/creator capacity measurements remain historical
results for their tested candidate (`docs/PUBLIC_VARYZ_MAXIMUM_CAPACITY_2026-10-11.md`).
This release has not remeasured maximum semantic-history throughput or tested
10,000 simultaneously active users. A new sustained workload measurement is the
next capacity experiment if that ceiling is needed.
"""
report.with_suffix(".md").write_text(content, encoding="utf-8")
print(str(report.with_suffix(".md")))
