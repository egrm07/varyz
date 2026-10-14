"""Isolated real HTTP spike acceptance; synthetic database only, no live credentials."""

import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import httpx
import uvicorn
from fastapi import Request

from varyz.cloud.app import Settings, create_app


async def main():
    app = create_app(Settings(database="/probe/probe.sqlite3", public_url="http://127.0.0.1:8081"))
    gate = getattr(app.state, "admission", None)
    store = app.state.store
    with store.connection() as db:
        db.executemany(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES(?,?,?,'active',1)",
            [(f"probe{i}", f"probe{i}@example.test", "unused") for i in range(10000)],
        )
    tokens = [store.credential(f"probe{i}", "access", 3600, "varyz-local") for i in range(128)]

    # A deterministic slow handler makes queue/cancellation pressure reproducible;
    # real guarded/authenticated sync endpoints are tested separately below.
    @app.get("/probe-work")
    async def work():
        await asyncio.sleep(0.2)
        return {"ok": True}

    @app.get("/probe-status")
    async def status(request: Request):
        return gate.status() if gate else {}

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8081,
            workers=1,
            limit_concurrency=128 if gate else 32,
            backlog=128 if gate else 64,
            timeout_keep_alive=2 if gate else 5,
            access_log=False,
            log_level="error",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    result = {
        "candidate": bool(gate),
        "max_accounts": app.state.settings.max_accounts,
        "http": [],
        "health_ms": [],
    }
    monitoring = True

    async def health():
        async with httpx.AsyncClient(timeout=3, limits=httpx.Limits(max_connections=1)) as client:
            while monitoring:
                began = time.monotonic()
                response = await client.get("http://127.0.0.1:8081/health")
                result.setdefault("health_codes", []).append(response.status_code)
                if gate:
                    assert response.status_code == 200
                result["health_ms"].append(round((time.monotonic() - began) * 1000, 2))
                await asyncio.sleep(0.05)

    health_task = asyncio.create_task(health())

    def headers(i):
        return {
            "Authorization": "Bearer " + tokens[i % 128],
            "X-Forwarded-For": f"198.18.{i // 250}.{i % 250 + 1}",
        }

    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8081",
            timeout=20,
            limits=httpx.Limits(max_connections=110, max_keepalive_connections=0),
        ) as client:
            for count in (64, 96, 64):
                responses = await asyncio.gather(
                    *[client.get("/probe-work", headers=headers(i)) for i in range(count)]
                )
                codes = dict(Counter(r.status_code for r in responses))
                delays = [int(r.headers.get("x-varyz-queue-ms", 0)) for r in responses]
                result["http"].append({"burst": count, "codes": codes, "max_queue_ms": max(delays)})
                if gate:
                    assert all(
                        r.status_code == 200
                        or (
                            r.status_code == 503
                            and r.headers.get("x-varyz-retry-safe") == "1"
                            and int(r.headers["retry-after"]) > 0
                        )
                        for r in responses
                    )
                    if count == 64:
                        assert codes == {200: 64} and max(delays) >= 200
                await asyncio.sleep(0.1)
            # A noisy client is bounded while another client still makes progress.
            responses = await asyncio.gather(
                *[client.get("/probe-work", headers=headers(100)) for _ in range(16)],
                client.get("/probe-work", headers=headers(101)),
            )
            result["noisy_client"] = dict(Counter(r.status_code for r in responses[:-1]))
            assert responses[-1].status_code == 200
            if gate:
                assert sum(r.status_code == 200 for r in responses[:-1]) == 4
            # Real authenticated concurrent writes, unique records, then read them back.
            successes = []
            for round_number in range(3):
                responses = await asyncio.gather(
                    *[
                        client.post(
                            "/api/sync",
                            headers=headers(i),
                            json={
                                "records": [
                                    {
                                        "kind": "rule",
                                        "id": f"round{round_number}",
                                        "base_version": 0,
                                        "payload": {
                                            "statement": "Synthetic spike test",
                                            "scope": "*",
                                        },
                                    }
                                ]
                            },
                        )
                        for i in range(64)
                    ]
                )
                successes += [
                    (i, round_number) for i, r in enumerate(responses) if r.status_code == 200
                ]
                if gate:
                    assert all(r.status_code == 200 for r in responses), [
                        (r.status_code, r.text[:100]) for r in responses if r.status_code != 200
                    ]
                await asyncio.sleep(0.1)
            for i in range(64):
                records = store.pull(f"probe{i}", 0, 100)["records"]
                assert len(records) == sum(owner == i for owner, _ in successes)
            result["verified_sync_writes"] = len(successes)
            if gate:
                # Eight idle long polls cannot consume the active-work budget.
                held = [
                    asyncio.create_task(
                        client.get("/api/sync", headers=headers(i), params={"wait": 2})
                    )
                    for i in range(110, 118)
                ]
                await asyncio.sleep(0.5)
                result["idle_polls_gate"] = gate.status()
                assert gate.active == gate.queued == 0
                response = await client.get("/api/sync", headers=headers(120))
                assert response.status_code == 200
                assert all(r.status_code == 200 for r in await asyncio.gather(*held))
                assert gate.high_active <= 8 and gate.high_queued <= 64
                assert gate.active == gate.queued == 0
                result["gate"] = gate.status()
        with store.connection() as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 10000
        result["database_integrity"] = "ok"
        result["resources"] = {
            n: (Path("/sys/fs/cgroup") / n).read_text()
            for n in ("memory.peak", "memory.events", "cpu.stat")
        }
        assert "oom_kill 0" in result["resources"]["memory.events"]
    finally:
        monitoring = False
        await health_task
        server.should_exit = True
        await serving
    Path("/probe/result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
