"""The single choke point for every LLM call. Nothing marked sensitive (or
still unclassified — fail-closed) may ever reach a cloud provider; callers get
back what was allowed plus an explicit count of what was excluded, so gaps
are visible instead of silent."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from varyz.config import Config
from varyz.domain import SearchHit
from varyz.llm.providers import NoCredentialsError, build_provider

# Back-compat alias: callers catch this around provider construction.
NoApiKeyError = NoCredentialsError


def partition_for_cloud(hits: Sequence[SearchHit]) -> tuple[list[SearchHit], int]:
    """Split hits into (cloud-allowed, excluded_count). The audit boundary:
    tests assert nothing sensitive survives this call."""
    allowed = [h for h in hits if h.entry.sensitivity.cloud_ok]
    return allowed, len(hits) - len(allowed)


class LLMRouter:
    def __init__(self, cfg: Config, provider=None):
        self.cfg = cfg
        self._provider = provider  # injectable for tests (FakeLLM)

    def chat_provider(self):
        if self._provider is not None:
            return self._provider
        return build_provider(self.cfg, role="chat")

    def enrich_provider(self):
        if self._provider is not None:
            return self._provider
        return build_provider(self.cfg, role="enrich", max_tokens=1500)

    def stream_with_context(
        self, system: str, user: str, hits: Sequence[SearchHit]
    ) -> Iterator[str]:
        """Callers must build `user` ONLY from hits returned by
        partition_for_cloud — this re-checks as a last line of defense."""
        for hit in hits:
            if not hit.entry.sensitivity.cloud_ok:
                raise PermissionError(
                    f"entry {hit.entry.id} is {hit.entry.sensitivity} and cannot go to cloud"
                )
        return self.chat_provider().stream(system, user)
