"""Factory for the configured local inference backend."""

from __future__ import annotations

from typing import Any

from varyz.config import Config


def build_local_provider(
    cfg: Config,
    *,
    max_tokens: int = 1024,
    context_window: int | None = None,
    think: bool | None = None,
    format_schema: dict[str, Any] | None = None,
    conservative_resources: bool = False,
):
    if cfg.local_model.backend == "ollama":
        from varyz.llm.ollama_local import OllamaProvider

        return OllamaProvider(
            cfg.ollama.model,
            url=cfg.ollama.url,
            max_tokens=max_tokens,
            context_window=context_window or cfg.local_model.context_window,
            think=think,
            format_schema=format_schema,
            request_timeout_seconds=cfg.local_model.request_timeout_seconds,
        )

    from varyz.llm.llamacpp_local import LlamaCppProvider
    from varyz.llm.local_runtime import ManagedLlamaRuntime

    runtime_cfg = cfg
    if conservative_resources:
        # Interactive distillation needs neither the compiler's concurrency nor
        # its long synthesis window. Keep archive-level compile tuning intact,
        # but do not let one question recreate a high-memory serving shape.
        runtime_cfg = cfg.model_copy(deep=True)
        runtime_cfg.local_model = cfg.local_model.model_copy(
            update={
                "context_window": min(cfg.local_model.context_window, 8192),
                "parallel_slots": 1,
            }
        )

    configured = runtime_cfg.local_model.context_window
    if context_window is not None and context_window > configured:
        # One managed server serves every caller in the process, so its window is
        # fixed when it starts and cannot be widened per request. Accepting the
        # argument and dropping it lets a caller believe it has room it does not
        # have, and the refusal then arrives from the server as a bare 400.
        raise ValueError(
            f"managed backend runs one shared server at a {configured} token "
            f"context window; a caller asking for {context_window} must raise "
            "local_model.context_window in the archive configuration"
        )

    return LlamaCppProvider(
        ManagedLlamaRuntime(runtime_cfg),
        max_tokens=max_tokens,
        format_schema=format_schema,
    )
