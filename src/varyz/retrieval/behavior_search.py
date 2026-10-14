"""Deterministic understanding and evidence checks for behavioral searches.

This is deliberately an ontology and reranker, not a lie detector. It recognizes
what the user is looking for, constrains results to named people, and distinguishes
an admission or observable contrast from a merely semantically similar message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from varyz.domain import Entry, SearchHit

TOKEN_RE = re.compile(r"[^\W_][\w'’.-]*", re.UNICODE)


@dataclass(frozen=True)
class BehaviorConcept:
    key: str
    label: str
    query_patterns: tuple[str, ...]
    fts_terms: tuple[str, ...]
    rewrite: str
    first_person_patterns: tuple[str, ...]
    second_person_patterns: tuple[str, ...]
    evidence_patterns: tuple[str, ...]
    contextual_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class BehaviorIntent:
    concept: BehaviorConcept
    focus_terms: tuple[str, ...]
    focus_label: str | None
    actor: str  # user | target | unknown

    @property
    def expanded_query(self) -> str:
        target = f" involving {self.focus_label}" if self.focus_label else ""
        actor = "the user" if self.actor == "user" else "the other person"
        return f"Evidence that {actor} {self.concept.rewrite}{target}"


@dataclass(frozen=True)
class EvidenceAssessment:
    relevant: bool
    score: float = 0.0
    strength: str | None = None
    reasons: tuple[str, ...] = ()


CONCEPTS = (
    BehaviorConcept(
        key="deception",
        label="deception",
        query_patterns=(
            r"\b(?:lie|lied|lying|dishonest|deceiv\w*|misle(?:ad|d)|not honest|"
            r"wasn['’]?t honest)\b",
            r"\b(?:hid|hide|withheld|concealed)\s+(?:the\s+)?truth\b",
            r"\b(?:made|make)\s+(?:it|that|something|a story|an excuse)\s+up\b",
        ),
        fts_terms=(
            "lie",
            "lied",
            "lying",
            "dishonest",
            "deceived",
            "misled",
            "made up",
            "wasn't honest",
            "not honest",
            "pretended",
            "fake excuse",
            "hid the truth",
            "let them think",
            "told",
            "actually",
            "even though",
            "let",
            "mentí",
            "mentira",
            "engañé",
            "fingí",
            "inventé",
            "oculté",
            "en realidad",
            "соврал",
            "соврала",
            "обманул",
            "обманула",
            "притворился",
            "притворилась",
            "на самом деле",
        ),
        rewrite="admitted dishonesty, invented an excuse, concealed the truth, "
        "or contrasted what was said with what actually happened",
        first_person_patterns=(
            r"\b(?:i|we)\s+(?:lied|lie|wasn['’]?t honest|was not honest|"
            r"deceived|misled|pretended)\b",
            r"\b(?:i|we)\s+(?:made|invented)\s+(?:it|that|something|a story|"
            r"an excuse)\s+up\b",
            r"\b(?:i|we)\s+made\s+up\s+(?:it|that|something|a story|an excuse)\b",
            r"\b(?:i|we)\s+(?:hid|concealed|withheld)\s+(?:the\s+)?truth\b",
            r"\b(?:i|we)\s+let\s+[\w'’.-]+\s+think\b",
            r"\b(?:le\s+)?mentí\b|\bengañé\b|\bfingí\b|\binventé\b|\boculté\b",
            r"\b(?:я\s+)?(?:соврал(?:а)?|обманул(?:а)?|притворил(?:ся|ась)|"
            r"выдумал(?:а)?|скрыл(?:а)?)\b",
        ),
        second_person_patterns=(
            r"\byou\s+(?:lied|were dishonest|deceived|misled|pretended)\b",
            r"\byou\s+(?:told|said|claimed).{0,100}\bbut\b",
            r"\bme dijiste\b.{0,100}\bpero\b",
            r"\bты\s+(?:соврал(?:а)?|обманул(?:а)?)\b",
        ),
        evidence_patterns=(
            r"\b(?:lie|lied|lying|dishonest|deceiv\w*|misled|pretended|"
            r"fake excuse|made .{0,18} up)\b",
            r"\b(?:mentí|mentira|engañé|fingí|inventé|oculté)\b",
            r"\b(?:соврал(?:а)?|обманул(?:а)?|притворил(?:ся|ась)|выдумал(?:а)?)\b",
        ),
        contextual_patterns=(
            r"\b(?:i|we)\s+(?:told|said|claimed).{0,180}\b(?:but|although|"
            r"even though|actually|in fact|truth (?:is|was))\b",
            r"\b(?:i|we)\s+let\s+[\w'’.-]+\s+think.{0,180}\b(?:even though|"
            r"but|actually)\b",
            r"\b(?:le\s+dije|dije|afirmé).{0,180}\b(?:pero|en realidad)\b",
            r"\b(?:я\s+сказал(?:а)?|я\s+утверждал(?:а)?).{0,180}"
            r"\b(?:но|на самом деле)\b",
        ),
    ),
    BehaviorConcept(
        key="apology",
        label="apology or repair",
        query_patterns=(
            r"\b(?:apolog(?:y|ize|ized|ise|ised)|sorry|regret|make amends)\b",
        ),
        fts_terms=("apologize", "apologized", "apology", "sorry", "regret", "make amends"),
        rewrite="apologized, expressed regret, or tried to repair harm",
        first_person_patterns=(
            r"\b(?:i['’]?m|i am|we(?:'re| are))\s+sorry\b",
            r"\b(?:i|we)\s+(?:apologized?|regret(?:ted)?)\b",
            r"\b(?:i|we)\s+owe\s+(?:you|[\w'’.-]+)\s+an apology\b",
            r"\b(?:i|we)\s+(?:want|wanted|tried|am trying)\s+to\s+make amends\b",
        ),
        second_person_patterns=(r"\byou\s+(?:apologized?|said sorry)\b",),
        evidence_patterns=(
            r"\b(?:apolog(?:y|ize|ized|ise|ised)|sorry|regret(?:ted)?|make amends)\b",
        ),
    ),
    BehaviorConcept(
        key="avoidance",
        label="avoidance",
        query_patterns=(r"\b(?:ghost(?:ed|ing)?|ignor(?:e|ed|ing)|avoid(?:ed|ing)?)\b",),
        fts_terms=(
            "ghosted",
            "ignored",
            "ignoring",
            "avoided",
            "never replied",
            "stopped replying",
            "disappeared",
        ),
        rewrite="ignored, avoided, disappeared on, or repeatedly did not reply to",
        first_person_patterns=(
            r"\b(?:i|we)\s+(?:ghosted|ignored|kept ignoring|avoided)\b",
            r"\b(?:i|we)\s+(?:never|stopped)\s+(?:replied|replying|responded|responding)\b",
        ),
        second_person_patterns=(
            r"\byou\s+(?:ghosted|ignored|avoided)\b",
            r"\byou\s+never\s+(?:replied|responded)\b",
            r"\b(?:i|we)\s+(?:sent|wrote).{0,80}\byou\s+never\s+(?:replied|responded)\b",
        ),
        evidence_patterns=(
            r"\b(?:ghosted|ignoring|ignored|avoided|never replied|never responded|"
            r"stopped replying|disappeared)\b",
        ),
    ),
    BehaviorConcept(
        key="conflict",
        label="conflict",
        query_patterns=(r"\b(?:argu(?:e|ed|ing)|fight|fought|conflict|fell out)\b",),
        fts_terms=(
            "argued",
            "argument",
            "fight",
            "fought",
            "conflict",
            "fell out",
            "raised our voices",
        ),
        rewrite="argued, fought, fell out, or had an interpersonal conflict with",
        first_person_patterns=(
            r"\b(?:i|we)\s+(?:argued|fought|fell out)\b",
            r"\b[\w'’.-]+\s+and\s+i\s+had\s+(?:an?\s+)?(?:argument|fight|conflict)\b",
            r"\b(?:we|both)\s+raised\s+our voices\b",
        ),
        second_person_patterns=(r"\byou\s+(?:argued|fought)\b",),
        evidence_patterns=(r"\b(?:argued|argument|fight|fought|conflict|fell out)\b",),
    ),
    BehaviorConcept(
        key="support",
        label="support",
        query_patterns=(r"\b(?:support(?:ed|ing)?|helped|was there for)\b",),
        fts_terms=(
            "supported",
            "helped",
            "was there for",
            "stayed with",
            "didn't want to be alone",
        ),
        rewrite="supported, helped, accompanied, or was emotionally present for",
        first_person_patterns=(
            r"\b(?:i|we)\s+(?:supported|helped|stayed with|was there for|"
            r"accompanied)\b",
        ),
        second_person_patterns=(r"\byou\s+(?:supported|helped|were there for)\b",),
        evidence_patterns=(
            r"\b(?:supported|helped|stayed with|was there for|accompanied)\b",
        ),
    ),
    BehaviorConcept(
        key="boundary",
        label="boundary crossing",
        query_patterns=(r"\b(?:cross(?:ed|ing)?|violate(?:d|ing)?)\s+.{0,20}\bboundar",),
        fts_terms=(
            "crossed boundary",
            "violated boundary",
            "asked me not to",
            "told me not to",
            "did it anyway",
            "posted it anyway",
        ),
        rewrite="crossed a stated boundary or did something after being asked not to",
        first_person_patterns=(
            r"\b[\w'’.-]+\s+(?:asked|told)\s+me\s+not\s+to.{0,180}"
            r"\b(?:but\s+)?i\b.{0,100}\banyway\b",
            r"\b(?:i|we)\s+(?:crossed|violated|ignored)\s+.{0,30}\bboundar",
        ),
        second_person_patterns=(
            r"\byou\s+(?:crossed|violated|ignored)\s+.{0,30}\bboundar",
        ),
        evidence_patterns=(
            r"\b(?:crossed|violated|ignored)\s+.{0,30}\bboundar",
            r"\b(?:asked|told)\s+me\s+not\s+to.{0,180}\banyway\b",
        ),
    ),
    BehaviorConcept(
        key="promise",
        label="promise or commitment",
        query_patterns=(
            r"\b(?:promise(?:d|s)?|commitment)\b",
            r"\bbroke\s+(?:my\s+|a\s+|the\s+)?promise\b",
        ),
        fts_terms=(
            "promised",
            "promise",
            "broke my promise",
            "didn't keep my word",
            "cancelled again",
        ),
        rewrite="made or broke a promise or consequential commitment to",
        first_person_patterns=(
            r"\b(?:i|we)\s+promised\b",
            r"\b(?:i|we)\s+(?:broke|didn['’]?t keep)\s+.{0,20}\b(?:promise|word)\b",
        ),
        second_person_patterns=(r"\byou\s+(?:promised|broke your promise)\b",),
        evidence_patterns=(r"\b(?:promise|promised|broke .{0,20} promise|keep my word)\b",),
        contextual_patterns=(
            r"\b(?:i|we)\s+promised.{0,180}\b(?:but|then)\b.{0,100}"
            r"\b(?:cancelled|canceled|didn['’]?t|couldn['’]?t|failed)\b",
        ),
    ),
    BehaviorConcept(
        key="betrayal",
        label="betrayal",
        query_patterns=(r"\b(?:betray(?:ed|ing|al)?|broke .{0,15} trust)\b",),
        fts_terms=("betrayed", "betrayal", "broke your trust", "broke their trust"),
        rewrite="betrayed the person or knowingly broke their trust",
        first_person_patterns=(
            r"\b(?:i|we)\s+(?:betrayed|broke)\b.{0,40}\b(?:trust|confidence)\b",
        ),
        second_person_patterns=(
            r"\byou\s+(?:betrayed|broke)\b.{0,40}\b(?:trust|confidence)\b",
        ),
        evidence_patterns=(r"\b(?:betray(?:ed|al)?|broke .{0,20} trust)\b",),
    ),
)

QUERY_STOP = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "any",
    "at",
    "did",
    "do",
    "evidence",
    "find",
    "for",
    "from",
    "give",
    "i",
    "in",
    "involving",
    "me",
    "my",
    "of",
    "or",
    "show",
    "something",
    "the",
    "them",
    "this",
    "to",
    "was",
    "when",
    "where",
    "with",
}


def analyze_behavior_query(query: str) -> BehaviorIntent | None:
    """Read an interpersonal-behavior question, or decline the query.

    Declining matters as much as recognising. A behavior keyword alone is not
    evidence of a behavioral question — "avoid", "support", "ignore", "promise"
    and "conflict" are ordinary technical English, and a question like "how does
    sync avoid duplicate entries?" contains one by coincidence. Routing it here
    is not a mild mis-ranking: `_behavioral_search` filters its candidates
    through `assess_behavior_evidence`, so a wrongly-claimed query comes back
    empty rather than merely mis-ordered.

    The signal that settles it is a **named person**, which is also this
    module's stated contract — it constrains results to named people, and with
    no name it has nothing to constrain on. A behavioral question with no name
    in it ("when did I lie?") is therefore declined too, and answered by the
    ordinary ranker, which returns something imperfect rather than nothing.
    """
    concept = next(
        (
            candidate
            for candidate in CONCEPTS
            if any(re.search(pattern, query, re.IGNORECASE) for pattern in candidate.query_patterns)
        ),
        None,
    )
    if concept is None:
        return None

    focus_terms = _target_from_query(query, concept)
    if not focus_terms:
        return None
    focus_label = " ".join(focus_terms).title()

    q = query.casefold()
    if re.search(r"\b(i|my|myself|we|our)\b", q):
        actor = "user"
    elif focus_terms and (
        re.search(r"\bto\s+me\b", q)
        or min((q.find(term) for term in focus_terms if term in q), default=10_000)
        < _first_behavior_position(q, concept)
    ):
        actor = "target"
    else:
        actor = "unknown"
    return BehaviorIntent(concept, focus_terms, focus_label, actor)


def assess_behavior_evidence(intent: BehaviorIntent, entry: Entry) -> EvidenceAssessment:
    material = "\n".join(part for part in (entry.title, entry.body[:50_000]) if part)
    folded = material.casefold()
    focus_matches = [term for term in intent.focus_terms if term in folded]
    if intent.focus_terms and len(focus_matches) != len(intent.focus_terms):
        return EvidenceAssessment(False)

    headers = _headers(entry.body)
    sender = headers.get("from", "")
    chat = headers.get("chat", "")
    recipient = headers.get("to", "")
    sender_user = _is_me(sender, entry.source_account)
    sender_target = bool(sender and _contains_focus(sender, intent.focus_terms))
    channel_target = _contains_focus(f"{chat} {recipient}", intent.focus_terms)

    first = _matches(intent.concept.first_person_patterns, material)
    second = _matches(intent.concept.second_person_patterns, material)
    contextual = _matches(intent.concept.contextual_patterns, material)
    generic = _matches(intent.concept.evidence_patterns, material)
    focus_actor = _focus_precedes_evidence(intent, folded)

    reasons = []
    if intent.focus_label:
        reasons.append(f"mentions {intent.focus_label}")

    if intent.actor == "user":
        first_is_user = first and (not sender or sender_user)
        contextual_is_user = contextual and (not sender or sender_user)
        if not first_is_user and not contextual_is_user:
            return EvidenceAssessment(False)
        if sender_user:
            reasons.append("written or sent by you")
        if first_is_user:
            reasons.append(f"explicit {intent.concept.label} wording")
            return EvidenceAssessment(True, 1.25, "explicit", tuple(reasons))
        reasons.append("statement contrasted with what actually happened")
        return EvidenceAssessment(True, 1.05, "contextual", tuple(reasons))

    if intent.actor == "target":
        target_admission = first and sender_target
        accusation = second and (sender_user or (channel_target and not sender_target))
        if not target_admission and not accusation and not focus_actor:
            return EvidenceAssessment(False)
        if target_admission:
            reasons.extend(("message from the named person", "explicit first-person wording"))
            return EvidenceAssessment(True, 1.2, "explicit", tuple(reasons))
        if accusation:
            reasons.append("contemporaneous accusation or contradiction")
            return EvidenceAssessment(True, 0.95, "contextual", tuple(reasons))
        reasons.append(f"{intent.focus_label} appears as the actor")
        return EvidenceAssessment(True, 1.05, "explicit", tuple(reasons))

    if first or second or contextual or generic:
        reasons.append(
            "explicit behavioral wording" if first or second or generic else "contextual contrast"
        )
        return EvidenceAssessment(True, 0.9, "contextual", tuple(reasons))
    return EvidenceAssessment(False)


def qualify_behavior_hits(
    intent: BehaviorIntent, hits: list[SearchHit], *, limit: int
) -> list[SearchHit]:
    """Recheck mixed graph/vector candidates against observable evidence."""
    qualified = []
    for hit in hits:
        assessment = assess_behavior_evidence(intent, hit.entry)
        if not assessment.relevant:
            continue
        qualified.append(
            hit.model_copy(
                update={
                    "score": assessment.score + min(hit.score, 0.25),
                    "match_reasons": assessment.reasons,
                    "match_strength": assessment.strength,
                }
            )
        )
    return sorted(
        qualified, key=lambda hit: (hit.score, hit.entry.id), reverse=True
    )[:limit]


def _normalize_token(value: str) -> str:
    token = value.casefold().strip(".'’")
    if token.endswith("'s") or token.endswith("’s"):
        token = token[:-2]
    return token


_PERSONAL_PRONOUN = re.compile(r"\b(?:i|me|my|myself|we|us|our|you|your)\b", re.IGNORECASE)


def _reads_as_a_name(raw: str) -> bool:
    """Could this word be a person, or is it just the next noun in a sentence?

    The test is that the word is not written in lower case. Names are
    capitalised in running text and common nouns are not, which is a weaker
    signal than a contact list but needs no data and no language list. It is
    also the safe direction to be wrong in: a name typed in lower case is
    merely handled by the ordinary ranker, whereas a common noun accepted as a
    person empties the result set. Scripts without letter case — Chinese,
    Japanese, Korean — pass, since `islower()` is false for them and refusing
    them all would be worse than admitting a few common nouns.
    """
    return not raw[:1].islower()


def _positional_target_allowed(query: str) -> bool:
    """Guard for the two patterns that read a target from sentence position.

    "…to X" and "did X …" match anything, so `users` in "avoid sending
    duplicates to users" and `Postgres` in "did Postgres ignore the index
    hint?" both present as people — capitalisation alone cannot separate a
    person from a product. What separates them is that this module searches the
    user's own archive for the user's own dealings: those questions always
    place the user or the addressee in them, and a question about two third
    parties reaches the ontology through a verb-anchored pattern instead
    ("when Sandra lied to Marta"), which does not need this guard because the
    verb has already established that the next word is a person.
    """
    return bool(_PERSONAL_PRONOUN.search(query))


def _target_from_query(
    query: str, concept: BehaviorConcept
) -> tuple[str, ...]:
    quoted = re.search(r"""["“](?P<name>[^"”]{2,60})["”]""", query)
    if quoted:
        return tuple(
            _normalize_token(token) for token in TOKEN_RE.findall(quoted.group("name"))
        )

    behavior_words = {
        _normalize_token(token)
        for term in (*concept.fts_terms, concept.rewrite)
        for token in TOKEN_RE.findall(term)
    }
    positional = _positional_target_allowed(query)
    preposition_targets = []
    for match in re.finditer(
        r"\b(?:to|with|from|against)\s+(?:my\s+)?(?P<name>[^\W\d_][\w'’.-]*)",
        query,
        re.IGNORECASE,
    ):
        raw = match.group("name")
        target = _normalize_token(raw)
        if target in QUERY_STOP or target in behavior_words:
            continue
        if positional and _reads_as_a_name(raw):
            preposition_targets.append(target)
    if preposition_targets:
        return (preposition_targets[-1],)

    possessive = re.search(
        r"\b(?P<name>[^\W\d_][\w'’.-]*)['’]s\s+(?:boundary|trust|confidence)\b",
        query,
        re.IGNORECASE,
    )
    if possessive:
        return (_normalize_token(possessive.group("name")),)

    did_actor = re.search(
        r"\bdid\s+(?P<name>[^\W\d_][\w'’.-]*)\s+",
        query,
        re.IGNORECASE,
    )
    if did_actor:
        raw = did_actor.group("name")
        target = _normalize_token(raw)
        if target not in QUERY_STOP and positional and _reads_as_a_name(raw):
            return (target,)

    object_patterns = {
        "deception": r"\b(?:lied to|deceived|misled|betrayed)\s+(?P<name>[\w'’.-]+)",
        "apology": r"\b(?:apologized to|apologised to)\s+(?P<name>[\w'’.-]+)",
        "avoidance": r"\b(?:ghosted|ignored|avoided)\s+(?P<name>[\w'’.-]+)",
        "conflict": r"\b(?:argued with|fought with)\s+(?P<name>[\w'’.-]+)",
        "support": r"\b(?:supported|helped)\s+(?P<name>[\w'’.-]+)",
        "promise": r"\b(?:promised)\s+(?P<name>[\w'’.-]+)",
        "betrayal": r"\b(?:betray(?:ed)?)\s+(?P<name>[\w'’.-]+)",
    }
    pattern = object_patterns.get(concept.key)
    match = re.search(pattern, query, re.IGNORECASE) if pattern else None
    if match:
        target = _normalize_token(match.group("name"))
        if target not in QUERY_STOP:
            return (target,)
    return ()


def _first_behavior_position(query: str, concept: BehaviorConcept) -> int:
    positions = []
    for pattern in concept.query_patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            positions.append(match.start())
    return min(positions, default=10_000)


def _matches(patterns: tuple[str, ...], material: str) -> bool:
    return any(re.search(pattern, material, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def _headers(body: str) -> dict[str, str]:
    head, separator, _ = body.partition("\n\n")
    if not separator:
        return {}
    fields = {}
    for line in head.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip()
    return fields


def _is_me(value: str, source_account: str = "") -> bool:
    folded = value.casefold().strip()
    account = source_account.casefold().strip()
    return folded == "me" or bool(account and account in folded)


def _contains_focus(material: str, focus_terms: tuple[str, ...]) -> bool:
    folded = material.casefold()
    return bool(focus_terms) and all(term in folded for term in focus_terms)


def _focus_precedes_evidence(intent: BehaviorIntent, material: str) -> bool:
    if not intent.focus_terms:
        return False
    focus_pattern = r"\b(?:" + "|".join(re.escape(term) for term in intent.focus_terms) + r")\b"
    evidence = "|".join(f"(?:{pattern})" for pattern in intent.concept.evidence_patterns)
    return bool(re.search(f"{focus_pattern}.{{0,80}}(?:{evidence})", material, re.IGNORECASE))
