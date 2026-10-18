"""The managed model's installer: what it downloads, and the download itself.

Held back from the alpha (docs/SHIPPING.md): the build leaves this module out,
so an alpha install never offers `varyz local-model install` and never fetches
the managed model (gemma, 7.4 GB) or the llama.cpp runtime that serves it.
Without them the semantic graph is not built, and the `recall` judge reads with
the `[workflows]` classifier model through Ollama instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from varyz.config import Config
from varyz.llm.local_runtime import LocalRuntimeStatus, ManagedLlamaRuntime, Progress


@dataclass(frozen=True)
class Download:
    label: str
    source: str
    size_bytes: int
    sha256: str | None
    destination: Path


def managed_downloads(cfg: Config) -> tuple[Download, ...]:
    """Exact artifacts the existing managed-runtime installer would fetch."""

    runtime = ManagedLlamaRuntime(cfg)
    downloads = [
        Download(
            "llama.cpp runtime",
            runtime.asset.url,
            runtime.asset.size,
            runtime.asset.sha256,
            runtime.download_dir / runtime.asset.name,
        )
    ]
    if runtime.companion is not None:
        downloads.append(
            Download(
                "llama.cpp companion runtime",
                runtime.companion.url,
                runtime.companion.size,
                runtime.companion.sha256,
                runtime.download_dir / runtime.companion.name,
            )
        )
    model_name = Path(urlparse(cfg.local_model.model_url).path).name or cfg.local_model.model
    downloads.append(
        Download(
            f"managed model {cfg.local_model.model}",
            cfg.local_model.model_url,
            cfg.local_model.model_size_bytes,
            cfg.local_model.model_sha256,
            runtime.model_path.parent / model_name,
        )
    )
    return tuple(downloads)


def install(
    runtime: ManagedLlamaRuntime, *, on_progress: Progress | None = None
) -> LocalRuntimeStatus:
    """Download, verify and unpack the runtime and the model.

    The one caller of `ManagedLlamaRuntime.install`: kept here so that leaving
    this module out of a build leaves nothing in it that downloads the model.
    """

    return runtime.install(on_progress=on_progress)
