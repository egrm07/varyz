from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from varyz.config import Config, LocalModelConfig, StorageConfig
from varyz.llm.llamacpp_local import LlamaCppProvider
from varyz.llm.local_runtime import (
    ManagedEndpoint,
    ManagedLlamaRuntime,
    ManagedServerLeaseHeldError,
    _RunningServer,
)
from varyz.llm.ollama_local import OllamaProvider
from varyz.llm.request_deadline import LocalModelRequestError


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 42_424_242
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class FakeLog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path, **settings) -> Config:
    return Config(
        storage=StorageConfig(data_dir=tmp_path / "archive"),
        local_model=LocalModelConfig(runtime_variant="cpu", **settings),
        base_dir=tmp_path,
    )


def _fake_server() -> _RunningServer:
    return _RunningServer(
        FakeProcess(),
        "http://127.0.0.1:32123",
        "secret",
        FakeLog(),
        Path("local-model.log"),
    )


def _prepare_runtime(runtime: ManagedLlamaRuntime, monkeypatch, server=None) -> None:
    monkeypatch.setattr(runtime, "available", lambda: True)
    if server is not None:
        monkeypatch.setattr(runtime, "_start_server", lambda: server)


def test_two_runtime_instances_reuse_the_machine_lease(tmp_path, monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", lambda _seconds: None)
    first = ManagedLlamaRuntime(_config(tmp_path))
    second = ManagedLlamaRuntime(_config(tmp_path))
    server = _fake_server()
    _prepare_runtime(first, monkeypatch, server)
    _prepare_runtime(second, monkeypatch)

    assert first.endpoint() == (server.base_url, server.api_key)
    # Forget only the in-process registry to exercise the file lease path.
    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(
        second,
        "_start_server",
        lambda: pytest.fail("a live machine lease must be reused"),
    )

    assert second.endpoint() == (server.base_url, server.api_key)
    local_runtime._terminate(server)


def test_a_live_incompatible_lease_is_reported_not_stolen(tmp_path, monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", lambda _seconds: None)
    first = ManagedLlamaRuntime(_config(tmp_path, model_path=tmp_path / "first.gguf"))
    second = ManagedLlamaRuntime(_config(tmp_path, model_path=tmp_path / "second.gguf"))
    server = _fake_server()
    _prepare_runtime(first, monkeypatch, server)
    _prepare_runtime(second, monkeypatch)
    first.endpoint()
    monkeypatch.setattr(
        second,
        "_start_server",
        lambda: pytest.fail("an incompatible live lease must not be stolen"),
    )

    with pytest.raises(ManagedServerLeaseHeldError, match=r"process \d+.*serves model"):
        second.endpoint()
    local_runtime._terminate(server)


def test_a_dead_lease_owner_is_reclaimed(tmp_path, monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", lambda _seconds: None)
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    server = _fake_server()
    _prepare_runtime(runtime, monkeypatch, server)
    runtime.state_path.parent.mkdir(parents=True)
    runtime.state_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "owner_pid": 2_000_000_000,
                "server_pid": 2_000_000_001,
                "base_url": "http://127.0.0.1:1",
                "api_key": "stale",
                "runtime_dir": str(runtime.runtime_dir.resolve()),
                "model_path": str(runtime.model_path.resolve()),
                "token": "stale-token",
            }
        ),
        encoding="utf-8",
    )

    assert runtime.endpoint() == (server.base_url, server.api_key)
    assert json.loads(runtime.state_path.read_text(encoding="utf-8"))["owner_pid"] == os.getpid()
    local_runtime.stop_managed_servers()


def test_last_remote_user_cleans_up_a_detached_server(tmp_path, monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", lambda _seconds: None)
    owner = ManagedLlamaRuntime(_config(tmp_path))
    user = ManagedLlamaRuntime(_config(tmp_path))
    server = _fake_server()
    _prepare_runtime(owner, monkeypatch, server)
    _prepare_runtime(user, monkeypatch)
    monkeypatch.setattr(local_runtime, "_PidProcess", lambda _pid: server.process)
    owner.endpoint()

    with user.request_endpoint():
        local_runtime.stop_managed_servers()
        assert not server.process.terminated

    assert server.process.terminated
    assert not owner.state_path.exists()


def test_idle_unload_waits_for_a_remote_usage_lease(tmp_path, monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", lambda _seconds: None)
    owner = ManagedLlamaRuntime(_config(tmp_path))
    user = ManagedLlamaRuntime(_config(tmp_path))
    server = _fake_server()
    _prepare_runtime(owner, monkeypatch, server)
    _prepare_runtime(user, monkeypatch)
    owner.endpoint()

    with user.request_endpoint():
        local_runtime._unload_if_idle(0.0)
        assert not server.process.terminated

    local_runtime._unload_if_idle(0.0)
    assert server.process.terminated


@contextmanager
def _peer(*, close_immediately: bool) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    release = threading.Event()

    def serve() -> None:
        connection, _address = listener.accept()
        if not close_immediately:
            release.wait(2.0)
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        release.set()
        listener.close()
        thread.join(timeout=2.0)


def _llamacpp_provider(url: str, timeout: float) -> LlamaCppProvider:
    runtime = SimpleNamespace(
        settings=SimpleNamespace(model="fixture.gguf", request_timeout_seconds=timeout),
        endpoint=lambda: (url, "secret"),
        available=lambda: True,
        status=lambda: SimpleNamespace(detail="ready"),
    )
    return LlamaCppProvider(runtime)


class _WedgedPeer:
    """Accept requests forever and record when each client cancels its socket."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.accepted = 0
        self.closed = 0
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="wedged-peer")
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._changed:
                self.accepted += 1
                self._changed.notify_all()
            connection.settimeout(0.05)
            try:
                while not self._stop.is_set():
                    try:
                        chunk = connection.recv(64 * 1024)
                    except TimeoutError:
                        continue
                    if not chunk:
                        with self._changed:
                            self.closed += 1
                            self._changed.notify_all()
                        break
            finally:
                connection.close()

    def wait_for_closed(self, expected: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.closed < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def close(self) -> None:
        self._stop.set()
        self.listener.close()
        self._thread.join(timeout=1.0)


@contextmanager
def _wedged_peer() -> Iterator[_WedgedPeer]:
    peer = _WedgedPeer()
    try:
        yield peer
    finally:
        peer.close()


def _deadline_workers() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("varyz-local-model-")
    ]


def _invoke_wedged(backend: str, url: str, timeout: float) -> None:
    if backend == "llamacpp":
        _llamacpp_provider(url, timeout).complete("system", "user")
    elif backend == "ollama":
        OllamaProvider("fixture", url=url, request_timeout_seconds=timeout).complete(
            "system", "user"
        )
    else:
        list(
            OllamaProvider(
                "fixture", url=url, request_timeout_seconds=timeout
            ).stream("system", "user")
        )


@pytest.mark.parametrize("backend", ["llamacpp", "ollama"])
def test_a_peer_that_closes_without_responding_is_a_named_failure(backend):
    with _peer(close_immediately=True) as url:
        provider = (
            _llamacpp_provider(url, 1.0)
            if backend == "llamacpp"
            else OllamaProvider("fixture", url=url, request_timeout_seconds=1.0)
        )
        with pytest.raises(LocalModelRequestError, match="exited|restarted"):
            provider.complete("system", "user")


@pytest.mark.parametrize("backend", ["llamacpp", "ollama"])
def test_the_wall_deadline_survives_a_stuck_transport(backend):
    with _peer(close_immediately=False) as url:
        provider = (
            _llamacpp_provider(url, 0.1)
            if backend == "llamacpp"
            else OllamaProvider("fixture", url=url, request_timeout_seconds=0.1)
        )
        started = time.monotonic()
        with pytest.raises(LocalModelRequestError, match="deadline"):
            provider.complete("system", "user")
        assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("backend", ["llamacpp", "ollama", "ollama_stream"])
def test_an_expired_request_closes_its_socket_and_joins_its_worker(backend):
    assert not _deadline_workers()
    with _wedged_peer() as peer:
        with pytest.raises(LocalModelRequestError, match="deadline"):
            _invoke_wedged(backend, peer.url, 0.05)

        assert not _deadline_workers()
        assert peer.wait_for_closed(1)
        assert peer.accepted == peer.closed == 1


@pytest.mark.parametrize("backend", ["llamacpp", "ollama"])
def test_repeated_wedged_requests_do_not_accumulate_workers_or_sockets(backend):
    request_count = 12
    with _wedged_peer() as peer:
        for completed in range(1, request_count + 1):
            with pytest.raises(LocalModelRequestError, match="deadline"):
                _invoke_wedged(backend, peer.url, 0.03)
            assert not _deadline_workers()
            assert peer.wait_for_closed(completed)

        assert peer.accepted == peer.closed == request_count


def test_managed_peer_loss_cancels_the_socket_and_joins_the_worker():
    with _wedged_peer() as peer:

        @contextmanager
        def request_endpoint() -> Iterator[ManagedEndpoint]:
            yield ManagedEndpoint(peer.url, "secret", 12345, "generation")

        runtime = SimpleNamespace(
            settings=SimpleNamespace(
                model="fixture.gguf",
                request_timeout_seconds=10.0,
            ),
            request_endpoint=request_endpoint,
            endpoint_alive=lambda _endpoint: peer.accepted == 0,
            available=lambda: True,
            status=lambda: SimpleNamespace(detail="ready"),
        )

        with pytest.raises(LocalModelRequestError, match="exited|restarted"):
            LlamaCppProvider(runtime).complete("system", "user")

        assert not _deadline_workers()
        assert peer.wait_for_closed(1)
        assert peer.accepted == peer.closed == 1


def _ready_state(runtime: ManagedLlamaRuntime, *, server_pid: int, token: str) -> None:
    runtime.state_path.parent.mkdir(parents=True, exist_ok=True)
    runtime.state_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "owner_pid": os.getpid(),
                "server_pid": server_pid,
                "base_url": "http://127.0.0.1:32123",
                "api_key": "secret",
                "runtime_dir": str(runtime.runtime_dir.resolve()),
                "model_path": str(runtime.model_path.resolve()),
                "token": token,
            }
        ),
        encoding="utf-8",
    )


def test_an_unreadable_lease_file_does_not_declare_a_running_server_dead(tmp_path, monkeypatch):
    """A bookkeeping collision must not abort an in-flight group of episodes.

    ``endpoint_alive`` is polled every 50 ms for the whole of a request, while
    ``_touch_state`` rewrites the same file on every request without being
    serialised against readers. On Windows that rename collides routinely. When
    a failed read counted as death, a compile lost 29 of 274 grouped annotation
    calls — eight episodes each — to a server that never exited.
    """
    from varyz.llm import local_runtime

    runtime = ManagedLlamaRuntime(_config(tmp_path))
    endpoint = ManagedEndpoint("http://127.0.0.1:32123", "secret", os.getpid(), "live-token")
    _ready_state(runtime, server_pid=os.getpid(), token="live-token")
    assert runtime.endpoint_alive(endpoint)

    monkeypatch.setattr(local_runtime, "_read_lease_state", lambda _path: None)
    assert runtime.endpoint_alive(endpoint), "a transient read failure aborted a live request"


def test_a_lease_taken_over_by_another_server_is_still_reported_dead(tmp_path):
    """The fix must not blind the check: a readable state that names a different
    server still ends the request, and so does a pid that is genuinely gone."""
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    endpoint = ManagedEndpoint("http://127.0.0.1:32123", "secret", os.getpid(), "live-token")

    _ready_state(runtime, server_pid=os.getpid(), token="someone-elses-token")
    assert not runtime.endpoint_alive(endpoint)

    _ready_state(runtime, server_pid=os.getpid() + 1, token="live-token")
    assert not runtime.endpoint_alive(endpoint)

    dead = ManagedEndpoint("http://127.0.0.1:32123", "secret", 2_000_000_001, "live-token")
    _ready_state(runtime, server_pid=2_000_000_001, token="live-token")
    assert not runtime.endpoint_alive(dead)


def test_a_lost_server_says_so_in_the_log() -> None:
    """The message of a lost-server error is the operator's only diagnosis.

    Reduced to a type name, two compiles on 2026-09-06 logged
    ``LocalModelRequestError: LocalModelRequestError`` and nothing else while
    the server was dying of a CUDA out-of-memory under six slots.  These
    messages are composed in :mod:`varyz.llm.request_deadline` from a server
    name, a deadline and a transport error, so none of them can quote a reply.
    """

    from varyz.llm.refusal import refusal_reason

    exc = LocalModelRequestError(
        "managed llama.cpp server pid 939128 exited or was restarted "
        "while inference was in flight"
    )
    assert refusal_reason(exc) == str(exc)


def test_a_validation_error_still_says_only_which_check_failed() -> None:
    """The privacy rule that silences pydantic is untouched by the above."""

    from pydantic import BaseModel, ValidationError

    from varyz.llm.refusal import refusal_reason

    class Reply(BaseModel):
        count: int

    with pytest.raises(ValidationError) as caught:
        Reply(count="a sentence somebody wrote to somebody else")  # type: ignore[arg-type]
    reason = refusal_reason(caught.value)
    assert "somebody" not in reason
    assert reason == "int_parsing"


def test_a_bounds_failure_says_which_bound_and_by_how_much() -> None:
    """``too_long`` on its own does not say which array ran over.

    Concept 12 of the compile of 2026-09-07 was refused with that word and
    nothing else, and finding the field meant re-running it.  The limit and
    the length that arrived are numbers pydantic already carries, and numbers
    cannot quote the archive.
    """

    from pydantic import BaseModel, Field, ValidationError

    from varyz.llm.refusal import refusal_reason

    class Reply(BaseModel):
        alternatives: list[str] = Field(max_length=2)

    with pytest.raises(ValidationError) as caught:
        Reply(alternatives=["a note", "somebody wrote", "to somebody else"])
    reason = refusal_reason(caught.value)
    assert "somebody" not in reason
    assert reason.startswith("too_long (")
    assert "max_length=2" in reason
    assert "actual_length=3" in reason


def test_a_bounds_failure_repeats_no_text_from_the_reply() -> None:
    """Only numbers leave ``ctx``; a string in there could be anything."""

    from typing import Literal

    from pydantic import BaseModel, ValidationError

    from varyz.llm.refusal import refusal_reason

    class Reply(BaseModel):
        kind: Literal["fact", "preference"]

    with pytest.raises(ValidationError) as caught:
        Reply(kind="a sentence somebody wrote")  # type: ignore[arg-type]
    reason = refusal_reason(caught.value)
    assert "somebody" not in reason
    assert "fact" not in reason
    assert reason == "literal_error"
