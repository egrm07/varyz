"""Canonical personal-model domain contracts.

The package deliberately exposes only pure, immutable records here. Persistence,
inference providers, and projections belong to adapters built around these contracts.
"""

from varyz.personal_model.models import (
    ConversationEpisode,
    Event,
    Fact,
    Goal,
    Interpretation,
    InterpretationCandidate,
    Observation,
    Person,
    Preference,
    Project,
    RelationshipSnapshot,
)

__all__ = [
    "ConversationEpisode",
    "Event",
    "Fact",
    "Goal",
    "Interpretation",
    "InterpretationCandidate",
    "Observation",
    "Person",
    "Preference",
    "Project",
    "RelationshipSnapshot",
    "compiler_is_shipped",
]


def compiler_is_shipped() -> bool:
    """Whether this build can compile a personal model (docs/SHIPPING.md).

    The alpha leaves the compiler out of the package; its records, query and
    repository stay, since the resolver and the pipeline share their types.
    """
    import importlib.util

    # find_spec, not an import: the module stays unloaded until a compile runs.
    try:
        return importlib.util.find_spec("varyz.personal_model.compiler") is not None
    except ModuleNotFoundError as exc:
        if exc.name != "varyz.personal_model.compiler":
            raise
        return False
