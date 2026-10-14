"""Validate and report the declared maximum-capacity experiment without hiding overload."""

import json
from pathlib import Path

from public_varyz_capacity_summary import resources

ROOT = Path(__file__).resolve().parents[1]


def delivery(phase):
    return (
        not phase["errors"]
        and not phase["dropped"]
        and phase["achieved_rps"] >= 0.99 * phase["offered_rps"]
        and phase["completed"] > 0
    )


def qualifies(phase, p95=1000, p99=2000):
    return delivery(phase) and all(
        endpoint["p95_ms"] <= p95 and endpoint["p99_ms"] <= p99
        for endpoint in phase["endpoints"].values()
    )


def summarize(data):
    assert data["plan"]["maximum"]
    assert data["generator_exit_code"] == 0 and data["aborted"] is None
    assert data["production_image_unchanged"] and data["candidate_resources_removed"]
    assert data["integrity"]["quick_check"] == "ok"
    assert data["integrity"]["foreign_key_violations"] == 0
    phases = data["phases"]
    assert len(phases) == 22, "An incomplete experiment cannot certify maximum capacity"
    for phase in phases:
        expected = {"brief", "ask", "profile"}
        if phase["workload"] == "mixed":
            expected |= {"history", "sync_pull", "sync_push"}
        assert set(phase["endpoints"]) == expected
        assert sum(v["requests"] for v in phase["endpoints"].values()) == phase["completed"]
    windows = []
    elapsed = 0
    for phase in phases:
        if phase["phase"] == "creator-sustained-1":
            elapsed += 60  # Declared fixed-minute recovery after 55-rps overload.
        end = elapsed + phase["seconds"]
        if "sustained" in phase["phase"]:
            samples = [s for s in data["samples"] if elapsed + 5 <= s["seconds"] <= end - 5]
            windows.append(dict(phase=phase["phase"], **resources(samples, data["limits"]["cpu"])))
        elapsed = end
    workloads = {}
    for workload, prefix, duration in (
        ("mixed", "sustained-", data["plan"]["duration"] / 3),
        ("creator", "creator-sustained-", 200),
    ):
        scan = [p for p in phases if p["phase"].startswith(workload + "-step-")]
        steady = [p for p in phases if p["phase"].startswith(prefix)]
        assert len(steady) == 3
        rate = steady[0]["offered_rps"]
        assert all(p["offered_rps"] == rate for p in steady)
        complete = all(
            p["seconds"] >= duration - 1 and p["completed"] + p["dropped"] == round(rate * duration)
            for p in steady
        )
        passing = [p["offered_rps"] for p in scan if qualifies(p)]
        assert rate == max(passing), "Confirmation must use the highest qualifying scanned rate"
        workloads[workload] = dict(
            selected_rps=rate,
            sustained_delivery_passed=complete and all(delivery(p) for p in steady),
            sustained_slo_passed=complete and all(qualifies(p) for p in steady),
            previous_strict_slo_passed=complete and all(qualifies(p, 500, 1000) for p in steady),
            seconds=round(sum(p["seconds"] for p in steady), 3),
            responses=sum(p["completed"] for p in steady),
            errors=sum(sum(p["errors"].values()) for p in steady),
            drops=sum(p["dropped"] for p in steady),
            worst_window_p95_ms=max(p["p95_ms"] for p in steady),
            worst_window_p99_ms=max(p["p99_ms"] for p in steady),
            worst_endpoint_p95_ms=max(e["p95_ms"] for p in steady for e in p["endpoints"].values()),
            worst_endpoint_p99_ms=max(e["p99_ms"] for p in steady for e in p["endpoints"].values()),
            scan_highest_error_free_rps=max(
                (p["offered_rps"] for p in scan if delivery(p)), default=0
            ),
            scan_highest_qualifying_rps=max(passing),
            scan_highest_previous_strict_rps=max(
                (p["offered_rps"] for p in scan if qualifies(p, 500, 1000)), default=0
            ),
            next_higher_scanned_slo_failure_rps=min(
                (p["offered_rps"] for p in scan if p["offered_rps"] > rate and not qualifies(p)),
                default=None,
            ),
        )
    health = [s["production_health_ms"] for s in data["samples"]]
    return dict(
        criterion="Zero errors/drops, >=99% offered rate, every endpoint p95<=1000ms/p99<=2000ms",
        criterion_origin=(
            "Technical test criterion declared before this run; not an owner-approved SLA"
        ),
        previous_strict_criterion="Every endpoint p95<=500ms/p99<=1000ms, separately retained",
        workloads=workloads,
        total_responses=sum(p["completed"] for p in phases),
        total_errors=sum(sum(p["errors"].values()) for p in phases),
        total_drops=sum(p["dropped"] for p in phases),
        successful_follower_coverage=max(p["cumulative_successful_followers"] for p in phases),
        production_health_checks=len(health),
        production_health_failures=sum(x is None for x in health),
        maximum_production_health_ms=max(x for x in health if x is not None),
        minimum_host_available_mib=round(
            min(s["host_available_bytes"] for s in data["samples"]) / 1024**2, 2
        ),
        resources_all=resources(data["samples"], data["limits"]["cpu"]),
        sustained_resources=windows,
    )


def report(data, summary):
    mixed, creator = (summary["workloads"][k] for k in ("mixed", "creator"))
    lines = [
        "# Public Varyz maximum capacity measurement",
        "",
        f"**Highest confirmed tested rates: {creator['selected_rps']} creator-only requests/s "
        f"for ten minutes; {mixed['selected_rps']} mixed requests/s for thirty minutes. "
        "Both passed with zero errors/drops and every endpoint p95 <=1 s / p99 <=2 s.**",
        "",
        f"Measured {data['started_utc']} on the actual 1.5 CPU / 1,536 MiB allocation.",
        "The repaired candidate remains undeployed. Live registration remains capped at 100.",
        "",
        "## Program",
        "",
        f"- Run: `{data['run_id']}`; source SHA-256 `{data['source_sha256']}`.",
        f"- Base image: `{data['base_image']}`.",
        "- Exact uploaded source retained at `.tmp/public-varyz-maximum-tested-source.tar`; "
        "its reconstructed content hash is checked against the measured snapshot.",
        "- Isolated synthetic data on the production disk device; no live volume or secrets "
        "mounted.",
        "- One Uvicorn worker, concurrency 32, backlog 64; generator shares the candidate cgroup.",
        "- 10,000 followers, 50 creators, 500,000 follows; 20 rules and one answer per creator.",
        "- Fixture accounts are seeded directly; mass signup, verification mail and OAuth "
        "client registration are not exercised.",
        "- 1,000 encrypted history documents with real cached-model vectors and hybrid-search "
        "assertions.",
        "- Mixed traffic: 60% brief, 10% ask, 10% profile, 10% hybrid history, 5% pull, 5% "
        "unique sync writes.",
        "- Creator-only traffic: 80% brief, 10% ask, 10% profile; no history or sync.",
        "- 1,000 synthetic source IPs distributed across 10,000 account tokens, with all "
        "limiters enabled.",
        "  This is an explicit shared-IP scenario separating throughput from key admission "
        "capacity.",
        "- Mixed rates 2, 5, 8, 10, 11, 12, 14, 16, 18/s for 90 seconds each; select the "
        "highest qualifying rate.",
        "  Confirm in three consecutive ten-minute windows, with no retry or hidden queue.",
        "- Creator rates 10, 20, 30, 40, 45, 49, 55/s for 60 seconds each; recover for a full "
        "minute",
        "  after overload, then confirm the highest qualifying rate in three 200-second windows.",
        "- Criterion declared before traffic: zero errors/drops, >=99% offered rate, every "
        "endpoint",
        "  p95 <=1,000 ms and p99 <=2,000 ms. This is a test criterion, not an owner-approved SLA.",
        "- The previous 500-ms p95 / 1,000-ms p99 criterion is retained separately below.",
        "- An earlier partial preflight was stopped to add the cooldown; it is retained at",
        "  `.tmp/public-varyz-maximum-preflight-stopped.json` and excluded from certification.",
        "",
        "## Results",
        "",
        "| Workload | Selected requests/s | Confirmation seconds | Responses | Errors / drops "
        "| Latency criterion passed | Previous strict criterion passed |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for name, value in (("Mixed", mixed), ("Creator only", creator)):
        lines.append(
            f"| {name} | {value['selected_rps']} | {value['seconds']} | {value['responses']} | "
            f"{value['errors']} / {value['drops']} | {value['sustained_slo_passed']} | "
            f"{value['previous_strict_slo_passed']} |"
        )
    lines += [
        "",
        "These are the highest qualifying rates on the declared test grid, not universal "
        "hardware maxima.",
        "Short scan delivery ceilings and the next latency failure are retained in the JSON.",
        "Percentiles in overload rows include failed responses, so fast rejections cannot "
        "certify capacity.",
        "",
        "| Phase | Offered /s | HTTP responses | Errors | Drops | Overall p95 ms | Overall p99 "
        "ms | Worst endpoint p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in data["phases"]:
        lines.append(
            f"| {p['phase']} | {p['offered_rps']} | {p['completed']} | "
            f"{sum(p['errors'].values())} | {p['dropped']} | {p['p95_ms']} | {p['p99_ms']} | "
            f"{max(e['p95_ms'] for e in p['endpoints'].values())} |"
        )
    lines += [
        "",
        "## Audience and hard limits",
        "",
        "- Stored-follower scale tested: 10,000 accounts and 500,000 relationships. This is "
        "not a maximum storage measurement.",
        f"- Successfully served distinct fixture followers: "
        f"{summary['successful_follower_coverage']}.",
        "- Live registration cap: 100 accounts. Public creator routes and repaired limiter are "
        "not deployed.",
        "- Global budget: 3,000 HTTP requests per fixed minute, nominally 50/s for the whole "
        "service.",
        "  The tested creator workload already overloads at 40/s, before reaching that budget.",
        "- Per-IP general budget: 120/min; auth budget: 12/min. One shared IP cannot sustain "
        "service-wide throughput.",
        "- SQLite rate-counter admission ceiling: 10,000 retained keys (IP/category plus "
        "internal keys),",
        "  retained up to two hours. This is fewer than 10,000 independent new IP/category "
        "combinations.",
        "  This run uses 1,000 IPs and therefore does not certify 10,000 distinct public IPs.",
        "- Existing-key rollover at full counter capacity is covered by regression tests; "
        "new-key rejection remains enabled.",
        "- Each follower can follow at most 50 creators. Per-account preference/history quotas "
        "remain 1/128 MiB.",
        "",
        "| Assumed usage | Mixed-rate equivalent active users | Creator-only equivalent active "
        "users |",
        "|---|---:|---:|",
        f"| One request per user per minute | {mixed['selected_rps'] * 60} | "
        f"{creator['selected_rps'] * 60} |",
        f"| Two requests per user per minute | {mixed['selected_rps'] * 30} | "
        f"{creator['selected_rps'] * 30} |",
        "",
        "These are rate conversions under evenly spread traffic, not additional measured "
        "audiences or registration capacity.",
        "A task issuing multiple tool calls consumes multiple requests; peaks and paid traffic "
        "reduce the audience budget.",
        "",
        "## Integrity and production impact",
        "",
        f"- SQLite quick check: {data['integrity']['quick_check']}; foreign-key violations: "
        f"{data['integrity']['foreign_key_violations']}.",
        f"- Live health checks: {summary['production_health_checks']}; failures: "
        f"{summary['production_health_failures']}; maximum "
        f"{summary['maximum_production_health_ms']} ms.",
        f"- Candidate peak memory: {summary['resources_all']['cgroup_peak_memory_mib']} MiB; "
        f"minimum available host memory: {summary['minimum_host_available_mib']} MiB.",
        "- Live image unchanged; disposable container, image and synthetic files removed.",
        "",
        "## Limits of the result",
        "",
        "The three continuous windows are not independent fresh-server replications. "
        "Creator-only confirmation is ten minutes,",
        "mixed confirmation thirty minutes; neither proves a 24-hour rate. Traffic uses "
        "loopback rather than the public TLS/proxy",
        "path. Registration bursts, publication/edit churn, full 128-MiB archives, more "
        "creators, cold model loading, other paid",
        "workload proportions, host contention and internet latency need their own "
        "measurements. Generator overhead is included",
        "in the candidate CPU allocation, which makes this a conservative candidate experiment "
        "rather than a production SLA.",
        "The load calls the HTTP following API and profile HTML directly. Browser asset fetches, "
        "MCP envelope overhead and periodic client following-list calls are not included.",
        "Sync pulls use wait=0; concurrent long-polling clients need a separate capacity test.",
        "",
        "The earlier failed limiter experiment remains in "
        "`docs/PUBLIC_VARYZ_SERVER_CAPACITY_2026-10-11.md`.",
        "The raw current run, all failures, resource samples and validation are retained in "
        "the adjacent JSON.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raw = json.loads((ROOT / ".tmp/public-varyz-maximum-capacity.json").read_text())
    snapshot = json.loads((ROOT / ".tmp/public-varyz-maximum-tested-source.json").read_text())
    assert snapshot["run_id"] == raw["run_id"]
    assert snapshot["source_sha256"] == raw["source_sha256"]
    raw["retained_source"] = snapshot
    live = json.loads((ROOT / ".tmp/public-varyz-live-status.json").read_text())
    assert live["nano_cpus"] / 1e9 == raw["limits"]["cpu"]
    assert live["memory_bytes"] == raw["limits"]["memory_bytes"]
    assert live["runtime"]["max_accounts"] == 100
    assert not live["runtime"]["following_module"] and not live["runtime"]["profile_route"]
    raw["final_live_metadata"] = dict(
        memory_bytes=live["memory_bytes"], nano_cpus=live["nano_cpus"], runtime=live["runtime"]
    )
    summary = summarize(raw)
    target = ROOT / "docs/PUBLIC_VARYZ_MAXIMUM_CAPACITY_2026-10-11"
    target.with_suffix(".json").write_text(
        json.dumps(dict(summary=summary, experiment=raw), indent=2) + "\n", encoding="utf-8"
    )
    target.with_suffix(".md").write_text(report(raw, summary), encoding="utf-8")
    print(json.dumps(summary, indent=2))
