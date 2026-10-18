"""Deterministic routing between entry, graph, conflict, and mixed retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class RetrievalMode(StrEnum):
    ENTRY = "entry"
    CLAIM = "claim"
    ENTITY_NEIGHBORHOOD = "entity_neighborhood"
    CONFLICT = "conflict"
    MIXED = "mixed"


@dataclass(frozen=True)
class RetrievalPlan:
    mode: RetrievalMode
    reason_code: str


CONFLICT_RE = re.compile(
    r"\b(conflict\w*|contradict\w*|disagree\w*|different dates|uncertain|"
    r"fact or|only a plan)\b",
    re.IGNORECASE,
)
TEMPORAL_RE = re.compile(
    r"\b(when|where was|what year|what month|before|after|during|first mention|"
    r"living in|working on|decide|changed|postponed|superseded)\b",
    re.IGNORECASE,
)
RELATION_RE = re.compile(
    r"\b(relationship|friendship|with [A-Z][\w'-]+|who is|about [A-Z][\w'-]+)\b",
    re.IGNORECASE,
)
NARRATIVE_RE = re.compile(
    r"\b(tell me|story|remember|what happened|quote|message|email|journal|photo)\b",
    re.IGNORECASE,
)
REFLECTIVE_RE = re.compile(
    r"\b(why|evidence suggests|support the idea|alternative explanation|pattern|"
    r"transition year|became closer|how did)\b",
    re.IGNORECASE,
)
BEHAVIOR_RE = re.compile(
    r"\b(lie|lied|lying|dishonest|deceiv\w*|apolog\w*|ghost\w*|ignor(?:e|ed|ing)|"
    r"argu(?:e|ed|ing)|fought|betray\w*|cross\w* boundar\w*|"
    r"broke .{0,15} promise|support(?:ed|ing)?)\b",
    re.IGNORECASE,
)


def plan_query(question: str, *, graph_available: bool = True) -> RetrievalPlan:
    if not graph_available:
        return RetrievalPlan(RetrievalMode.ENTRY, "no_active_graph")
    if CONFLICT_RE.search(question):
        return RetrievalPlan(RetrievalMode.CONFLICT, "conflict_intent")
    if REFLECTIVE_RE.search(question):
        return RetrievalPlan(RetrievalMode.MIXED, "reflective_intent")
    if BEHAVIOR_RE.search(question):
        return RetrievalPlan(RetrievalMode.MIXED, "behavioral_evidence_intent")
    if RELATION_RE.search(question):
        return RetrievalPlan(
            RetrievalMode.ENTITY_NEIGHBORHOOD, "entity_neighborhood_intent"
        )
    if TEMPORAL_RE.search(question):
        return RetrievalPlan(RetrievalMode.CLAIM, "factual_temporal_intent")
    if NARRATIVE_RE.search(question):
        return RetrievalPlan(RetrievalMode.ENTRY, "narrative_or_quote_intent")
    return RetrievalPlan(RetrievalMode.MIXED, "default_mixed")
