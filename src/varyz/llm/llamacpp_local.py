"""Local completion provider backed by Varyz-managed llama.cpp."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from typing import Any

import httpx

from varyz.llm.grammar import gbnf_from_schema
from varyz.llm.local_runtime import ManagedEndpoint, ManagedLlamaRuntime
from varyz.llm.refusal import ReplyTruncated
from varyz.llm.request_deadline import request_with_deadline


def _raise_for_status_with_detail(response: httpx.Response) -> None:
    """Raise on an error status, keeping the server's explanation in the message.

    llama.cpp answers a refused request with a JSON body naming the reason —
    which token limit was exceeded, by how much. ``raise_for_status`` discards
    it, leaving only "400 Bad Request" in every log and quarantine record.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            body = response.json()
            detail = str(body.get("error", {}).get("message") or body)
        except (ValueError, AttributeError):
            detail = response.text
        if not detail:
            raise
        raise httpx.HTTPStatusError(
            f"{exc.args[0]}\nserver said: {detail[:500]}",
            request=exc.request,
            response=exc.response,
        ) from exc


class LlamaCppProvider:
    def __init__(
        self,
        runtime: ManagedLlamaRuntime,
        *,
        max_tokens: int = 1024,
        format_schema: dict[str, Any] | None = None,
    ):
        self.runtime = runtime
        self.model = runtime.settings.model
        self.max_tokens = max_tokens
        self.format_schema = format_schema
        # llama.cpp constrains a reply with a grammar either way; the schema is
        # only a way of asking it to write one. Written here it comes without
        # the whitespace rules, which were 17.8% of a measured reply and which
        # nothing reads. A schema this cannot express is left to the server.
        self.grammar = (
            gbnf_from_schema(format_schema) if format_schema is not None else None
        )
        self.request_timeout_seconds = runtime.settings.request_timeout_seconds
        self.metrics: list[dict[str, int]] = []

    def available(self) -> bool:
        return self.runtime.available()

    def status(self) -> str:
        return self.runtime.status().detail

    def loaded(self) -> bool:
        """Whether a request would be answered without loading the model first."""
        return self.runtime.loaded()

    def complete(
        self, system: str, user: str, *, history: Sequence[dict[str, str]] = ()
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *history,
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.grammar is not None:
            payload["grammar"] = self.grammar
        elif self.format_schema is not None:
            # The schema must sit under "json_schema", as the OpenAI-compatible
            # endpoint defines it. Placed at the top level it is not rejected,
            # merely ignored: the server answers exactly as it would with no
            # schema at all, so a pinned call silently becomes a free-form one
            # and the parse fails afterwards on the model rather than here.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "varyz_response",
                    "strict": True,
                    "schema": self.format_schema,
                },
            }
        request_endpoint = getattr(self.runtime, "request_endpoint", None)
        if request_endpoint is None:
            base_url, api_key = self.runtime.endpoint()
            scope = nullcontext(ManagedEndpoint(base_url, api_key, 0, "legacy"))
        else:
            scope = request_endpoint()
        with scope as endpoint:
            alive = (
                (lambda: self.runtime.endpoint_alive(endpoint))
                if endpoint.server_pid > 0
                else None
            )

            response = request_with_deadline(
                "POST",
                f"{endpoint.base_url}/v1/chat/completions",
                timeout_seconds=self.request_timeout_seconds,
                server_name=f"managed llama.cpp server pid {endpoint.server_pid}",
                server_alive=alive,
                request_function=httpx.post,
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json=payload,
            )
            _raise_for_status_with_detail(response)
            result = response.json()
        usage = result.get("usage", {})
        produced = int(usage.get("completion_tokens", 0))
        consumed = int(usage.get("prompt_tokens", 0))
        self.metrics.append(
            {
                "prompt_eval_count": consumed,
                "eval_count": produced,
            }
        )
        # A structured reply cut short is invalid JSON, and said so: every
        # caller reported it as a model that cannot follow a schema, when what
        # happened is that it ran out of room.  The server has always
        # distinguished the two, and the prompt size says which room ran out --
        # the cap this request asked for, or the slot the prompt is sharing.
        if result["choices"][0].get("finish_reason") == "length":
            raise ReplyTruncated(
                produced=produced, cap=self.max_tokens, prompt=consumed
            )
        return str(result["choices"][0]["message"]["content"])

    def runtime_details(self) -> dict[str, Any] | None:
        status = self.runtime.status()
        if not status.runtime_installed:
            return None
        return {
            "backend": status.backend,
            "runtime_path": str(status.runtime_path),
            "model_path": str(status.model_path),
            "model_installed": status.model_installed,
        }

    def stream(self, system: str, user: str) -> Iterator[str]:
        yield self.complete(system, user)
