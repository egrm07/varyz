"""Versioned, rebuildable temporal truth projection."""

from varyz.graph.models import (
    Claim,
    ClaimCandidate,
    ClaimModality,
    Entity,
    EntityAlias,
    EntityType,
    EvidenceLink,
    GraphRun,
)

__all__ = [
    "Claim",
    "ClaimCandidate",
    "ClaimModality",
    "Entity",
    "EntityAlias",
    "EntityType",
    "EvidenceLink",
    "GraphRun",
    "builder_is_shipped",
]


def builder_is_shipped() -> bool:
    """Whether this build can build the graph (docs/SHIPPING.md).

    The alpha leaves the builder out of the package: no answer read the graph.
    Its records, the corrections it would replay and their time parsing stay,
    since the trust centre writes corrections without a graph to apply them to.
    """
    import importlib.util

    # find_spec, not an import: the module stays unloaded until a build runs.
    try:
        return importlib.util.find_spec("varyz.graph.service") is not None
    except ModuleNotFoundError as exc:
        if exc.name != "varyz.graph.service":
            raise
        return False
