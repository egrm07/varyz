"""Local heuristic significance scoring — free, instant, runs for every entry.

Deliberate captures (journals, manual notes, photos) start high: the user
chose to record them. Chat messages start low and must earn their place on
the timeline through length or life-event signals. The LLM refinement pass
(pipeline/enrich.py) later overrides these scores for cloud-eligible chats;
this heuristic is all that sensitive content ever gets, by design.
"""

from __future__ import annotations

import re

from varyz.domain import Kind, NormalizedEntry

KIND_BASE = {
    Kind.JOURNAL: 0.90,
    Kind.NOTE: 0.80,       # manual capture: the user bothered to write it down
    Kind.PHOTO: 0.75,
    Kind.DOCUMENT: 0.70,
    Kind.CALENDAR_EVENT: 0.60,
    Kind.EMAIL: 0.60,
    Kind.TRANSCRIPT: 0.50,
    Kind.MESSAGE: 0.25,
}

LIFE_EVENT_RE = re.compile(
    r"\b(mov(?:e|ed|ing)(?:\s+\w+){0,2}\s+to|new (?:job|flat|apartment|house)|"
    r"quit|fired|hired|promot(?:ed|ion)|engag(?:ed|ement)|marr(?:y|ied|iage)|"
    r"wedding|pregnan(?:t|cy)|born|birth|died|death|funeral|hospital|surgery|"
    r"graduat(?:e|ed|ion)|diploma|visa|passport|flight to|landed in|"
    r"broke up|break.?up|divorce|anniversary|birthday|"
    r"signed|contract|offer|accepted|bought|sold)\b",
    re.IGNORECASE,
)

# A message is noise when it is nothing but these atoms (any mix, any order):
# laughter, acknowledgements, greetings, thanks, emoji.
_NOISE_WORD = (
    r"(?:a?ha)+h?|lo+l+|xd+|ok(?:ay)?|yes+|no+|yea+h*|nah+|thanks?|thx|ty|you|u|so+|"
    r"much|please|pls|good|morning|night|gn|gm|omg|wow+|nice+|coo+l+|great|same|true|"
    r"lmao+|hi+|hey+|hello+|bye+|yo|love (?:it|you)|❤+"
)
NOISE_RE = re.compile(
    rf"^(?:[\s\W]*(?:\b(?:{_NOISE_WORD})\b|[\U0001F300-\U0001FAFF☀-➿]+))+[\s\W]*$",
    re.IGNORECASE,
)

NEWSLETTER_RE = re.compile(
    r"\b(unsubscribe|view (?:this email )?in (?:your )?browser|no.?reply@|"
    r"you (?:are|’re|'re) receiving this)\b",
    re.IGNORECASE,
)


def score(norm: NormalizedEntry) -> float:
    return score_text(norm.kind, norm.body)


def score_text(kind: Kind, body: str) -> float:
    base = KIND_BASE.get(kind, 0.5)
    text = body or ""
    # Strip the From:/Chat: byline block so length reflects actual content.
    if text.startswith("From:"):
        text = text.partition("\n\n")[2]
    text = text.strip()

    s = base
    if kind == Kind.MESSAGE:
        if NOISE_RE.fullmatch(text) or len(text) < 12:
            return 0.05
        if len(text) > 200:
            s += 0.15
        if len(text) > 500:
            s += 0.10
        if LIFE_EVENT_RE.search(text):
            s += 0.35
        if text.endswith("?") and len(text) > 80:
            s += 0.05
    elif kind == Kind.EMAIL:
        if NEWSLETTER_RE.search(text):
            s = 0.20
        elif LIFE_EVENT_RE.search(text):
            s += 0.20
    elif LIFE_EVENT_RE.search(text):
        s += 0.05

    return round(min(s, 1.0), 3)
