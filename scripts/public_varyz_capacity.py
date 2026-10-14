"""Offline synthetic follower capacity probe; never opens the live database.

Measures the real service path (routing, decryption cache, SQLite and usage writes).
This is a local microbenchmark, not a production capacity guarantee.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet

from varyz.cloud.app import Settings, create_app
from varyz.cloud.sharing_service import identifier

ROOT = Path(__file__).resolve().parents[1]


def size(store) -> int:
    with store.connection() as db:
        return (
            db.execute("PRAGMA page_count").fetchone()[0]
            * db.execute("PRAGMA page_size").fetchone()[0]
        )


def measure(call, requests: int, concurrency: int) -> dict:
    def one(index):
        started = time.perf_counter()
        result = call(index)
        assert result is None or result["standards"][0]["creator"] == "@creator000"
        return (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = sorted(pool.map(one, range(requests)))
    elapsed = time.perf_counter() - started
    return dict(
        requests=requests,
        concurrency=concurrency,
        seconds=round(elapsed, 4),
        requests_per_second=round(requests / elapsed, 2),
        p50_ms=round(statistics.median(samples), 3),
        p95_ms=round(samples[math.ceil(len(samples) * 0.95) - 1], 3),
    )


async def measure_http(app, tokens, repeats):
    import httpx

    runs = []
    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=app, client=(f"192.0.2.{i % 200 + 1}", 50000)
                    ),
                    base_url="https://synthetic.example.test",
                )
            )
            for i in range(len(tokens))
        ]
        for concurrency in (1, 8):
            for _ in range(repeats):
                gate = asyncio.Semaphore(concurrency)

                async def one(i, semaphore=gate):
                    async with semaphore:
                        started = time.perf_counter()
                        response = await clients[i].post(
                            "/api/sharing/v1/following/brief",
                            headers={"Authorization": "Bearer " + tokens[i]},
                            json={"task": "Review this pull request"},
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["standards"][0]["creator"] == "@creator000"
                        return (time.perf_counter() - started) * 1000

                started = time.perf_counter()
                samples = sorted(await asyncio.gather(*(one(i) for i in range(len(tokens)))))
                elapsed = time.perf_counter() - started
                runs.append(
                    dict(
                        requests=len(tokens),
                        concurrency=concurrency,
                        seconds=round(elapsed, 4),
                        requests_per_second=round(len(tokens) / elapsed, 2),
                        p50_ms=round(statistics.median(samples), 3),
                        p95_ms=round(samples[math.ceil(len(samples) * 0.95) - 1], 3),
                    )
                )
    return runs


def scenario(
    path: Path, followers: int, per_person: int, requests: int, repeats: int, http: bool
) -> dict:
    app = create_app(
        Settings(
            database=str(path),
            public_url="https://synthetic.example.test",
            shared_varyz_enabled=True,
            archive_encryption_key=Fernet.generate_key().decode(),
        )
    )
    store, following = app.state.store, app.state.sharing.following
    with store.connection() as db:
        for n in range(per_person):
            name = f"creator{n:03}"
            db.execute(
                "INSERT INTO accounts(id,email,password,status,email_verified,username) "
                "VALUES(?,?,?,'active',1,?)",
                (name, name + "@example.test", "unused", name),
            )
    for n in range(per_person):
        name = f"creator{n:03}"
        store.push(
            name,
            [
                dict(
                    kind="rule",
                    id=f"rule{i}",
                    base_version=0,
                    payload=dict(
                        scope="*",
                        statement=f"Code review standard {i}: "
                        "Read the complete diff and explain the impact.",
                    ),
                )
                for i in range(20)
            ],
        )
        preview = following.preview(
            name,
            "synthetic",
            "code review",
            [dict(kind="rule", id=f"rule{i}", version=1) for i in range(20)],
        )
        following.publish(name, "synthetic", preview["preview_id"], identifier())
    before = size(store)
    with store.connection() as db:
        db.executemany(
            "INSERT INTO accounts(id,email,password,status,email_verified,username) "
            "VALUES(?,?,?,'inactive',1,?)",
            [
                (f"follower{i:06}", f"follower{i:06}@example.test", "unused", f"follower{i:06}")
                for i in range(followers)
            ],
        )
    accounts_bytes = size(store) - before
    before = size(store)
    with store.connection() as db:
        db.executemany(
            "INSERT INTO follows VALUES(?,?,?)",
            (
                (f"follower{i:06}", f"creator{n:03}", float(per_person - n))
                for i in range(followers)
                for n in range(per_person)
            ),
        )
        assert db.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == followers * per_person
    edges_bytes = size(store) - before

    def call(i):
        return following.brief(f"follower{(i * 7919) % followers:06}", "Review this pull request")

    following._cache.clear()
    cold = measure(call, 1, 1)
    runs = [measure(call, requests, concurrency) for concurrency in (1, 8) for _ in range(repeats)]
    baseline = [
        measure(
            lambda i: following.brief("creator000", "Review this pull request"),
            requests,
            concurrency,
        )
        for concurrency in (1, 8)
        for _ in range(repeats)
    ]
    http_runs = []
    if http:
        tokens = [
            store.credential(f"follower{(i * 7919) % followers:06}", "access", 3600)
            for i in range(requests)
        ]
        http_runs = asyncio.run(measure_http(app, tokens, repeats))
    with store.connection() as db:
        usage = db.execute("SELECT SUM(n) FROM public_uses WHERE kind='brief'").fetchone()[0]
        assert usage == 1 + requests * repeats * (4 if http else 2)
        assert db.execute("SELECT COUNT(*) FROM public_publications").fetchone()[0] == per_person
    return dict(
        followers=followers,
        creators_per_follower=per_person,
        standards_per_creator=20,
        follow_edges=followers * per_person,
        account_bytes_per_follower=round(accounts_bytes / followers, 2),
        follow_bytes_per_edge=round(edges_bytes / (followers * per_person), 2),
        database_bytes=size(store),
        cold=cold,
        runs=runs,
        no_follow_baseline=baseline,
        http_asgi_runs=http_runs,
        usage_count_verified=usage,
        errors=0,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--followers", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--follows", nargs="+", type=int, default=[1, 10, 50])
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--http",
        action="store_true",
        help="Also measure authenticated ASGI requests (no TLS or network)",
    )
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp/public-varyz-capacity.json")
    args = parser.parse_args()
    if (
        min(args.followers + args.follows + [args.requests, args.repeats]) < 1
        or max(args.follows) > 50
    ):
        parser.error("Positive counts required; at most 50 follows per person")
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    sources = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "src/varyz/cloud/following.py",
            "src/varyz/cloud/store.py",
            "src/varyz/cloud/sharing_store.py",
            "src/varyz/cloud/sharing_routes.py",
            "scripts/public_varyz_capacity.py",
        )
    }
    result = dict(
        measured_at=datetime.now(UTC).isoformat(),
        platform=platform.platform(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        scope="Local service microbenchmark with real usage writes and optional authenticated "
        "ASGI HTTP; excludes TLS/network, OAuth sign-in, production CPU/memory limits "
        "and mixed workloads",
        source_sha256=sources,
        scenarios=[],
    )
    with TemporaryDirectory(prefix="public-capacity-", dir=scratch) as temp:
        for followers in args.followers:
            for follows in args.follows:
                value = scenario(
                    Path(temp) / f"{followers}-{follows}.sqlite",
                    followers,
                    follows,
                    args.requests,
                    args.repeats,
                    args.http,
                )
                result["scenarios"].append(value)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(value), flush=True)


if __name__ == "__main__":
    main()
