"""Local LLM via Ollama — runs entirely on this machine, nothing leaves it.

Used as the distillation stage in front of the cloud model: it reads a broad
set of candidate memories and extracts only the excerpts needed to answer, so
the cloud sees a minimal context. Also usable as a fully-local answer provider
for sensitive questions.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from varyz.llm.request_deadline import iter_with_deadline, request_with_deadline


def _loopback(url: str) -> str:
    """`localhost` as the address Ollama listens on.

    On Windows a connection to `localhost` tries IPv6 first, and Ollama listens
    on IPv4 only, so every request waited two seconds for the first attempt to
    fail: measured at 2.1 s against 0.01 s, on calls whose inference took half
    a second.
    """
    scheme, sep, rest = url.partition("://")
    if sep and (rest == "localhost" or rest.startswith(("localhost:", "localhost/"))):
        return f"{scheme}://127.0.0.1{rest[len('localhost'):]}"
    return url


class OllamaProvider:
    def __init__(
        self,
        model: str,
        *,
        url: str = "http://127.0.0.1:11434",
        max_tokens: int = 1024,
        context_window: int | None = None,
        think: bool | None = None,
        format_schema: dict[str, Any] | None = None,
        request_timeout_seconds: float = 600.0,
        keep_alive: str | None = None,
    ):
        self.model = model
        self.url = _loopback(url).rstrip("/")
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.think = think
        self.format_schema = format_schema
        self.request_timeout_seconds = request_timeout_seconds
        # Ollama's own default (5 minutes) when None.
        self.keep_alive = keep_alive
        self.metrics: list[dict[str, int]] = []

    def available(self) -> bool:
        """True only if the server is up AND the configured model is pulled."""
        names = self._installed_model_names()
        if names is None:
            return False
        base = self.model.split(":")[0]
        return any(n == self.model or n.split(":")[0] == base for n in names)

    def status(self) -> str:
        names = self._installed_model_names()
        if names is None:
            return "not running"
        base = self.model.split(":")[0]
        ready = any(n == self.model or n.split(":")[0] == base for n in names)
        return "ready" if ready else f"running (pull {self.model})"

    def loaded(self) -> bool:
        """Whether a request would be answered without loading the model first."""
        return self.runtime_details() is not None

    def _installed_model_names(self) -> list[str] | None:
        """Read installed models, tolerating Ollama's brief unload-time 500s."""
        for attempt in range(3):
            try:
                resp = request_with_deadline(
                    "GET",
                    f"{self.url}/api/tags",
                    timeout_seconds=3.0,
                    server_name="Ollama server",
                    request_function=httpx.get,
                )
                if resp.status_code == 200:
                    return [
                        str(model.get("name", ""))
                        for model in resp.json().get("models", [])
                    ]
                if resp.status_code < 500:
                    return None
            except Exception:
                pass
            if attempt < 2:
                time.sleep(0.2)
        return None

    def complete(
        self, system: str, user: str, *, history: Sequence[dict[str, str]] = ()
    ) -> str:
        options: dict[str, int | float] = {
            "num_predict": self.max_tokens,
            "temperature": 0.0,
        }
        if self.context_window is not None:
            options["num_ctx"] = self.context_window
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *history,
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        if self.format_schema is not None:
            payload["format"] = self.format_schema
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        resp = request_with_deadline(
            "POST",
            f"{self.url}/api/chat",
            timeout_seconds=self.request_timeout_seconds,
            server_name="Ollama server",
            request_function=httpx.post,
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
        self.metrics.append(
            {
                key: int(result.get(key, 0))
                for key in (
                    "load_duration",
                    "prompt_eval_count",
                    "prompt_eval_duration",
                    "eval_count",
                    "eval_duration",
                )
            }
        )
        return result["message"]["content"]

    def runtime_details(self) -> dict[str, Any] | None:
        """Return Ollama's loaded-model record for this provider, when present."""
        try:
            resp = request_with_deadline(
                "GET",
                f"{self.url}/api/ps",
                timeout_seconds=3.0,
                server_name="Ollama server",
                request_function=httpx.get,
            )
            resp.raise_for_status()
        except Exception:
            return None
        base = self.model.split(":")[0]
        for model in resp.json().get("models", []):
            name = str(model.get("name", ""))
            if name == self.model or name.split(":")[0] == base:
                return model
        return None

    def stream(self, system: str, user: str) -> Iterator[str]:
        client = httpx.Client(timeout=self.request_timeout_seconds, trust_env=False)

        def request() -> Iterator[str]:
            with client.stream(
                "POST",
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": True,
                    "options": {"num_predict": self.max_tokens},
                },
            ) as resp:
                resp.raise_for_status()
                import json

                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = event.get("message", {}).get("content")
                    if piece:
                        yield piece

        try:
            yield from iter_with_deadline(
                request,
                cancel=client.close,
                timeout_seconds=self.request_timeout_seconds,
                server_name="Ollama server",
            )
        finally:
            client.close()
