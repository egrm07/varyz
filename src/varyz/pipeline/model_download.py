"""The local search models, fetched so that a download can stop and carry on.

fastembed downloads its models through huggingface_hub, into a temporary file
it deletes on any failure: stopping 900 MB into the 1 GB answer-checking model
began it again from nothing. Here the same files are fetched into a staging
folder with `download_verified`, which resumes a partial file and checks it
against the size and SHA-256 the Hub lists. The folder moves into fastembed's
cache only once it is whole, laid out as huggingface_hub lays out its own, so
fastembed finds the model cached and downloads nothing.

A Hub that cannot be reached, or that lists something unexpected, leaves the
models to fastembed as before: a stop then costs the file, never the model.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from varyz.filesystem import publish_directory
from varyz.llm.local_runtime import download_verified

if TYPE_CHECKING:
    from varyz.config import Config

PHASE = "Downloading the search models"
# What fastembed takes from a model's repository besides the model's own files.
_EXTRAS = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "preprocessor_config.json",
)
# Beside the cached models, where `models--*` never matches it.
STAGING = ".downloads"
_TIMEOUT = 30.0

# (phase, bytes done, bytes in all), as `download_verified` reports.
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Wanted:
    """A model's Hub repository, and the files fastembed cannot load it without."""

    repo: str
    required: tuple[str, ...]


@dataclass(frozen=True)
class HubFile:
    name: str
    size: int
    # git's id for the file's contents: how the Hub vouches for a small file.
    blob_id: str
    # A large file's, stored through LFS.
    sha256: str | None


@dataclass(frozen=True)
class HubModel:
    repo: str
    commit: str
    files: tuple[HubFile, ...]


def cache_dir(cfg: Config) -> Path:
    """Where fastembed keeps its models: `Embedder` and `Reranker` agree on it."""
    return Path(os.environ.get("VARYZ_EMBEDDING_CACHE", str(cfg.data_dir / "models")))


def cached_folder(repo: str) -> str:
    """huggingface_hub's name for a repository's folder in its cache."""
    return "models--" + repo.replace("/", "--")


def wanted_models(cfg: Config) -> list[Wanted]:
    """The embedding model, and the reranker when `recall` would use it."""
    if os.environ.get("VARYZ_DISABLE_EMBEDDINGS") == "1":
        return []
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from varyz.retrieval.rerank import Reranker, worth_fetching

    kinds: list[tuple[type, str]] = [(TextEmbedding, cfg.embedding.model)]
    reranker = Reranker(cfg)
    # The same conditions as `fetch_reranker`.
    if reranker.calibration is not None and worth_fetching(cfg):
        kinds.append((TextCrossEncoder, reranker.model_name))
    wanted = []
    for kind, name in kinds:
        entry = next((m for m in kind.list_supported_models() if m["model"] == name), None)
        repo = (entry or {}).get("sources", {}).get("hf")
        if entry is None or not repo:
            continue  # not a model fastembed fetches from the Hub: left to it
        wanted.append(Wanted(repo, (entry["model_file"], *entry["additional_files"])))
    return wanted


def hub_model(want: Wanted) -> HubModel:
    """The repository's current commit and the listing of the files to fetch."""
    from huggingface_hub import model_info

    info = model_info(want.repo, files_metadata=True, timeout=_TIMEOUT)
    names = {*_EXTRAS, *want.required}
    files = []
    for sibling in info.siblings or []:
        if sibling.rfilename not in names:
            continue
        lfs = sibling.lfs
        size = lfs.size if lfs else sibling.size
        if size is None or not sibling.blob_id:
            raise ValueError(f"the Hub lists {want.repo}/{sibling.rfilename} without a size")
        files.append(HubFile(sibling.rfilename, int(size), sibling.blob_id,
                             lfs.sha256 if lfs else None))
    missing = set(want.required) - {file.name for file in files}
    if not info.sha or missing:
        raise ValueError(f"the Hub does not list {', '.join(sorted(missing))} in {want.repo}")
    return HubModel(want.repo, info.sha, tuple(files))


def file_url(repo: str, name: str, commit: str) -> str:
    from huggingface_hub import hf_hub_url

    return hf_hub_url(repo, name, revision=commit)


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def fetch_small(url: str, destination: Path, file: HubFile) -> None:
    """A file small enough to fetch whole, checked against its git id."""
    if destination.is_file():
        return  # written only once it matched
    response = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
    response.raise_for_status()
    data = response.content
    if len(data) != file.size or _git_blob_id(data) != file.blob_id:
        raise RuntimeError(f"{file.name} does not match the Hub's listing")
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(data)
    os.replace(part, destination)


def _fetch_large(url: str, destination: Path, file: HubFile, report: Progress | None) -> None:
    try:
        download_verified(url, destination, file.sha256 or "", file.size, PHASE, report)
    except RuntimeError:
        # A file that failed its check would fail it on every run after.
        destination.unlink(missing_ok=True)
        destination.with_name(destination.name + ".part").unlink(missing_ok=True)
        raise


def _after(on_progress: Progress | None, base: int, total: int) -> Progress | None:
    """One file's progress as the whole download's: the files before it are done."""
    if on_progress is None:
        return None

    def report(_phase: str, got: int, _size: int) -> None:
        on_progress(PHASE, base + got, total)

    return report


def fetch_search_models(cfg: Config, on_progress: Progress | None = None) -> bool:
    """Fetch whichever search models are not cached yet, resuming a stopped run.

    True once they are all in place. False when the Hub could not be asked or
    a file did not check out: fastembed then downloads them as it loads them.
    An exception from ``on_progress`` - how a download is paused - is not
    caught, and leaves what has arrived for the next run.
    """
    cache = cache_dir(cfg)
    try:
        missing: list[tuple[HubModel, Path, Path]] = []
        for want in wanted_models(cfg):
            target = cache / cached_folder(want.repo)
            staging = cache / STAGING / cached_folder(want.repo)
            if target.is_dir():
                # Left by a run that fastembed then finished.
                shutil.rmtree(staging, ignore_errors=True)
                continue
            missing.append((hub_model(want), staging, target))
        total = sum(file.size for model, _, _ in missing for file in model.files)
        done = 0
        for model, staging, target in missing:
            snapshots = staging / "snapshots"
            if snapshots.is_dir():
                # Part of a commit the repository has moved on from.
                for old in snapshots.iterdir():
                    if old.name != model.commit:
                        shutil.rmtree(old, ignore_errors=True)
            for file in model.files:
                destination = snapshots / model.commit / file.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                url = file_url(model.repo, file.name, model.commit)
                if file.sha256:
                    _fetch_large(url, destination, file, _after(on_progress, done, total))
                else:
                    fetch_small(url, destination, file)
                done += file.size
                if on_progress is not None:
                    on_progress(PHASE, done, total)
            refs = staging / "refs"
            refs.mkdir(exist_ok=True)
            (refs / "main").write_text(model.commit, encoding="utf-8")
            publish_directory(staging, target)
        downloads = cache / STAGING
        if downloads.is_dir() and not any(downloads.iterdir()):
            downloads.rmdir()
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(
            f"Could not fetch the search models ahead ({exc}); they download as they "
            "load instead, and a stop there starts that file again.",
            file=sys.stderr,
        )
        return False
    return True
