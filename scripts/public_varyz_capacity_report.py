"""Write the measured report, preserving failed latency and overload findings."""

import json
from pathlib import Path

from public_varyz_capacity_summary import summarize

ROOT = Path(__file__).resolve().parents[1]


def main():
    data = json.loads((ROOT / ".tmp/public-varyz-capacity-hybrid.json").read_text())
    if (
        data.get("generator_exit_code") != 0
        or data.get("aborted")
        or len(data.get("phases", [])) != 9
    ):
        raise SystemExit("Only a completed sustained experiment may produce the final report")
    summary = summarize(data)
    integrity = json.loads((ROOT / ".tmp/public-varyz-capacity-integrity.json").read_text())
    live = json.loads((ROOT / ".tmp/public-varyz-live-status.json").read_text())["runtime"]
    data.update(summary=summary, integrity_after_sustained=integrity, live_runtime=live)
    failure = json.loads((ROOT / ".tmp/public-varyz-capacity-limiter-failure.json").read_text())
    if failure["run_id"] != data["run_id"] or integrity["run_id"] != data["run_id"]:
        raise SystemExit("Evidence belongs to a different experiment; do not reuse stale findings")
    data["limiter_failure"] = failure
    stem = ROOT / "docs/PUBLIC_VARYZ_SERVER_CAPACITY_2026-10-11"
    stem.with_suffix(".json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    rows = []
    for phase in data["phases"]:
        rows.append(
            f"| {phase['phase']} | {phase['offered_rps']} | {phase['completed']:,} | "
            f"{sum(phase['errors'].values()):,} | {phase['dropped']:,} | "
            f"{phase['p95_ms']:.0f} | {phase['p99_ms']:.0f} |"
        )
    resource_rows = [
        f"| {p['phase']} | {p['mean_cpu_cores']} | "
        f"{p['mean_cpu_allocation_percent']}% | {p['peak_sample_memory_mib']} | "
        f"{p['throttled_periods_percent']}% |"
        for p in summary["steady_resources"]
    ]
    verdict = (
        "The candidate sustained 10 mixed requests/s for approximately 30 minutes "
        "with no request failures or dropped work."
        if summary["sustained_delivery_passed"]
        else "The candidate failed to sustain 10 mixed requests/s without errors or drops."
    )
    target = "passed" if summary["sustained_slo_passed"] else "failed"
    report = f"""# Public Varyz server capacity verification

**{verdict} The 500 ms p95 / 1,000 ms p99 latency target {target}.**

Measured on 2026-10-11 using the existing server's actual 1.5 CPU and 1,536 MiB
container limits. This verifies an isolated candidate under those limits; the
Public Varyz feature remains undeployed and the live registration cap remains 100.

## Program

- Source snapshot SHA-256: `{data["source_sha256"]}`.
- The measured fixture, generator and pre-fix store are retained locally in
  `.tmp/capacity-tested-source/`. Subsequent local changes add harness typing
  cleanup/PIPE checks and the limiter repair below. This load result is
  from the original candidate, not a sustained test of the repaired code.
- Base image: `{data["base_image"]}`; run began `{data["started_utc"]}`.
- 10,000 inactive free follower accounts, 50 paid synthetic creators, 50 follows
  per follower (500,000 relationships), 20 rules and one answer per publication.
- One synthetic paid history account with 1,000 encrypted documents and 384-value
  encrypted vectors. Queries must return matching evidence and `hybrid` search;
  silent keyword fallback is a failed content check.
- The production image's cached multilingual embedding model runs locally.
  No downloads, external model calls, mail or billing operations occur.
- Initial database: {data["fixture"]["database_bytes"] / 1024**2:.2f} MiB. The
  probe uses the production disk device, with a separate disposable directory.
- One Uvicorn worker, concurrency limit 32, backlog 64 and five-second keepalive.
  The real HTTP generator shares the 1.5 CPU/1,536 MiB budget with the candidate,
  so generation overhead is included. HTTP uses private loopback sockets.
- Fixed 20-request mix: 60% followed briefs, 10% followed answers, 10% public
  profile HTML, 10% semantic history searches, 5% sync pulls and 5% sync pushes.
  Paid sync traffic writes distinct new records; empty no-op syncs are not used.
- Sixty-second offered-rate steps at 5, 10, 20, 30 and 40 requests/s; three
  consecutive ten-minute windows at 10 requests/s; a final 40 requests/s burst.
- Open-loop scheduling keeps offered load independent of response speed. At 32
  in-flight requests, extra scheduled work is counted as dropped rather than
  hidden in a client queue. No retries hide errors. All returned content is checked.
- Auth and global/per-IP limits remain enabled. Synthetic distributed addresses
  reach Uvicorn through trusted loopback proxy headers, as through the live edge.
- Preference quota 1 MiB, 10,000 changes and history quota 128 MiB match live.
  The synthetic fixture is seeded directly; it does not test mass registrations.

## Blocking limiter defect

During the third steady window, the synthetic `rate_limits` table reached its
10,000-row ceiling. The SQLite limiter applies that ceiling even when resetting
an existing key into a new minute. Consequently, the existing `global` counter
could not roll over, and **every candidate route, including health, returned 429
Service rate limit**. Inspection found {failure['rate_keys']:,} rate keys and an
old global count of only {failure['global_counter_count']}, below the advertised
3,000/minute request budget. This is key-cardinality exhaustion, not 50 requests/s.

The targeted local repair checks table capacity only when inserting a new key.
Both regression cases (global and existing peer counters) failed before the fix
and pass after it; they also prove new keys remain rejected at the ceiling and
the normal per-window request limit is preserved. The focused rate-limit,
signup and security suite passed **45 tests**. Ruff and checked typing passed
for the changed store and probe scripts. **The fix is not deployed and
has not completed a fresh constrained sustained test.** The unchanged 10,000-key
ceiling still limits new IP/category combinations retained over two hours;
supporting a wider audience requires an explicit capacity policy and retest.

## Results

Percentiles include failed responses in overload phases. Drops are additional
offered requests that could not enter the bounded generator. Error counts
include HTTP failures, timeouts and content-validation failures.

| Phase | Offered/s | Completed | Errors | Dropped | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

The sustained windows returned {summary["steady_requests"]:,} HTTP responses in
{summary["steady_seconds"]:.3f} seconds: {summary["steady_successful_requests"]:,}
successful requests and {summary["steady_errors"]:,} failures. The final burst's
fast responses were all 429 rejections, not increased useful throughput.
The sustained windows' worst overall p95 was
{summary["worst_steady_window_p95_ms"]:.0f} ms and worst p99 was
{summary["worst_steady_window_p99_ms"]:.0f} ms. Across the experiment,
{summary["maximum_successful_follower_coverage"]:,} distinct free follower
identities successfully received a brief or answer.

The latency gate requires every sustained window to deliver at least 99% of the
offered rate without errors or drops, with overall **and every endpoint** p95
at most 500 ms and p99 at most 1,000 ms. Gate result: **{target}**. Successful
delivery alone does not establish that latency target. Endpoint percentiles and
raw five-second resource samples are in the adjacent JSON.

| Window | Mean CPU cores | CPU allocation used | Peak sampled MiB | CPU periods throttled |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(resource_rows)}

Whole-run cgroup memory high-water mark, including fixture/model preparation:
{summary["resources_all"]["cgroup_peak_memory_mib"]:.2f} MiB. Resource sampling
also includes the generator and monitoring processes, and memory includes page cache.

## Integrity and live isolation

The read-only synthetic database check after the sustained phase returned
`quick_check={integrity["quick_check"]}`, {integrity["foreign_key_violations"]} foreign-key
violations, {integrity["follows"]:,} follows, {integrity["publications"]} publications
and {integrity["vector_chunks"]:,} vector chunks. Publication revisions remained
{integrity["publication_revision_range"]}. This check runs during the final burst;
it is not an exact end-of-run reconciliation of every usage counter.

The live service had {summary["production_health_failures"]} failed sampled health
checks; its slowest sampled response was {summary["maximum_production_health_ms"]:.1f} ms.
Minimum host available memory was {summary["minimum_host_available_mib"]:.0f} MiB.
The guard stops the temporary candidate after repeated live health deterioration
or available memory below 512 MiB. The live image was unchanged, the final health
check succeeded, and the candidate container, image tag and synthetic files were
removed. No production volumes, credentials or public routes were given to it.

## Meaning and limits

10,000 **stored followers** with maximal outgoing follows fit this tested
fixture. This is not certification of 10,000 concurrently active people. At the
tested mix, 10 total requests/s contains seven follower requests/s. For an
illustrative burst where 10% of daily follower requests arrive in ten minutes,
that equals 2,100 daily active followers at 20 follower requests/day, or 42,000
at one/day. Those are arithmetic scenarios, not tested user capacities or a
subsecond service guarantee. Ten thousand people at 20 requests/day would
produce 33.3 follower requests/s in that burst and exceed the tested range.

The live cap is still 100 accounts ({live["counts"]["accounts"]} currently registered),
the sharing pilot is restricted, and Public Varyz routes are absent. This run
changes no rollout gates. Keep the earlier 1,000-account planning target
provisional; higher throughput and latency assurances need performance work
and another measured run before expanding it.

This workload has 1,000 history documents in one account, one warm model, 50
cached creator publications and a deterministic request mix. Full-quota
histories, many distinct creators, publication/cache churn, follow/unfollow
writes, registration/password hashing, email delivery, long polling, web assets,
TLS, internet latency, private collaborations and failover are not load-tested.
The three sustained windows share one process and database; they are consecutive
observations, not independent fresh-server replications. The server also hosts
the lightly used live pilot; the generator is co-located rather than remote.

The short keyword-only probe was stopped when the live semantic-search setting
was discovered. Its faster figures and the earlier local ASGI benchmark are
preliminary evidence only and are not the server capacity verdict.

## Reproduce

```powershell
.venv\\Scripts\\python.exe scripts/public_varyz_capacity_remote.py `
  --duration 1800 --step-seconds 60 --rate 10 `
  --output .tmp/public-varyz-capacity-hybrid.json
.venv\\Scripts\\python.exe scripts/public_varyz_capacity_summary.py `
  .tmp/public-varyz-capacity-hybrid.json
```

This requires the owner's existing SSH access and Docker on the existing server.
The script builds from its current image without network access and creates only
its uniquely named temporary resources. Run the integrity helper with the printed
run ID and `--wait-for-sustained` while the experiment is active, then use
`scripts/public_varyz_capacity_report.py` to regenerate this dated report and JSON
from the original run files. Its failure evidence must match that exact run ID;
use the summary command for new experiments rather than reusing old diagnostics.
"""
    stem.with_suffix(".md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
