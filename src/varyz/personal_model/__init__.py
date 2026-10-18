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
]
