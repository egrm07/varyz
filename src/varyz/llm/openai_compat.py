"""Chat-completions providers: OpenAI and DeepSeek share the same wire format.

DeepSeek is plain API-key auth against api.deepseek.com. OpenAI works with an
API key here; subscription OAuth ("Sign in with ChatGPT") lives in
llm/openai_oauth.py because it talks to a different backend.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class OpenAICompatProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | Callable[[], str],
        base_url: str | None = None,
        max_tokens: int = 2048,
        extra_headers: dict | None = None,
    ):
        self.model = model
        self._api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.extra_headers = extra_headers or {}

    def _client(self):
        from openai import OpenAI

        key = self._api_key() if callable(self._api_key) else self._api_key
        return OpenAI(
            api_key=key,
            base_url=self.base_url,
            default_headers=self.extra_headers or None,
        )

    def stream(self, system: str, user: str) -> Iterator[str]:
        response = self._client().chat.completions.create(
            model=self.model,
            max_completion_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def complete(self, system: str, user: str) -> str:
        return "".join(self.stream(system, user))
