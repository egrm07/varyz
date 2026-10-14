"""Safe first-run discovery for local evidence sources.

The bootstrap deliberately has a very small ambition: make a fresh archive
useful without asking for an account or a secret.  It discovers only ordinary
documents and local Git repositories, shows the exact roots, and writes
``approved = true`` only after the caller has collected explicit consent.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from varyz import connectors as connector_registry  # noqa: F401 - registers connectors
from varyz.config import Config, load_config
from varyz.configfile import upsert_connector
from varyz.connectors.ai_conversations import detect_adapters
from varyz.connectors.base import get_connector
from varyz.connectors.git_repos import DEFAULT_SCAN_DEPTH, find_repos
from varyz.db.connection import WriteLock, open_db
from varyz.domain import IngestStats
from varyz.graph.repository import RebuildResult
from varyz.pipeline.embed import Embedder, embed_pending
from varyz.pipeline.ingest import sync_connector
from varyz.vault import Vault

# AI conversations comes first because it holds the owner's own words, and it
# needs no account, no password, and no export request — the histories are
# already on the disk.
LOCAL_CONNECTORS = ("ai_conversations", "documents", "git")


@dataclass(frozen=True)
class DetectedSources:
    """Local roots that are useful enough to offer during first-run."""

    document_paths: tuple[Path, ...] = ()
    git_paths: tuple[Path, ...] = ()
    # Adapter ids whose transcript directories exist on this machine. Roots are
    # not stored: each adapter knows its own default locations, and pinning
    # them here would silently stop finding new sessions if a tool moved them.
    ai_adapters: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.document_paths or self.git_paths or self.ai_adapters)


@dataclass(frozen=True)
class LocalSyncResult:
    stats: tuple[IngestStats, ...]
    embedded: int
    failed: tuple[tuple[str, str], ...]
    graph: RebuildResult | None = None
    graph_error: str | None = None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _without_nested(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Drop a git root the connector would already reach from a wider one.

    The checkout you are standing in is usually inside the projects folder
    beside it, so the consent screen offered both -- listing a repository and
    its own parent, and asking a person to approve a scan of each. The
    document roots already refuse that overlap; the git roots did not. Keep
    the wider root, and only where the connector's own scan depth reaches the
    narrower one, so nothing stops being offered.
    """

    kept: list[Path] = []
    for root in roots:
        covered = any(
            root != other
            and _inside(root, other)
            and len(root.relative_to(other).parts) <= DEFAULT_SCAN_DEPTH
            for other in roots
        )
        if not covered:
            kept.append(root)
    return tuple(kept)


def _unique_existing(paths: list[Path]) -> tuple[Path, ...]:
    # One folder is one root however it is spelled: on macOS's case-insensitive
    # disk `Documents/projects` and `Documents/Projects` are the same folder,
    # and `resolve` keeps whichever case it was given.
    unique: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
            if not resolved.is_dir():
                continue
            stat = resolved.stat()
        except OSError:
            continue
        # FAT and some network drives report no inode; there the path decides.
        identity = (stat.st_dev, stat.st_ino) if stat.st_ino else None
        if identity in seen or resolved in unique:
            continue
        if identity is not None:
            seen.add(identity)
        unique.append(resolved)
    return tuple(unique)


def discover_local_sources(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    documents_dir: Path | None = None,
) -> DetectedSources:
    """Find conservative, local-only roots without reading file contents.

    The function inspects directory names and ``.git`` markers only.  In
    particular, discovery itself never opens a document.  A project checkout
    is useful for both its current docs and commit history; a conventional
    Documents folder is offered for documents, while only conventional code
    folders are searched for repositories.
    """

    home = (home or Path.home()).expanduser().resolve()
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    if documents_dir is None:
        configured = os.environ.get("XDG_DOCUMENTS_DIR")
        documents_dir = (
            Path(os.path.expandvars(configured)).expanduser() if configured else home / "Documents"
        )
    documents_dir = documents_dir.expanduser().resolve()

    document_roots: list[Path] = []
    if documents_dir.is_dir():
        document_roots.append(documents_dir)

    # A checkout outside Documents is a narrow, high-signal source.  Never
    # offer the home directory itself: consent to index "$HOME" is too broad.
    cwd_repos = find_repos(cwd, max_depth=0) if cwd != home else []
    if cwd_repos and not _inside(cwd, documents_dir):
        document_roots.append(cwd)

    git_roots: list[Path] = []
    if cwd_repos:
        git_roots.append(cwd)
    conventional = (
        home / "projects",
        home / "src",
        home / "code",
        documents_dir / "projects",
        documents_dir / "Projects",
    )
    for root in _unique_existing(list(conventional)):
        # This reads directory entries and .git markers, never repository
        # contents or remotes.  Keep the scan deliberately shallow.
        if find_repos(root, max_depth=2):
            git_roots.append(root)

    return DetectedSources(
        document_paths=_unique_existing(document_roots),
        git_paths=_without_nested(_unique_existing(git_roots)),
        # Directory names only, exactly like the checks above: which agents
        # this machine has, never a line of what was said to them.
        ai_adapters=tuple(adapter.adapter_id for adapter, _ in detect_adapters(home)),
    )


def _raw_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def missing_detected_sources(config_path: Path, detected: DetectedSources) -> DetectedSources:
    """Return only connector groups not already owned by the user's config."""

    connectors = _raw_config(config_path).get("connectors", {})
    configured = connectors if isinstance(connectors, dict) else {}
    return DetectedSources(
        document_paths=() if "documents" in configured else detected.document_paths,
        git_paths=() if "git" in configured else detected.git_paths,
        ai_adapters=(() if "ai_conversations" in configured else detected.ai_adapters),
    )


def approve_detected_sources(config_path: Path, detected: DetectedSources) -> tuple[str, ...]:
    """Persist exact approved roots, without overwriting configured connectors."""

    missing = missing_detected_sources(config_path, detected)
    added: list[str] = []
    if missing.ai_adapters:
        upsert_connector(
            config_path,
            "ai_conversations",
            {
                "approved": True,
                "adapters": list(missing.ai_adapters),
                # The assistant's half of the transcript is available but off:
                # it is the larger half by volume and the smaller half by
                # relevance, and it is never the user's own words.
                "include_assistant": False,
            },
        )
        added.append("ai_conversations")
    if missing.document_paths:
        upsert_connector(
            config_path,
            "documents",
            {"approved": True, "paths": [str(path) for path in missing.document_paths]},
        )
        added.append("documents")
    if missing.git_paths:
        upsert_connector(
            config_path,
            "git",
            {"approved": True, "paths": [str(path) for path in missing.git_paths]},
        )
        added.append("git")
    return tuple(added)


def approved_local_connectors(config_path: Path) -> tuple[str, ...]:
    """List only explicitly approved local connectors with at least one path."""

    cfg = load_config(config_path)
    approved = []
    for connector_id in LOCAL_CONNECTORS:
        options = cfg.connector_options(connector_id)
        if options.get("approved") is not True:
            continue
        # AI conversations is the one local connector that finds its own roots,
        # so requiring `paths` would exclude exactly the source onboarding
        # leads with.
        if connector_id == "ai_conversations" or options.get("paths"):
            approved.append(connector_id)
    return tuple(approved)


def sync_local_sources(
    config_path: Path,
    connector_ids: tuple[str, ...],
    *,
    on_progress: Callable[[str, IngestStats], None] | None = None,
) -> LocalSyncResult:
    """Sync only local bootstrap connectors, then build their local index."""

    invalid = sorted(set(connector_ids) - set(LOCAL_CONNECTORS))
    if invalid:
        raise ValueError(f"bootstrap cannot sync non-local connector(s): {', '.join(invalid)}")

    cfg: Config = load_config(config_path)
    conn = open_db(cfg.db_path)
    vault = Vault(cfg.vault_dir)
    stats: list[IngestStats] = []
    failed: list[tuple[str, str]] = []
    graph: RebuildResult | None = None
    graph_error: str | None = None
    try:
        with WriteLock(cfg.data_dir):
            for connector_id in connector_ids:
                try:
                    result = sync_connector(
                        conn,
                        vault,
                        get_connector(connector_id),
                        cfg,
                        on_progress=(
                            (lambda value, cid=connector_id: on_progress(cid, value))
                            if on_progress
                            else None
                        ),
                    )
                except (ValueError, FileNotFoundError, PermissionError) as exc:
                    failed.append((connector_id, str(exc)))
                    continue
                stats.append(result)
            embedded = embed_pending(conn, cfg, Embedder(cfg)) if stats else 0
            if stats:
                try:
                    from varyz.graph.service import rebuild_graph_while_locked

                    graph = rebuild_graph_while_locked(conn, cfg)
                except Exception as exc:
                    # Ingestion and vectors are already committed and remain useful.
                    # Report graph failure as a first-run failure so setup never claims
                    # that `understand` is ready when only `recall` can answer.
                    graph_error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return LocalSyncResult(
        tuple(stats),
        embedded,
        tuple(failed),
        graph=graph,
        graph_error=graph_error,
    )
