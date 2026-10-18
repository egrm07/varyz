"""Hard wall-clock deadlines for local inference transports."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator

import httpx

_ORIGINAL_HTTPX_GET = httpx.get
_ORIGINAL_HTTPX_POST = httpx.post


class LocalModelRequestError(RuntimeError):
    """A local inference request lost its server or exceeded its deadline."""


def _transport_error(server_name: str, exc: BaseException) -> LocalModelRequestError:
    return LocalModelRequestError(
        f"{server_name} connection closed or failed while inference was in flight; "
        f"the server may have exited or been restarted ({exc})"
    )


def _raise_result[T](
    value: T | BaseException,
    server_name: str,
    timeout_seconds: float,
) -> T:
    if not isinstance(value, BaseException):
        return value
    if isinstance(value, httpx.TimeoutException):
        raise LocalModelRequestError(
            f"{server_name} did not respond within the {timeout_seconds:g}-second "
            "request deadline"
        ) from value
    if isinstance(value, (httpx.TransportError, OSError)):
        raise _transport_error(server_name, value) from value
    raise value


def _cancel_and_join(worker: threading.Thread, cancel: Callable[[], None]) -> None:
    try:
        cancel()
    finally:
        worker.join()


def run_with_deadline[T](
    operation: Callable[[], T],
    *,
    cancel: Callable[[], None],
    timeout_seconds: float,
    server_name: str,
    server_alive: Callable[[], bool] | None = None,
) -> T:
    """Run a blocking call, cancelling its transport and joining before failure."""
    results: queue.SimpleQueue[T | BaseException] = queue.SimpleQueue()

    def run() -> None:
        try:
            results.put(operation())
        except BaseException as exc:
            results.put(exc)

    worker = threading.Thread(target=run, name="varyz-local-model-request")
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _cancel_and_join(worker, cancel)
            raise LocalModelRequestError(
                f"{server_name} did not respond within the {timeout_seconds:g}-second "
                "request deadline"
            )
        try:
            result = results.get(timeout=min(0.05, remaining))
            worker.join()
            return _raise_result(
                result,
                server_name,
                timeout_seconds,
            )
        except queue.Empty:
            if server_alive is not None and not server_alive():
                _cancel_and_join(worker, cancel)
                raise LocalModelRequestError(
                    f"{server_name} exited or was restarted while inference was in flight"
                ) from None


def request_with_deadline(
    method: str,
    url: str,
    *,
    timeout_seconds: float,
    server_name: str,
    server_alive: Callable[[], bool] | None = None,
    request_function: Callable[..., httpx.Response] | None = None,
    **kwargs: object,
) -> httpx.Response:
    """Make one HTTP request whose active socket can be closed on expiry."""
    normalized = method.upper()
    convenience = request_function or (httpx.get if normalized == "GET" else httpx.post)
    original = _ORIGINAL_HTTPX_GET if normalized == "GET" else _ORIGINAL_HTTPX_POST
    if convenience is not original:
        # Tests and embedders may replace HTTPX's convenience function with an
        # in-memory response. There is no transport to own in that case.
        return run_with_deadline(
            lambda: convenience(url, timeout=timeout_seconds, **kwargs),
            cancel=lambda: None,
            timeout_seconds=timeout_seconds,
            server_name=server_name,
            server_alive=server_alive,
        )
    client = httpx.Client(timeout=timeout_seconds, trust_env=False)
    try:
        return run_with_deadline(
            lambda: client.request(normalized, url, **kwargs),
            cancel=client.close,
            timeout_seconds=timeout_seconds,
            server_name=server_name,
            server_alive=server_alive,
        )
    finally:
        client.close()


def iter_with_deadline(
    operation: Callable[[], Iterator[str]],
    *,
    cancel: Callable[[], None],
    timeout_seconds: float,
    server_name: str,
    server_alive: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Apply the same absolute deadline while a streaming response is consumed."""
    events: queue.Queue[str | BaseException | None] = queue.Queue()

    def run() -> None:
        try:
            for piece in operation():
                events.put(piece)
        except BaseException as exc:
            events.put(exc)
        finally:
            events.put(None)

    worker = threading.Thread(target=run, name="varyz-local-model-stream")
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _cancel_and_join(worker, cancel)
                raise LocalModelRequestError(
                    f"{server_name} did not finish within the {timeout_seconds:g}-second "
                    "request deadline"
                )
            try:
                event = events.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if server_alive is not None and not server_alive():
                    _cancel_and_join(worker, cancel)
                    raise LocalModelRequestError(
                        f"{server_name} exited or was restarted while inference was in flight"
                    ) from None
                continue
            if event is None:
                worker.join()
                return
            yield _raise_result(event, server_name, timeout_seconds)
    finally:
        if worker.is_alive():
            _cancel_and_join(worker, cancel)
