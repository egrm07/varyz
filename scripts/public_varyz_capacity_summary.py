"""Derive resource and SLO results from the saved constrained experiment."""

import argparse
import json
import math
from pathlib import Path


def counters(value):
    return {key: int(number) for key, number in (line.split() for line in value.splitlines())}


def resources(samples, cpu_limit):
    intervals = []
    throttled = periods = usage = elapsed = 0
    for previous, current in zip(samples, samples[1:], strict=False):
        before, after = counters(previous["cpu.stat"]), counters(current["cpu.stat"])
        seconds = current["seconds"] - previous["seconds"]
        delta = after["usage_usec"] - before["usage_usec"]
        intervals.append(delta / seconds / 1e6)
        usage += delta
        elapsed += seconds
        periods += after["nr_periods"] - before["nr_periods"]
        throttled += after["nr_throttled"] - before["nr_throttled"]
    return dict(
        samples=len(samples),
        mean_cpu_cores=round(usage / elapsed / 1e6, 3) if elapsed else None,
        mean_cpu_allocation_percent=round(100 * usage / elapsed / 1e6 / cpu_limit, 2)
        if elapsed
        else None,
        p95_sample_cpu_cores=round(sorted(intervals)[math.ceil(len(intervals) * 0.95) - 1], 3)
        if intervals
        else None,
        peak_sample_memory_mib=round(max(int(s["memory.current"]) for s in samples) / 1024**2, 2)
        if samples
        else None,
        cgroup_peak_memory_mib=round(max(int(s["memory.peak"]) for s in samples) / 1024**2, 2)
        if samples
        else None,
        throttled_periods_percent=round(100 * throttled / periods, 3) if periods else None,
    )


def summarize(data):
    phases = data.get("phases", [])
    steady = [p for p in phases if p["phase"].startswith("sustained-")]
    elapsed = 0
    windows = []
    for phase in phases:
        end = elapsed + phase["seconds"]
        if phase in steady:
            selected = [s for s in data["samples"] if elapsed + 5 <= s["seconds"] <= end - 5]
            windows.append(dict(phase=phase["phase"], **resources(selected, data["limits"]["cpu"])))
        elapsed = end
    delivered = (
        len(steady) == 3
        and data.get("generator_exit_code") == 0
        and data.get("aborted") is None
        and len(phases) == 9
        and all(
            p["seconds"] >= 599
            and not p["errors"]
            and not p["dropped"]
            and p["achieved_rps"] >= 0.99 * p["offered_rps"]
            for p in steady
        )
    )
    qualified = delivered and all(
        p["seconds"] >= 599
        and not p["errors"]
        and not p["dropped"]
        and p["achieved_rps"] >= 0.99 * p["offered_rps"]
        and p["p95_ms"] <= 500
        and p["p99_ms"] <= 1000
        and all(e["p95_ms"] <= 500 and e["p99_ms"] <= 1000 for e in p["endpoints"].values())
        for p in steady
    )
    health = [s["production_health_ms"] for s in data["samples"]]
    return dict(
        sustained_delivery_passed=delivered,
        sustained_slo_passed=qualified and data["aborted"] is None,
        criterion="Three >=599-second windows; >=99% offered rate; no errors/drops; "
        "overall and each endpoint p95 <=500ms and p99 <=1000ms",
        steady_requests=sum(p["completed"] for p in steady),
        steady_successful_requests=sum(
            p["completed"] - sum(p["errors"].values()) for p in steady
        ),
        steady_errors=sum(sum(p["errors"].values()) for p in steady),
        steady_seconds=round(sum(p["seconds"] for p in steady), 3),
        worst_steady_window_p95_ms=max((p["p95_ms"] for p in steady), default=None),
        worst_steady_window_p99_ms=max((p["p99_ms"] for p in steady), default=None),
        total_requests=sum(p["completed"] for p in phases),
        total_dropped=sum(p["dropped"] for p in phases),
        maximum_successful_follower_coverage=max(
            (p.get("cumulative_successful_followers", 0) for p in phases), default=0
        ),
        production_health_failures=sum(x is None for x in health),
        maximum_production_health_ms=max((x for x in health if x is not None), default=None),
        minimum_host_available_mib=round(
            min(s["host_available_bytes"] for s in data["samples"]) / 1024**2, 2
        ),
        resources_all=resources(data["samples"], data["limits"]["cpu"]),
        steady_resources=windows,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    print(json.dumps(summarize(json.loads(Path(args.path).read_text())), indent=2))
