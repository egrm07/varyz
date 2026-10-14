"""Open-loop real HTTP traffic; errors, lateness and dropped work stay visible."""

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx


def stats(values):
    values = sorted(values)
    return (
        {f"p{p}_ms": round(values[math.ceil(len(values) * p / 100) - 1], 3) for p in (50, 95, 99)}
        if values
        else {}
    )


async def main(args):
    root = Path(args.data)
    auth = json.loads((root / "auth.json").read_text())
    results = []
    sequence = 0
    follower_sequence = 0
    all_followers = set()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8081",
        timeout=10,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
    ) as client:

        async def phase(name, rate, seconds, workload="mixed"):
            nonlocal sequence
            latencies, endpoints = [], defaultdict(list)
            errors: Counter[str] = Counter()
            statuses: Counter[str] = Counter()
            users: set[int] = set()
            dropped, peak, late = 0, 0, []
            pending: set[asyncio.Task[None]] = set()
            started = time.monotonic()

            async def request(index: int, scheduled: float) -> None:
                nonlocal follower_sequence
                slot = index % 20
                if workload == "creator" and slot >= 16:
                    slot = 0
                identity = follower_sequence if slot < 14 else index
                person = (identity * 7919) % len(auth["followers"])
                if slot < 14:
                    follower_sequence += 1
                peer = person % args.ip_pool if args.ip_pool else person
                headers = {"X-Forwarded-For": f"198.18.{peer // 256}.{peer % 256}"}
                token = auth["followers"][person]
                method, endpoint = "POST", "brief"
                body: dict[str, object] | None = {"task": "Review this pull request"}
                path = "/api/sharing/v1/following/brief"
                if slot in (12, 13):
                    endpoint, path = "ask", "/api/sharing/v1/following/ask"
                    body = dict(
                        task="Review this pull request",
                        questions=["Should pull requests include tests?"],
                    )
                elif slot in (14, 15):
                    method, endpoint, path, body = "GET", "profile", "/@creator000", None
                elif slot in (16, 17):
                    endpoint, path, token = "history", "/mcp/archive", auth["history"]
                    body = dict(
                        jsonrpc="2.0",
                        id=index,
                        method="tools/call",
                        params=dict(
                            name="search",
                            arguments=dict(
                                query=f"SQLite bucket{index % 100}", project="capacity", limit=3
                            ),
                        ),
                    )
                elif slot == 18:
                    method, endpoint, path, token, body = (
                        "GET",
                        "sync_pull",
                        ("/api/sync?cursor=0&limit=20&wait=0"),
                        auth["syncer"],
                        None,
                    )
                elif slot == 19:
                    endpoint, path, token = "sync_push", "/api/sync", auth["syncer"]
                    body = dict(
                        records=[
                            dict(
                                kind="rule",
                                id=f"load{index}",
                                base_version=0,
                                payload=dict(
                                    scope="capacity", statement="Synthetic capacity sync."
                                ),
                            )
                        ]
                    )
                headers["Authorization"] = "Bearer " + token
                began = time.monotonic()
                late.append(max(0, (began - scheduled) * 1000))
                try:
                    response = await client.request(method, path, headers=headers, json=body)
                    statuses[str(response.status_code)] += 1
                    response.raise_for_status()
                    if endpoint == "brief":
                        assert response.json()["standards"][0]["creator"] == "@creator000"
                        users.add(person)
                    elif endpoint == "ask":
                        assert "Include tests for changed behavior." in response.text
                    elif endpoint == "profile":
                        assert "Code review standard" in response.text
                    elif endpoint == "history":
                        value = response.json()["result"]
                        assert not value.get("isError") and value["structuredContent"]["results"]
                        assert value["structuredContent"]["search_mode"] == "hybrid"
                    else:
                        assert response.json()["records"]
                    if slot < 14:
                        all_followers.add(person)
                except Exception as error:
                    errors[f"{endpoint}:{type(error).__name__}"] += 1
                finally:
                    elapsed = (time.monotonic() - began) * 1000
                    latencies.append(elapsed)
                    endpoints[endpoint].append(elapsed)

            for i in range(round(rate * seconds)):
                scheduled = started + i / rate
                await asyncio.sleep(max(0, scheduled - time.monotonic()))
                if len(pending) >= 32:
                    dropped += 1
                else:
                    task = asyncio.create_task(request(sequence, scheduled))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    peak = max(peak, len(pending))
                sequence += 1
                if i and i % max(1, round(rate * 60)) == 0:
                    print(
                        json.dumps(
                            dict(
                                progress=name,
                                elapsed=round(time.monotonic() - started),
                                completed=len(latencies),
                                errors=dict(errors),
                                **stats(latencies),
                            )
                        ),
                        flush=True,
                    )
            await asyncio.gather(*pending)
            elapsed = time.monotonic() - started
            result = dict(
                phase=name,
                workload=workload,
                source_ip_pool=args.ip_pool or len(auth["followers"]),
                offered_rps=rate,
                seconds=round(elapsed, 3),
                completed=len(latencies),
                achieved_rps=round(len(latencies) / elapsed, 3),
                errors=dict(errors),
                statuses=dict(statuses),
                dropped=dropped,
                peak_inflight=peak,
                unique_brief_users=len(users),
                cumulative_successful_followers=len(all_followers),
                scheduling=stats(late),
                **stats(latencies),
                endpoints={k: dict(requests=len(v), **stats(v)) for k, v in endpoints.items()},
            )
            results.append(result)
            temporary = root / "load-results.tmp"
            temporary.write_text(json.dumps(results, indent=2))
            temporary.replace(root / "load-results.json")
            print(json.dumps(result), flush=True)
            return result

        if args.maximum:
            # Declared before traffic: zero errors/drops, >=99% offered rate,
            # every endpoint p95 <=1s and p99 <=2s. Retain stricter old SLO too.
            def qualifies(value):
                return (
                    not value["errors"]
                    and not value["dropped"]
                    and value["achieved_rps"] >= 0.99 * value["offered_rps"]
                    and all(
                        endpoint["p95_ms"] <= 1000 and endpoint["p99_ms"] <= 2000
                        for endpoint in value["endpoints"].values()
                    )
                )

            for rate in (2, 5, 8, 10, 11, 12, 14, 16, 18):
                await phase(f"mixed-step-{rate}", rate, args.step_seconds)
            passing = [v for v in results if qualifies(v)]
            if passing:
                chosen = max(v["offered_rps"] for v in passing)
                print(json.dumps(dict(selected_mixed_rps=chosen)), flush=True)
                for n in range(3):
                    await phase(f"sustained-{n + 1}", chosen, args.duration / 3)
            for rate in (10, 20, 30, 40, 45, 49, 55):
                await phase(f"creator-step-{rate}", rate, 60, "creator")
            passing = [v for v in results if v["workload"] == "creator" and qualifies(v)]
            if passing:
                chosen = max(v["offered_rps"] for v in passing)
                print(json.dumps(dict(selected_creator_rps=chosen)), flush=True)
                # The deliberate 55-rps overload can fill the current fixed
                # minute. Clear that window before judging steady delivery.
                print(json.dumps(dict(cooldown_seconds=60)), flush=True)
                await asyncio.sleep(60)
                for n in range(3):
                    await phase(f"creator-sustained-{n + 1}", chosen, 200, "creator")
            return

        for rate in (5, 10, 20, 30, 40):
            await phase(f"step-{rate}", rate, args.step_seconds)
        for n in range(3):
            await phase(f"sustained-{n + 1}", args.rate, args.duration / 3)
        await phase("burst-40", 40, args.step_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="/probe")
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--step-seconds", type=int, default=60)
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--maximum", action="store_true")
    parser.add_argument("--ip-pool", type=int, default=0)
    asyncio.run(main(parser.parse_args()))
