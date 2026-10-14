"""Question-depth classification for Ask.

The classifier is intentionally local and deterministic. It decides how much
retrieval and synthesis work a question deserves before any cloud call happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DepthProfile:
    level: int
    label: str
    intent: str
    candidate_multiplier: float
    send_multiplier: float

    @property
    def is_deep(self) -> bool:
        return self.level >= 4


_SIMPLE_PATTERNS = [
    r"\bwhen\b",
    r"\bwhere\b",
    r"\bwho\b",
    r"\bfind\b",
    r"\bshow\b",
    r"\bwhat did\b",
    r"\bremind me\b",
]

_REFLECTIVE_PATTERNS = [
    r"\bwhy\b",
    r"\bhow did\b",
    r"\bpattern\b",
    r"\btheme\b",
    r"\bchanged?\b",
    r"\brelationship\b",
    r"\bfeel\b",
]

_DEEP_PATTERNS = [
    r"\b(?:lie|lied|lying|dishonest|deceiv\w*|misled|betray\w*)\b",
    r"\bmanipulat(?:e|ed|ing|ion|ive)\b",
    r"\bcoerc(?:e|ed|ion|ive)\b",
    r"\bgaslight(?:ing|ed)?\b",
    r"\babuse|abusive\b",
    r"\btoxic\b",
    r"\btrauma\b",
    r"\battachment\b",
    r"\bself[- ]?sabotage\b",
    r"\bblind spot\b",
    r"\bwhat am i not seeing\b",
    r"\bdeeper insight\b",
    r"\bhidden pattern\b",
    r"\bhappiest\b",
    r"\bsaddest\b",
    r"\bbest moment\b",
    r"\bworst moment\b",
    r"\bbest friend\b",
    r"\bclosest friend\b",
    r"\bclosest person\b",
    r"\bmost important (friend|person|relationship)\b",
    r"\bmost (happy|alive|fulfilled|painful|important|meaningful|proud)\b",
]

_JUDGMENT_PATTERNS = [
    r"\bwas i\b",
    r"\bam i\b",
    r"\bdid they\b",
    r"\bshould i\b",
    r"\bdo you think\b",
    r"\bmeaning\b",
]


PROFILES = {
    1: DepthProfile(1, "simple", "locate a memory or answer a factual question", 1.0, 1.0),
    2: DepthProfile(2, "contextual", "connect a few related memories", 1.25, 1.15),
    3: DepthProfile(3, "reflective", "summarize patterns and changes over time", 1.75, 1.5),
    4: DepthProfile(4, "deep", "analyze motives, dynamics, and competing explanations", 2.5, 2.0),
    5: DepthProfile(
        5,
        "sensitive deep",
        "handle emotionally loaded interpretation carefully",
        3.0,
        2.25,
    ),
}


def _matches(patterns: list[str], text: str) -> int:
    return sum(1 for pattern in patterns if re.search(pattern, text))


def classify_depth(question: str) -> DepthProfile:
    q = question.strip().lower()
    if not q:
        return PROFILES[1]

    score = 1
    simple = _matches(_SIMPLE_PATTERNS, q)
    reflective = _matches(_REFLECTIVE_PATTERNS, q)
    deep = _matches(_DEEP_PATTERNS, q)
    judgment = _matches(_JUDGMENT_PATTERNS, q)

    if simple and not reflective and not deep and not judgment:
        return PROFILES[1]

    if len(q.split()) >= 12 or "?" in q:
        score = max(score, 2)
    if reflective:
        score = max(score, 3)
    if judgment and reflective:
        score = max(score, 4)
    if deep:
        score = max(score, 5 if judgment else 4)

    return PROFILES[min(score, 5)]
