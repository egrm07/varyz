"""Bound expensive work and share queue turns between clients in one ASGI worker."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import Counter, OrderedDict, deque
from http.cookies import CookieError, SimpleCookie

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class Busy(Exception):
    """Rejected before application work starts; safe for a client to retry."""


class Lease:
    def __init__(self, gate: Admission, key: str, peer: str) -> None:
        self.gate, self.key, self.peer = gate, key, peer
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.active = False
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        gate = self.gate
        if self.active:
            gate.active -= 1
            gate.active_keys[self.key] -= 1
            if not gate.active_keys[self.key]:
                del gate.active_keys[self.key]
        else:
            queue = gate.waiting[self.key]
            queue.remove(self)
            gate.queued -= 1
            if not queue:
                del gate.waiting[self.key]
        for counts, key in ((gate.clients, self.key), (gate.peers, self.peer)):
            counts[key] -= 1
            if not counts[key]:
                del counts[key]
        gate.dispatch()


class Admission:
    def __init__(
        self,
        *,
        workers: int = 8,
        queue: int = 64,
        per_client: int = 4,
        per_peer: int = 16,
        client_workers: int = 2,
        timeout: float = 10,
    ) -> None:
        if min(workers, queue, per_client, per_peer, client_workers, timeout) <= 0:
            raise ValueError("Admission limits must be positive")
        self.workers, self.queue_limit = workers, queue
        self.per_client, self.per_peer = per_client, per_peer
        self.client_workers, self.timeout = client_workers, timeout
        self.active = self.queued = 0
        self.clients: Counter[str] = Counter()
        self.peers: Counter[str] = Counter()
        self.active_keys: Counter[str] = Counter()
        self.waiting: OrderedDict[str, deque[Lease]] = OrderedDict()
        self.high_active = self.high_queued = self.accepted = self.rejected = 0

    def dispatch(self) -> None:
        while self.active < self.workers and self.waiting:
            key = next((k for k in self.waiting if self.active_keys[k] < self.client_workers), None)
            if key is None:
                break
            queue = self.waiting[key]
            lease = queue.popleft()
            if queue:
                self.waiting.move_to_end(key)
            else:
                del self.waiting[key]
            self.queued -= 1
            self.active += 1
            self.active_keys[key] += 1
            self.high_active = max(self.high_active, self.active)
            lease.active = True
            lease.ready.set_result(None)

    async def acquire(self, key: str, peer: str) -> Lease:
        if (
            self.clients[key] >= self.per_client
            or self.peers[peer] >= self.per_peer
            or self.queued >= self.queue_limit
        ):
            self.rejected += 1
            raise Busy
        lease = Lease(self, key, peer)
        self.clients[key] += 1
        self.peers[peer] += 1
        self.waiting.setdefault(key, deque()).append(lease)
        self.queued += 1
        self.dispatch()
        self.high_queued = max(self.high_queued, self.queued)
        try:
            await asyncio.wait_for(asyncio.shield(lease.ready), self.timeout)
        except (TimeoutError, asyncio.CancelledError):
            lease.release()
            if not lease.ready.done():
                lease.ready.cancel()
            self.rejected += 1
            raise
        self.accepted += 1
        return lease

    def status(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in ("active", "queued", "high_active", "high_queued", "accepted", "rejected")
        }


class Backpressure:
    def __init__(self, app: ASGIApp, gate: Admission) -> None:
        self.app, self.gate = app, gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        peer = (scope.get("client") or ("unknown", 0))[0]
        headers = dict(scope["headers"])
        identity = headers.get(b"authorization", b"")
        if not identity:
            try:
                cookie: SimpleCookie = SimpleCookie()
                cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
                session = cookie.get("__Host-session") or cookie.get("session")
                identity = session.value.encode() if session else peer.encode()
            except CookieError:
                identity = peer.encode()
        # Credential identity controls scheduling only; authentication still runs
        # in the application. A peer bound prevents rotated fake tokens filling it.
        key = hashlib.sha256(identity).hexdigest()
        began = time.monotonic()
        try:
            lease = await self.gate.acquire(key, peer)
        except (Busy, TimeoutError):
            response = JSONResponse(
                {"detail": "Cloud is busy; retry shortly. No work was started."},
                status_code=503,
                headers={
                    "Retry-After": "2",
                    "X-Varyz-Retry-Safe": "1",
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        scope["varyz_admission"] = lease
        waited = str(math.ceil((time.monotonic() - began) * 1000))

        async def timed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-varyz-queue-ms", waited.encode())
                ]
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            lease.release()
