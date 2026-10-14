"""Admission must bound work, share turns and recover without leaked slots."""

import asyncio
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from varyz.cloud.admission import Admission, Backpressure, Busy
from varyz.cloud.app import Settings, create_app
from varyz.cloud.client import CloudClient, CloudError


async def queued(gate, number):
    for _ in range(100):
        if gate.queued == number:
            return
        await asyncio.sleep(0)
    raise AssertionError(gate.status())


def test_round_robin_prevents_a_busy_client_owning_every_turn():
    async def run():
        gate = Admission(workers=1, client_workers=1)
        first = await gate.acquire("a", "a")
        a1 = asyncio.create_task(gate.acquire("a", "a"))
        a2 = asyncio.create_task(gate.acquire("a", "a"))
        b = asyncio.create_task(gate.acquire("b", "b"))
        await queued(gate, 3)
        first.release()
        (await a1).release()
        # Client b gets the next turn before a's second queued request.
        (await b).release()
        (await a2).release()
        assert gate.active == gate.queued == 0
        assert not gate.clients and not gate.peers and not gate.active_keys

    asyncio.run(asyncio.wait_for(run(), 2))


def test_global_client_and_peer_bounds():
    async def run():
        gate = Admission(workers=1, queue=2, per_client=2, per_peer=2)
        first = await gate.acquire("a", "peer")
        pending = asyncio.create_task(gate.acquire("a", "peer"))
        await queued(gate, 1)
        with pytest.raises(Busy):
            await gate.acquire("a", "other")
        with pytest.raises(Busy):
            await gate.acquire("rotated-token", "peer")
        other = asyncio.create_task(gate.acquire("b", "other"))
        await queued(gate, 2)
        with pytest.raises(Busy):
            await gate.acquire("c", "third")
        first.release()
        (await pending).release()
        (await other).release()
        assert gate.high_active == 1 and gate.high_queued == 2
        assert gate.rejected == 3
        assert gate.active == gate.queued == 0

    asyncio.run(run())


@pytest.mark.parametrize("after_grant", [False, True])
def test_cancellation_around_grant_releases_every_counter(after_grant):
    async def run():
        gate = Admission(workers=1)
        first = await gate.acquire("a", "peer")
        waiting = asyncio.create_task(gate.acquire("b", "peer"))
        await queued(gate, 1)
        if after_grant:
            first.release()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        first.release()
        assert gate.active == gate.queued == 0
        assert not gate.clients and not gate.peers and not gate.waiting
        (await gate.acquire("c", "peer")).release()

    asyncio.run(run())


def test_timeout_removes_queued_work():
    async def run():
        gate = Admission(workers=1, timeout=0.01)
        first = await gate.acquire("a", "peer")
        with pytest.raises(TimeoutError):
            await gate.acquire("b", "peer")
        first.release()
        assert gate.active == gate.queued == 0 and not gate.clients

    asyncio.run(run())


def test_http_queue_health_rejection_and_exception_cleanup():
    async def run():
        began, finish = asyncio.Event(), asyncio.Event()
        visited = []

        async def app(scope, receive, send):
            visited.append(scope["path"])
            if scope["path"] == "/slow":
                began.set()
                await finish.wait()
            if scope["path"] == "/fail":
                raise ValueError("handler failed")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        gate = Admission(workers=1, queue=1)
        transport = httpx.ASGITransport(app=Backpressure(app, gate))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            slow = asyncio.create_task(client.get("/slow"))
            await began.wait()
            waiting = asyncio.create_task(client.get("/queued"))
            await queued(gate, 1)
            assert (await client.get("/health")).status_code == 200
            rejected = await client.post("/must-not-run", json={"write": 1})
            assert rejected.status_code == 503
            assert rejected.headers["x-varyz-retry-safe"] == "1"
            assert int(rejected.headers["retry-after"]) > 0
            assert "/must-not-run" not in visited and "/queued" not in visited
            finish.set()
            assert (await slow).status_code == (await waiting).status_code == 200
            with pytest.raises(ValueError):
                await client.get("/fail")
            assert gate.active == gate.queued == 0

    asyncio.run(run())


def test_account_cap_default_and_environment(monkeypatch):
    monkeypatch.delenv("VARYZ_CLOUD_MAX_ACCOUNTS", raising=False)
    assert Settings().max_accounts == Settings.from_env().max_accounts == 10000
    monkeypatch.setenv("VARYZ_CLOUD_MAX_ACCOUNTS", "31")
    assert Settings.from_env().max_accounts == 31


def test_health_does_not_depend_on_saturated_rate_store(tmp_path, monkeypatch):
    app = create_app(Settings(database=str(tmp_path / "health.sqlite3")))

    def broken(*args):
        raise sqlite3.OperationalError("database busy")

    monkeypatch.setattr(app.state.store, "allow_request", broken)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        assert client.get("/health").status_code == 200
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 503
        assert response.headers.get("x-varyz-retry-safe") is None


@pytest.mark.parametrize("safe", [False, True])
def test_client_only_replays_safe_overload_writes(monkeypatch, safe):
    sleeps, requests = [], []
    monkeypatch.setattr("varyz.cloud.client.time.sleep", sleeps.append)
    monkeypatch.setattr("varyz.cloud.client.secrets.randbelow", lambda _: 250)

    def reply(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                503,
                headers={
                    "Retry-After": "2",
                    "X-Varyz-Retry-Safe": "1" if safe else "0",
                },
            )
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = CloudClient("https://cloud.example", {"access_token": "test"}, http=http)
        if safe:
            assert client.request("POST", "/api/sync", json={"write": 1}) == {"ok": True}
        else:
            with pytest.raises(CloudError):
                client.request("POST", "/api/sync", json={"write": 1})
        assert len(requests) == (2 if safe else 1)
        assert sleeps == ([2.25] if safe else [])


def test_client_retry_limit_is_finite(monkeypatch):
    sleeps, requests = [], []
    monkeypatch.setattr("varyz.cloud.client.time.sleep", sleeps.append)
    monkeypatch.setattr("varyz.cloud.client.secrets.randbelow", lambda _: 0)

    def reply(request):
        requests.append(request)
        return httpx.Response(503, headers={"X-Varyz-Retry-Safe": "1", "Retry-After": "9999"})

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = CloudClient("https://cloud.example", {"access_token": "test"}, http=http)
        with pytest.raises(CloudError):
            client.request("POST", "/api/sync", json={"write": 1})
    assert sleeps == [60, 60, 60] and len(requests) == 4


@pytest.mark.parametrize("overload", [429, 503])
def test_client_renews_authorization_after_overload_backoff(monkeypatch, overload):
    monkeypatch.setattr("varyz.cloud.client.time.sleep", lambda _: None)
    calls = []

    def reply(request):
        calls.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "renewed", "refresh_token": "next"})
        if len(calls) == 1:
            return httpx.Response(overload, headers={"X-Varyz-Retry-Safe": "1"})
        if request.headers["Authorization"] == "Bearer expired":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        client = CloudClient(
            "https://cloud.example",
            {"access_token": "expired", "refresh_token": "refresh"},
            http=http,
        )
        assert client.request("POST", "/api/sync", json={"write": 1}) == {"ok": True}
    assert [request.url.path for request in calls] == [
        "/api/sync", "/api/sync", "/oauth/token", "/api/sync",
    ]
