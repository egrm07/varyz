"""Application service for safe temporal-graph rebuilds."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from varyz.config import Config
from varyz.db.connection import LockHeldError, WriteLock
from varyz.domain import iso, new_id, utc_now
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph.cache import ExtractionCache, WindowFingerprints, cache_scope
from varyz.graph.extract import DeterministicExtractor, GraphBuildService
from varyz.graph.repository import GraphRepository, RebuildResult


class GraphRebuildAlreadyRunning(RuntimeError):
    """A graph rebuild already owns the long-lived rebuild claim."""


def graph_rebuild_claim_path(data_dir: Path) -> Path:
    return data_dir / "graph-rebuild.lock"


def _graph_rebuild_claim_metadata_path(data_dir: Path) -> Path:
    return data_dir / "graph-rebuild.lock.json"


def read_graph_rebuild_claim(data_dir: Path) -> dict[str, Any]:
    """Read holder metadata without opening and closing the locked file."""
    metadata = _graph_rebuild_claim_metadata_path(data_dir)
    if metadata.is_file():
        return json.loads(metadata.read_text(encoding="utf-8"))
    # Pre-portability releases stored JSON after the locked byte.
    raw = graph_rebuild_claim_path(data_dir).read_bytes()
    return json.loads(raw.lstrip(b"\0").decode("utf-8"))


class GraphRebuildClaim(WriteLock):
    """Crash-safe cross-process claim that does not exclude archive writers."""

    def __init__(self, data_dir: Path, *, version: str):
        super().__init__(data_dir)
        self.path = graph_rebuild_claim_path(data_dir)
        self.version = version
        self.claim_id = new_id()

    def __enter__(self) -> GraphRebuildClaim:
        try:
            super().__enter__()
        except LockHeldError as exc:
            holder = self._holder_description()
            raise GraphRebuildAlreadyRunning(
                f"Graph rebuild {holder} is already running"
            ) from exc
        payload = {
            "claim_id": self.claim_id,
            "pid": os.getpid(),
            "started_at": iso(utc_now()),
            "version": self.version,
        }
        try:
            metadata = _graph_rebuild_claim_metadata_path(self.path.parent)
            stage = metadata.with_name(f".{metadata.name}.{self.claim_id}.tmp")
            stage.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(stage, metadata)
        except Exception:
            super().__exit__()
            raise
        return self

    def _holder_description(self) -> str:
        try:
            value = read_graph_rebuild_claim(self.path.parent)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return f"holding {self.path.name}"
        claim_id = str(value.get("claim_id") or "holding the claim")
        details = [
            f"PID {value['pid']}" if value.get("pid") is not None else "",
            f"started {value['started_at']}" if value.get("started_at") else "",
            f"version {value['version']}" if value.get("version") else "",
        ]
        suffix = ", ".join(detail for detail in details if detail)
        return f"{claim_id} ({suffix})" if suffix else claim_id

class _WriteLockedProxy:
    """Acquire Varyz' writer lock only for methods that can mutate SQLite."""

    def __init__(self, target: Any, data_dir: Path, write_methods: frozenset[str]):
        self._target = target
        self._data_dir = data_dir
        self._write_methods = write_methods

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name not in self._write_methods:
            return attribute

        @wraps(attribute)
        def locked(*args, **kwargs):
            with WriteLock(self._data_dir):
                return attribute(*args, **kwargs)

        return locked


_REPOSITORY_WRITES = frozenset({"begin_run", "stage_batch", "activate", "fail"})
_CACHE_WRITES = frozenset({"get", "put", "flush", "sweep"})
_RECEIPT_WRITES = frozenset(
    {
        "register_destination",
        "create_grant",
        "add_rule",
        "record_preparation",
        "start_execution",
        "finish_execution",
        "consume_grant",
        "revoke_grant",
    }
)


def facet_max_tokens(batch_max_targets: int) -> int:
    """Output ceiling for one semantic batch, scaled to what it can return.

    A batch may return a facet per target. A fixed ceiling smaller than that
    truncates the JSON list mid-object, and envelope validation then discards
    the whole batch, silently losing every target in it.
    """
    return max(1200, min(4096, 200 + 120 * batch_max_targets))


def build_extraction_cache(
    conn: sqlite3.Connection,
    cfg: Config,
    extractor,
    extractor_version: str,
) -> ExtractionCache | None:
    """The cache scope for this configuration, or None when caching is off.

    Every setting that changes what an extractor is allowed to read, or how it
    reads it, belongs in the scope. A knob left out here would be a rebuild
    quietly replaying answers from the settings it replaced.
    """
    if not cfg.graph.extraction_cache:
        return None
    parameters: dict[str, object] = {
        "predicate_version": cfg.graph.predicate_version,
        "semantic_provider": cfg.graph.semantic_provider,
        "excluded_sources": sorted(cfg.graph.excluded_sources),
    }
    if getattr(extractor, "reads_context", False):
        parameters.update(
            {
                "context_before": cfg.graph.context_before,
                "context_after": cfg.graph.context_after,
                "context_max_gap_hours": cfg.graph.context_max_gap_hours,
                "batch_max_messages": cfg.graph.batch_max_messages,
                "batch_max_targets": cfg.graph.batch_max_targets,
                "max_context_chars": cfg.privacy.max_context_chars,
                "min_confidence": cfg.graph.semantic_min_confidence,
                "privacy_mode": cfg.privacy.mode,
                "policy_version": cfg.privacy.policy_version,
                "destination": getattr(extractor, "destination", None),
                "local_model": getattr(extractor, "local_model", None),
            }
        )
    return ExtractionCache(
        conn,
        scope=cache_scope(
            extractor_name=extractor.name,
            extractor_version=extractor_version,
            model=extractor.model,
            prompt_version=extractor.prompt_version,
            parameters=parameters,
        ),
        windows=(
            WindowFingerprints(
                conn, excluded_sources=tuple(cfg.graph.excluded_sources)
            )
            if getattr(extractor, "reads_context", False)
            else None
        ),
    )


def rebuild_graph(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    version: str | None = None,
    dry_run: bool = False,
    provider=None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> RebuildResult:
    """Build beside the active graph and atomically activate on success.

    ``on_progress`` hears (entries read, entries in the corpus) after each entry.
    """
    selected_version = version or cfg.graph.extractor_version
    with GraphRebuildClaim(cfg.data_dir, version=selected_version):
        return _rebuild_graph(
            conn,
            cfg,
            version=version,
            dry_run=dry_run,
            provider=provider,
            should_stop=should_stop,
            lock_writes=True,
            on_progress=on_progress,
        )


def rebuild_graph_while_locked(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    version: str | None = None,
    dry_run: bool = False,
    provider=None,
    should_stop: Callable[[], bool] | None = None,
) -> RebuildResult:
    """Build the graph while the caller holds Varyz' cross-process write lock.

    Legacy callers that need one non-interleaved writer session use this entry
    point. New callers should use ``rebuild_graph``, whose lock excludes other
    rebuilds for the whole run but excludes archive writers only around graph
    mutations.
    """
    selected_version = version or cfg.graph.extractor_version
    with GraphRebuildClaim(cfg.data_dir, version=selected_version):
        return _rebuild_graph(
            conn,
            cfg,
            version=version,
            dry_run=dry_run,
            provider=provider,
            should_stop=should_stop,
            lock_writes=False,
        )


def _rebuild_graph(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    version: str | None,
    dry_run: bool,
    provider,
    should_stop: Callable[[], bool] | None,
    lock_writes: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> RebuildResult:
    extractor_name = cfg.graph.extractor.casefold().strip()
    if extractor_name == "deterministic":
        extractor = DeterministicExtractor(cfg.owner.names)
    elif extractor_name in {"contextual_facets", "contextual_llm", "hybrid"}:
        from varyz.destinations.llm import LLMDestinationAdapter
        from varyz.graph.semantic import ContextualFacetExtractor, ModelFacetEnvelope
        from varyz.llm.local import build_local_provider
        from varyz.llm.local_runtime import (
            LocalModelNotInstalled,
            install_hint,
            installer_is_shipped,
        )
        from varyz.llm.router import LLMRouter
        from varyz.privacy.broker import EgressBroker
        from varyz.privacy.policy import PolicyEngine
        from varyz.privacy.receipts import ReceiptStore

        if not installer_is_shipped():
            # A build that holds the managed model back holds the semantic
            # graph back with it, the cloud (hybrid) provider included.
            raise LocalModelNotInstalled(
                "This build of varyz builds the graph with the deterministic "
                'extractor only; set [graph] extractor = "deterministic"'
            )
        policy = PolicyEngine(version=cfg.privacy.policy_version)
        receipts: Any = ReceiptStore(conn)
        if lock_writes:
            receipts = _WriteLockedProxy(receipts, cfg.data_dir, _RECEIPT_WRITES)

        def local_adapter(local_provider):
            return EgressBroker(
                receipts,
                policy=policy,
                adapters=(
                    LLMDestinationAdapter("local", local_provider, stream=False),
                ),
                mode=cfg.privacy.mode,
            )

        if cfg.graph.semantic_provider == "local":
            primary_provider = provider or build_local_provider(
                cfg,
                max_tokens=facet_max_tokens(cfg.graph.batch_max_targets),
                context_window=8192,
                think=False,
                format_schema=ModelFacetEnvelope.model_json_schema(),
            )
            if provider is None and not primary_provider.available():
                raise LocalModelNotInstalled(
                    f"Local semantic extraction requires the managed model; {install_hint()}"
                )
            destination = "local"
            broker = local_adapter(primary_provider)
            primary_model = str(
                getattr(primary_provider, "model", cfg.local_model.model)
            )
            # In local mode the primary broker already keeps every facet on
            # this machine, so sensitive entries route to that same broker
            # rather than degrading to the deterministic fallback.
            local_broker = broker
            local_model = primary_model
        else:
            primary_provider = provider or LLMRouter(cfg).enrich_provider()
            destination = f"cloud:{cfg.llm.provider}"
            broker = EgressBroker(
                receipts,
                policy=policy,
                adapters=(
                    LLMDestinationAdapter(
                        destination,
                        primary_provider,
                        stream=False,
                    ),
                ),
                mode=cfg.privacy.mode,
            )
            primary_model = str(
                getattr(
                    primary_provider,
                    "model",
                    cfg.llm.models_for().enrich_model,
                )
            )
            sensitive_provider = build_local_provider(
                cfg,
                max_tokens=facet_max_tokens(cfg.graph.batch_max_targets),
                context_window=8192,
                think=False,
                format_schema=ModelFacetEnvelope.model_json_schema(),
            )
            local_broker = (
                local_adapter(sensitive_provider)
                if sensitive_provider.available()
                else None
            )
            local_model = cfg.local_model.model if local_broker else None
        extractor = ContextualFacetExtractor(
            conn,
            broker,
            destination=destination,
            model=primary_model,
            context_before=cfg.graph.context_before,
            context_after=cfg.graph.context_after,
            context_max_gap_hours=cfg.graph.context_max_gap_hours,
            batch_max_messages=cfg.graph.batch_max_messages,
            batch_max_targets=cfg.graph.batch_max_targets,
            batch_max_chars=cfg.privacy.max_context_chars,
            min_confidence=cfg.graph.semantic_min_confidence,
            local_broker=local_broker,
            local_model=local_model,
            owner_names=cfg.owner.names,
        )
    else:
        raise ValueError(
            "[graph] extractor must be deterministic or contextual_facets, "
            f"got {cfg.graph.extractor!r}"
        )
    selected_version = version or cfg.graph.extractor_version
    repository: Any = GraphRepository(
        conn,
        predicate_version=cfg.graph.predicate_version,
        identity_version=cfg.graph.identity_version,
        candidate_threshold=cfg.graph.candidate_threshold,
    )
    cache: Any = build_extraction_cache(
        conn, cfg, extractor, selected_version
    )
    if lock_writes:
        repository = _WriteLockedProxy(repository, cfg.data_dir, _REPOSITORY_WRITES)
        if cache is not None:
            cache = _WriteLockedProxy(cache, cfg.data_dir, _CACHE_WRITES)
    return GraphBuildService(
        repository,
        SQLiteEvidenceReader(
            conn, excluded_sources=tuple(cfg.graph.excluded_sources)
        ),
        extractor,
        cache=cache,
        max_consecutive_failures=cfg.graph.max_consecutive_failures,
    ).rebuild(
        version=selected_version,
        dry_run=dry_run,
        should_stop=should_stop,
        on_progress=on_progress,
        # Naming a version is the operator saying which generation they want,
        # so an intentional rollback is allowed where a stale default is not.
        allow_downgrade=version is not None,
    )
