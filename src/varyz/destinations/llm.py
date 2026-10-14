"""Cloud/local model destination adapter; provider details stay behind it."""

from __future__ import annotations

import json

from varyz.privacy.models import PreparedEgress


class LLMDestinationAdapter:
    destination_type = "cloud_model"
    version = "1"
    supports_idempotency = True
    produces_external_effect = False
    reversible_default = False
    timeout_seconds = 120.0
    max_retries = 0

    def __init__(self, destination: str, provider, *, stream: bool = True):
        self.destination = destination
        self.provider = provider
        self.stream = stream

    def execute(self, prepared: PreparedEgress, *, idempotency_key: str):
        payload = json.loads(prepared.payload)
        system = str(payload.get("system", ""))
        user = str(payload.get("user", ""))
        if self.stream:
            return self.provider.stream(system, user)
        return self.provider.complete(system, user)

    @staticmethod
    def classify_error(error: Exception) -> str:
        return "destination_timeout" if isinstance(error, TimeoutError) else "model_error"
