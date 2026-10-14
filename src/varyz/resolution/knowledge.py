"""Append-only explicit owner knowledge and the tier-one resolver."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.personal_model.query import (
    AnswerBasis,
    LifeQueryResult,
    LifeRelation,
    QueryDisposition,
    QueryProvenance,
    TypedAnswer,
    WithheldRecordMarker,
)
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.scopes import GLOBAL_SCOPE, normalize_scope, stored_scope

# The local owner: the author of every answer given on this install, and the
# recipient of every question unless one is routed to someone else.
OWNER_ID = "owner:local"
# Where an answer came from, in resolution precedence: the owner's own answer
# always wins over an organisation's, which wins over a creator's published taste.
ORIGINS = ("own", "org", "published")
# Scopes of answers that are not the owner's own carry their origin as a prefix:
# 'org:<id>' and 'published:<id>'.
_ORIGIN_PREFIXES = {"org": "org:", "published": "published:"}
# Tier one cites the knowledge entry it answered from under this generation id.
KNOWLEDGE_GENERATION = "owner_knowledge"


def fold_stored_scopes(conn: sqlite3.Connection) -> None:
    """Let SQL compare stored scopes the way they are read: varyz_scope(scope) = ?"""
    conn.create_function("varyz_scope", 1, stored_scope, deterministic=True)


def scope_origin(scope: str) -> str:
    """The origin a normalised scope implies: 'org', 'published' or 'own'."""
    for origin, prefix in _ORIGIN_PREFIXES.items():
        if scope.startswith(prefix):
            return origin
    return "own"


def effective_origin(origin: str, scope: str) -> str:
    """The more foreign of a row's origin and its scope's: never promote to 'own'."""
    implied = scope_origin(scope)
    known = origin if origin in ORIGINS else "own"
    return max(known, implied, key=ORIGINS.index)


def scope_applies(scope: str, project: str | None, text: str) -> bool:
    """A general belief applies anywhere; a project's one-off choice only there.

    The project is the one the agent is working in, or one the question names:
    "what is persona's accent colour?" asked from another folder still means persona.
    Answers from an organisation or a published creator are not the owner's way
    of working: they never apply here and are reached only through tier one's
    fallback after the owner's own answers.
    """
    if scope_origin(scope) != "own":
        return False
    if scope == GLOBAL_SCOPE:
        return True
    if project is not None and normalize_scope(project) == scope:
        return True
    lowered = unicodedata.normalize("NFKC", text).casefold()
    return re.search(rf"(?<![\w-]){re.escape(scope)}(?![\w-])", lowered) is not None


def normalize_question(question: str) -> str:
    """Return a conservative identity for equivalent owner questions."""
    normalized = unicodedata.normalize("NFKC", question).casefold().strip()
    normalized = " ".join(normalized.split())
    return normalized.rstrip("?!.").rstrip()


_STOPWORDS = frozenset(re.findall(
    r"\w+",
    "a an and are as at be been being by can could did do does egor for from has have how i "
    "in into is it its me my of on or our should the their there these this to us was were "
    "what when where which who why will with would you your explicitly please tell "
    "agent agents claude varys varyz "
    # Intent, modality and deixis: they frame a question without scoping the decision.
    "owner owners want wants wanted like likes liked prefer prefers preferred preference "
    "okay ok allowed allow may might must need needs able normally usually generally "
    "currently current now right moment use uses used using through about via any some "
    "also just really process handle make get put go take new many more much during "
    "write follow choose chose chosen "
    # Framing that asks about a decision without narrowing it.
    "add fine acceptable another see give work built first tool assume appear policy",
))

# A deliberately small, general vocabulary map applied before stemming. Each entry
# is a spelling, abbreviation or strict hyponym, never a judgment about the owner.
_SYNONYMS = {
    "db": "database", "dbs": "database", "auth": "authentication", "repo": "repository",
    "repos": "repository", "saturday": "weekend", "saturdays": "weekend",
    "sunday": "weekend", "sundays": "weekend", "weekends": "weekend", "remain": "stay",
    "keep": "stay", "retain": "stay", "backwards": "backward", "deploy": "host",
    "deployed": "host",
    "hosted": "host", "hosting": "host", "colour": "color", "colours": "color",
    "sent": "send", "sending": "send", "app": "application", "apps": "application",
    "doc": "documentation", "docs": "documentation", "site": "website", "sites": "website",
    "top": "highest", "cost": "price", "costs": "price", "licence": "license",
    "established": "existing",
    "deployment": "host",
}
# Multi-word spellings of one term, joined before tokenising.
_COMPOUNDS = (
    (re.compile(r"\bself[\s-]?host(?:ed|ing)?\b"), "selfhost"),
    (re.compile(r"\bweekdays?\b"), "day week"),
    # Narrow decision phrases: a background hue is not an accent.
    (re.compile(r"\b(?:highlight (?:hue|color|colour)|accent (?:hue|shade))\b"),
     "accent color"),
    (re.compile(r"\btime[\s-]+zones?\b"), "timezone"),
    (re.compile(r"\bcommand[\s-]+line(?:\s+(?:tool|interface))?\b"), "cli"),
)

# Timing and rationale are not recorded by a stored choice.
_UNANSWERABLE_STARTS = frozenset({"why", "when"})

# Nouns that, straight after the project's name, still name the project itself:
# "the lumen page", "the varyz repo".
_PROJECT_REFERENTS = "page|site|website|app|project|repo|repository|codebase|product"


def without_project_name(question: str, project: str | None) -> str:
    """The question with the name of the project it is asked in taken out.

    The request already says which project it is in, so the name in the question
    is context, not subject. Counted as a subject term it had to be explained by
    a decision that never repeats it ("Which accent colour should the lumen page
    use?" missed "Which accent colour?" scoped to lumen), and it made any archive
    that mentions its own project look as if it knew the subject.
    """
    name = (project or "").strip()
    if not name:
        return question
    pattern = re.compile(
        rf"(?<![\w-]){re.escape(name)}(?:['’]s)?"
        rf"(?:\s+(?:{_PROJECT_REFERENTS})\b)?(?![\w-])",
        re.IGNORECASE,
    )
    value = pattern.sub(" ", unicodedata.normalize("NFKC", question))
    value = re.sub(r"\b(?:this|the|our) (?:product|project)\b", " ", value, flags=re.I)
    return re.sub(r"\bbelongs? (?:on|to)\b", " ", value, flags=re.I)


def _stem(word: str) -> str:
    word = _SYNONYMS.get(word, word)
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif (len(word) > 6 and word.endswith("er")) or (
        len(word) > 4 and word.endswith("ed") and not word.endswith("eed")
    ):
        word = word[:-2]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    # Seven characters join inflections (compatible/compatibility, prioritise/priority).
    return word[:7]


def _casefolded(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    # The OS name is not the plural of a UI window. Ordinary plural stemming
    # otherwise offered installer-directory repairs for terminal-window tasks.
    value = re.sub(r"\bWindows\b", "windowsos", value).casefold()
    for pattern, replacement in _COMPOUNDS:
        value = pattern.sub(replacement, value)
    return value


def _terms(value: str) -> set[str]:
    """Small lexical fallback, not semantic inference or a substitute for the owner."""
    words = re.findall(r"[^\W_]+", _casefolded(value))
    words = [_SYNONYMS.get(word, word) for word in words]
    terms = {
        _stem(word) for word in words if word not in _STOPWORDS and len(word) >= 3
    }
    # A named brand palette and named typefaces explicitly provide these axes.
    # This is literal typography/palette evidence, not a guessed owner taste.
    if "brand" in words and re.search(r"#[0-9a-f]{6}\b", value, re.I):
        terms.add("color")
    if "brand" in words and {"serif", "sans"} & set(words):
        terms.add("font")
    return terms


def _endorsed_projection(query_terms: set[str], question: str, answer: str) -> bool:
    """Allow a literal subchoice of an explicitly endorsed implementation.

    Enumerated framework/asset names need not all appear in a shorter question.
    Conditional approval, publication, spending and other permission records
    are excluded; all salient requested terms still need literal support.
    """
    value = re.sub(r"^just for [^:]{1,100}:\s*", "", answer.casefold())
    endorsed = re.match(
        r"^(?:use (?:this|(?:the|its) existing) (?:approach|stack)|"
        r"keep the (?:established|existing) brand)",
        value,
    )
    permission_terms = {
        _stem(word) for word in (
            "approve", "permission", "authorize", "permit", "commit", "push",
            "publish", "spend", "pay", "delete", "upload",
        )
    }
    if not endorsed or len(query_terms) < 2 or query_terms & permission_terms:
        return False
    return not re.search(
        r"\b(?:approve|permission|commit|push|publish\w*|spend|pay|delete|upload\w*|"
        r"unless|provided|if|except)\b", question, re.I,
    )


def _attribute_candidate(named: set[str], question: str, answer: str) -> bool:
    """Offer a scoped attribute to the local verifier, never answer it directly.

    A question about which model was chosen need not repeat the model's name.
    Require two literal topic anchors besides the attribute itself, and exclude
    conditional/permission records. The verifier still must accept exactly one.
    """
    attributes = {"model", "font", "color"}
    shared = named & _terms(question + " " + answer)
    if not shared & attributes:
        return False
    if len(shared - attributes - {"exist", "stack", "brand", "prefer"}) < 2:
        return False
    return not re.search(
        r"\b(?:approve|permission|commit|push|publish\w*|spend|pay|delete|upload\w*|"
        r"unless|provided|if|except)\b", question, re.I,
    )


def _topic_candidate(named: set[str], question: str, answer: str) -> bool:
    """Broader retrieval eligibility only; a model must verify the full pair.

    Two independent literal topic anchors are needed beyond generic attribute
    or implementation vocabulary. Neither permission queries nor conditional
    permissions enter this lane. This improves recall of choices whose long
    original question enumerates incidental platform names.
    """
    forbidden = {_stem(w) for w in (
        "approve", "permission", "authorize", "permit", "commit", "push", "publish",
        "spend", "pay", "delete", "upload",
    )}
    if named & forbidden:
        return False
    shared = named & _terms(question + " " + answer)
    generic = {"exist", "model", "font", "color", "brand", "stack", "approac", "prefer"}
    if len(shared - generic) < 2:
        return False
    return not re.search(r"\b(?:approve|permission|unless|provided|if|except)\b", question, re.I)


def _entities(value: str) -> set[str]:
    """Capitalised names after the first word: projects, products and platforms."""
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value))
    return {
        _stem("windowsos" if word == "Windows" else word.casefold()) for word in words[1:]
        if word[:1].isupper() and not word.isupper() and word.casefold() not in _STOPWORDS
    }


def _alternatives(question: str) -> tuple[tuple[str, str], ...]:
    """Stemmed option pairs from "X or Y": a decision names the option it chose."""
    pairs = re.findall(r"([^\W_]+)\s+or\s+([^\W_]+)", _casefolded(question))
    return tuple((_stem(left), _stem(right)) for left, right in pairs)


def _paraphrase_score(
    query_terms: set[str], stored_question: str, stored_answer: str,
    alternatives: tuple[tuple[str, str], ...] = (),
    context_terms: frozenset[str] = frozenset(),
) -> float | None:
    """Score a stored decision only when it explains every salient query term.

    ``context_terms`` are the terms of the project the question is asked in,
    already removed from ``query_terms``: they need no explaining, but a stored
    question that names the project is still about it.
    """
    stored = stored_question + " " + stored_answer
    unexplained = query_terms - _terms(stored)
    if alternatives and unexplained:
        # Short options such as "uv" are not salient terms but still name the choice.
        named = {_stem(word) for word in re.findall(r"[^\W_]+", _casefolded(stored))}
        for left, right in alternatives:
            if left in named or right in named:
                unexplained -= {left, right}
    if not query_terms or unexplained:
        return None
    named = query_terms | context_terms
    stored_terms = _terms(stored_question)
    # One term matches too much -- unless it is the whole stored question too:
    # "Which analytics should lumen use?" asks exactly "Which analytics?".
    if len(query_terms) < 2 and query_terms != stored_terms - context_terms:
        return None
    # A decision scoped to a named project does not answer for an unnamed one.
    projected = not _entities(stored_question) <= named
    if projected and not _endorsed_projection(
        query_terms, stored_question, stored_answer,
    ):
        return None
    if not stored_terms:
        return None
    specificity = len(stored_terms & named) / len(stored_terms)
    # A match through the stored answer alone needs more of the question explained.
    if specificity == 0 and len(query_terms) < 3:
        return None
    # Existing strict matches outrank the new subchoice fallback. A broad
    # endorsed plan must not turn a previously unambiguous answer into a tie.
    return specificity - 0.5 if projected else specificity + 0.5


def decision_score(question: str, stored_question: str, answer: str,
                   project: str | None = None) -> float | None:
    """Conservative equivalent-choice match shared by local and hosted surfaces."""
    if normalize_question(question).split(" ", 1)[0] in _UNANSWERABLE_STARTS:
        return None
    return _paraphrase_score(
        _terms(without_project_name(question, project)), stored_question, answer,
        _alternatives(question), frozenset(_terms(project)) if project else frozenset(),
    )


# Current beliefs: neither superseded by a correction nor retracted by the owner.
_CURRENT = """knowledge.id NOT IN (SELECT supersedes_id FROM owner_knowledge
                                   WHERE supersedes_id IS NOT NULL)
              AND knowledge.id NOT IN (SELECT knowledge_id
                                       FROM owner_knowledge_retractions)"""

# The SQL twin of effective_origin: a row's origin, or its scope's if more foreign.
_ORIGIN = """CASE WHEN knowledge.origin = 'published'
                    OR substr(knowledge.scope, 1, 10) = 'published:' THEN 'published'
               WHEN knowledge.origin = 'org'
                    OR substr(knowledge.scope, 1, 4) = 'org:' THEN 'org'
               ELSE 'own' END"""


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@contextmanager
def _write_transaction(conn: sqlite3.Connection, lock: threading.RLock) -> Iterator[None]:
    """Own a transaction only when the caller has not already opened one."""
    with lock:
        owned = not conn.in_transaction
        if owned:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if owned:
                conn.rollback()
            raise
        else:
            if owned:
                conn.commit()


def _next_monotonic_seq(conn: sqlite3.Connection, device_id: str) -> int:
    if not device_id.strip():
        raise ValueError("device_id cannot be blank")
    conn.execute(
        """INSERT INTO resolution_device_sequences(device_id, last_seq)
           VALUES (?, 1)
           ON CONFLICT(device_id) DO UPDATE SET last_seq = last_seq + 1""",
        (device_id,),
    )
    row = conn.execute(
        "SELECT last_seq FROM resolution_device_sequences WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - protected by the statement above
        raise RuntimeError("failed to allocate resolution sequence")
    return int(row[0])


def owner_knowledge_denial(conn: sqlite3.Connection, caller: Caller) -> str | None:
    """Owner knowledge reaches only callers whose capability covers it."""
    from varyz.trust import AgentAuthenticationError, capability_limits

    try:
        max_items, max_chars = capability_limits(conn, caller)
    except AgentAuthenticationError:
        return "caller_capability_denied"
    if max_items <= 0 or max_chars <= 0:
        return "caller_capability_denied"
    if caller.capability_id is not None:
        row = conn.execute(
            "SELECT resource_types_json FROM agent_capabilities WHERE id = ?",
            (caller.capability_id,),
        ).fetchone()
        if row is None or "owner_knowledge" not in json.loads(row[0]):
            return "caller_capability_denied"
    return None


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    question: str
    normalized_question: str
    answer: str
    basis: AnswerBasis
    source: str
    provenance: Mapping[str, Any]
    caller: Caller
    device_id: str
    monotonic_seq: int
    supersedes_id: str | None
    escalation_id: str | None
    stated_at: datetime
    created_at: datetime
    scope: str = GLOBAL_SCOPE
    author_id: str = OWNER_ID
    origin: str = "own"

    @property
    def general(self) -> bool:
        """How the owner works everywhere, rather than a choice for one product."""
        return self.scope == GLOBAL_SCOPE

    @property
    def own(self) -> bool:
        """The owner's own answer, rather than an organisation's or published taste."""
        return self.origin == "own"

    def to_dict(self) -> dict[str, Any]:
        """Serialize a self-contained future sync payload."""
        value = asdict(self)
        value["basis"] = self.basis.value
        value["caller"] = self.caller.model_dump(mode="json")
        value["stated_at"] = _iso(self.stated_at)
        value["created_at"] = _iso(self.created_at)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> KnowledgeEntry:
        """Interpret a serialized row without access to its originating database."""
        scope = normalize_scope(str(value.get("scope") or GLOBAL_SCOPE))
        return cls(
            id=str(value["id"]),
            question=str(value["question"]),
            normalized_question=str(value["normalized_question"]),
            answer=str(value["answer"]),
            basis=AnswerBasis(value["basis"]),
            source=str(value["source"]),
            provenance=dict(value["provenance"]),
            caller=Caller.model_validate(value["caller"]),
            device_id=str(value["device_id"]),
            monotonic_seq=int(value["monotonic_seq"]),
            supersedes_id=(
                str(value["supersedes_id"])
                if value.get("supersedes_id") is not None
                else None
            ),
            escalation_id=(
                str(value["escalation_id"])
                if value.get("escalation_id") is not None
                else None
            ),
            stated_at=_datetime(str(value["stated_at"])),
            created_at=_datetime(str(value["created_at"])),
            scope=scope,
            # Payloads serialized before authorship existed were the local owner's.
            author_id=str(value.get("author_id") or OWNER_ID),
            origin=effective_origin(str(value.get("origin") or "own"), scope),
        )


class SQLiteKnowledgeStore:
    """SQLite implementation of the frozen ``KnowledgeStore`` protocol."""

    tier = 1

    def __init__(
        self, conn: sqlite3.Connection, *, clock: Any = utc_now, verifier: Any = None
    ) -> None:
        self._conn = conn
        fold_stored_scopes(conn)
        self._clock = clock
        self._lock = threading.RLock()
        # ``paraphrase_verify.ParaphraseVerifier``: a second look, with the local
        # model, at a reworded question the strict matcher found nothing for.
        self._verifier = verifier

    def remember(
        self,
        *,
        question: str,
        answer: str,
        caller: Caller,
        basis: AnswerBasis,
        source: str,
        device_id: str,
        supersedes_id: str | None = None,
        escalation_id: str | None = None,
        scope: str | None = GLOBAL_SCOPE,
        author_id: str = OWNER_ID,
        origin: str = "own",
    ) -> str:
        """Append a belief. ``scope`` is '*' for a general preference (the owner's way
        of working) or a project name for a one-off choice that binds only there.

        ``author_id`` is who gave the answer and ``origin`` where it came from: the
        owner's ``own``, an ``org`` answer scoped 'org:<id>', or ``published`` taste
        scoped 'published:<id>'. Superseding an entry records every resolution it
        answered as ``overridden`` in the same transaction.
        """
        scope = normalize_scope(scope)
        question = question.strip()
        answer = answer.strip()
        source = source.strip()
        author_id = author_id.strip()
        if not question or not answer or not source:
            raise ValueError("question, answer and source cannot be blank")
        if not author_id:
            raise ValueError("author_id cannot be blank")
        if basis not in {AnswerBasis.EXPLICIT, AnswerBasis.USER_CONFIRMED}:
            raise ValueError("explicit knowledge must be explicit or user-confirmed")
        if origin not in ORIGINS:
            raise ValueError(f"origin must be one of {list(ORIGINS)}")
        if scope_origin(scope) != origin:
            raise ValueError(
                "origin and scope disagree: 'own' answers are scoped '*' or a project, "
                "'org' answers 'org:<id>', 'published' answers 'published:<id>'"
            )
        entry_id = new_id()
        now = self._clock()
        with _write_transaction(self._conn, self._lock):
            if supersedes_id is not None:
                previous = self._conn.execute(
                    "SELECT * FROM owner_knowledge WHERE id = ?", (supersedes_id,)
                ).fetchone()
                if previous is None:
                    raise KeyError(f"unknown knowledge entry: {supersedes_id}")
                replaced = effective_origin(_row_origin(previous), _row_scope(previous))
                if ORIGINS.index(origin) > ORIGINS.index(replaced):
                    # Only the owner corrects the owner; imported taste adds, never replaces.
                    raise ValueError(f"an '{origin}' answer cannot supersede an '{replaced}' one")
            seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """INSERT INTO owner_knowledge
                   (id, question, normalized_question, answer, basis, source,
                    provenance_json, caller_agent_id, caller_display_name,
                    caller_purpose, caller_capability_id, caller_device_id, caller_transport,
                    device_id, monotonic_seq, supersedes_id, escalation_id,
                    stated_at, created_at, scope, author_id, origin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?)""",
                (
                    entry_id,
                    question,
                    normalize_question(question),
                    answer,
                    basis.value,
                    source,
                    json.dumps(
                        {"source": source, "escalation_id": escalation_id, "scope": scope},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    caller.agent_id,
                    caller.display_name,
                    caller.purpose,
                    caller.capability_id,
                    caller.device_id,
                    caller.transport,
                    device_id,
                    seq,
                    supersedes_id,
                    escalation_id,
                    _iso(now),
                    _iso(now),
                    scope,
                    author_id,
                    origin,
                ),
            )
            if supersedes_id is not None:
                from varyz.resolution.outcomes import record_supersession

                record_supersession(
                    self._conn,
                    supersedes_id,
                    knowledge_id=entry_id,
                    device_id=device_id,
                    recorded_at=now,
                )
        return entry_id

    def try_resolve(self, request: ResolutionRequest) -> Resolution | None:
        denial = self._caller_denial(request.caller)
        if denial is not None:
            return self._withheld(denial)
        project = request.project

        def origin_of(candidate: sqlite3.Row) -> str:
            return effective_origin(_row_origin(candidate), _row_scope(candidate))

        def applies(candidate: sqlite3.Row) -> bool:
            if origin_of(candidate) != "own":
                # An organisation's answer or published taste is general by nature.
                return True
            return scope_applies(_row_scope(candidate), project, request.question)

        exact = [
            candidate for candidate in self._conn.execute(
                f"""SELECT knowledge.*
                   FROM owner_knowledge AS knowledge
                   WHERE (knowledge.question = ? OR knowledge.normalized_question = ?)
                     AND {_CURRENT}
                   ORDER BY CASE WHEN knowledge.question = ? THEN 0 ELSE 1 END,
                            knowledge.monotonic_seq DESC,
                            knowledge.created_at DESC""",
                (request.question, normalize_question(request.question), request.question),
            ).fetchall()
            if applies(candidate)
        ]
        # This project's own choice outranks the owner's general default.
        exact.sort(key=lambda candidate: _row_scope(candidate) == GLOBAL_SCOPE)
        question_start = normalize_question(request.question).split(" ", 1)[0]
        # Own project, own general, org, published: an exact or paraphrased answer
        # of the owner's own always wins over anyone else's, even an exact one.
        for origin in ORIGINS:
            row = next((item for item in exact if origin_of(item) == origin), None)
            paraphrased = row is None
            verified = False
            if row is None:
                if question_start in _UNANSWERABLE_STARTS:
                    continue
                matches = self._paraphrase_matches(
                    request.question, origin, applies, project
                )
                # Two plausible answers are a question for the owner, not a guess,
                # and never a reason to fall through to someone else's taste.
                if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.2:
                    plausible = [r for score, r in matches if matches[0][0] - score < 0.2]
                    if len({" ".join(r["answer"].split()) for r in plausible}) > 1:
                        return None
                if matches:
                    row = matches[0][1]
                elif origin == "own" and self._verifier is not None:
                    row = self._verified_match(request.question, applies, project)
                    verified = row is not None
                if row is None:
                    continue
            entry = self._entry(row)
            if len(entry.answer) > self._max_chars(request.caller):
                # A truncated owner decision can invert its meaning; withhold it whole.
                return self._withheld("answer_exceeds_caller_limit")
            confidence = 1.0 if not paraphrased else 0.7 if verified else 0.8
            return knowledge_resolution(entry, confidence=confidence)
        return None

    def _verified_match(
        self, question: str, applies: Callable[[sqlite3.Row], bool], project: str | None = None,
    ) -> sqlite3.Row | None:
        """The owner's one decision the verifier says answers ``question``, or None."""
        named = _terms(question) | _entities(question)
        rows = {
            row["id"]: row
            for row in self._conn.execute(
                f"""SELECT knowledge.* FROM owner_knowledge AS knowledge
                   WHERE {_CURRENT} AND {_ORIGIN} = 'own'
                   ORDER BY knowledge.created_at DESC LIMIT 500"""
            ).fetchall()
            # A decision naming a project or product the question does not name
            # never answers it, however close the wording.
            if applies(row) and (named & {_stem("selfhost"), "window"}) <= _terms(
                row["question"] + " " + row["answer"]
            ) and _entities(question) <= (
                _terms(row["question"] + " " + row["answer"]) | _terms(project or "")
            ) and (
                _entities(row["question"]) <= named
                or _endorsed_projection(named, row["question"], row["answer"])
                or _attribute_candidate(named, row["question"], row["answer"])
                or _topic_candidate(named, row["question"], row["answer"])
            )
        }
        # Exhaust the existing narrow lane before considering new subchoice
        # candidates. Broader retrieval must not displace a clear narrow match,
        # or resolve a conflict that the narrow lane already identified.
        narrow = {key: row for key, row in rows.items() if _entities(row["question"]) <= named}
        if narrow:
            selected = self._verified_rows(question, narrow)
            if selected is not None or any(
                p >= getattr(self._verifier, "threshold", 0.9)
                for _, p in getattr(self._verifier, "last", [])
            ):
                return selected
        return self._verified_rows(question, rows)

    def _verified_rows(self, question: str, rows: dict[str, sqlite3.Row]) -> sqlite3.Row | None:
        chosen = self._verifier.pick(
            question, [(key, row["question"], row["answer"]) for key, row in rows.items()]
        )
        # The same project-over-general precedence used by the strict matcher
        # also applies after semantic verification. A general default must not
        # make an explicitly confirmed project choice look like a conflict.
        threshold = getattr(self._verifier, "threshold", 0.9)
        specific = [
            rows[key] for key, probability in getattr(self._verifier, "last", [])
            if probability >= threshold and key in rows and _row_scope(rows[key]) != GLOBAL_SCOPE
        ]
        if specific:
            from varyz.resolution.paraphrase_verify import consistent_verified_answer

            key = consistent_verified_answer(
                [row["id"] for row in specific],
                {row["id"]: row["answer"] for row in specific},
            )
            return rows.get(key) if key is not None else None
        return rows.get(chosen) if chosen is not None else None

    def _paraphrase_matches(
        self, question: str, origin: str, applies: Callable[[sqlite3.Row], bool],
        project: str | None = None,
    ) -> list[tuple[float, sqlite3.Row]]:
        """Paraphrased answers of one origin, best first; project choices over general."""
        context_terms = frozenset(_terms(project)) if project else frozenset()
        query_terms = _terms(without_project_name(question, project))
        alternatives = _alternatives(question)
        rows = self._conn.execute(
            f"""SELECT knowledge.* FROM owner_knowledge AS knowledge
               WHERE {_CURRENT} AND {_ORIGIN} = ?
               ORDER BY knowledge.created_at DESC LIMIT 500""",
            (origin,),
        ).fetchall()
        matches = []
        for candidate in rows:
            if not applies(candidate):
                continue
            score = _paraphrase_score(
                query_terms, candidate["question"], candidate["answer"], alternatives,
                context_terms,
            )
            if score is not None:
                matches.append((score, candidate))
        specific = [item for item in matches if _row_scope(item[1]) != GLOBAL_SCOPE]
        if origin == "own" and specific:
            matches = specific
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches

    def retract(
        self, entry_id: str, *, caller: Caller, device_id: str, reason: str = ""
    ) -> str:
        """Withdraw a belief from resolution; the original row stays for audit."""
        retraction_id = new_id()
        now = self._clock()
        with _write_transaction(self._conn, self._lock):
            if self._conn.execute(
                "SELECT 1 FROM owner_knowledge WHERE id = ?", (entry_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown knowledge entry: {entry_id}")
            if self._conn.execute(
                "SELECT 1 FROM owner_knowledge_retractions WHERE knowledge_id = ?",
                (entry_id,),
            ).fetchone() is not None:
                raise ValueError(f"knowledge entry already retracted: {entry_id}")
            seq = _next_monotonic_seq(self._conn, device_id)
            self._conn.execute(
                """INSERT INTO owner_knowledge_retractions
                   (id, knowledge_id, reason, caller_agent_id, caller_display_name,
                    caller_purpose, caller_capability_id, caller_device_id,
                    caller_transport, device_id, monotonic_seq, retracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    retraction_id, entry_id, reason.strip(), caller.agent_id,
                    caller.display_name, caller.purpose, caller.capability_id,
                    caller.device_id, caller.transport, device_id, seq, _iso(now),
                ),
            )
        return retraction_id

    def current(self) -> tuple[KnowledgeEntry, ...]:
        """Every belief resolution may use, newest first: the Mirror's view."""
        rows = self._conn.execute(
            f"""SELECT knowledge.* FROM owner_knowledge AS knowledge WHERE {_CURRENT}
                ORDER BY knowledge.created_at DESC, knowledge.monotonic_seq DESC"""
        ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def _caller_denial(self, caller: Caller) -> str | None:
        return owner_knowledge_denial(self._conn, caller)

    def _max_chars(self, caller: Caller) -> int:
        from varyz.trust import capability_limits

        return capability_limits(self._conn, caller)[1]

    def _withheld(self, reason: str) -> Resolution:
        return Resolution(
            disposition=QueryDisposition.NOT_KNOWN,
            basis=None,
            answer=None,
            tier=1,
            confidence=1.0,
            withheld=(
                WithheldRecordMarker(
                    record_kind="owner_knowledge", reason=reason, rule_version="alpha-local-v1"
                ),
            ),
            resolved_at=self._clock(),
            monotonic_seq=0,
        )

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        row = self._conn.execute(
            "SELECT * FROM owner_knowledge WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._entry(row) if row is not None else None

    def history(self, question: str) -> tuple[KnowledgeEntry, ...]:
        rows = self._conn.execute(
            """SELECT * FROM owner_knowledge
               WHERE normalized_question = ?
               ORDER BY created_at, device_id, monotonic_seq""",
            (normalize_question(question),),
        ).fetchall()
        return tuple(self._entry(row) for row in rows)

    @staticmethod
    def _entry(row: sqlite3.Row) -> KnowledgeEntry:
        caller = Caller(
            agent_id=row["caller_agent_id"],
            display_name=row["caller_display_name"],
            purpose=row["caller_purpose"],
            capability_id=row["caller_capability_id"],
            device_id=row["caller_device_id"],
            transport=row["caller_transport"],
        )
        return KnowledgeEntry(
            id=row["id"],
            question=row["question"],
            normalized_question=row["normalized_question"],
            answer=row["answer"],
            basis=AnswerBasis(row["basis"]),
            source=row["source"],
            provenance=json.loads(row["provenance_json"]),
            caller=caller,
            device_id=row["device_id"],
            monotonic_seq=int(row["monotonic_seq"]),
            supersedes_id=row["supersedes_id"],
            escalation_id=row["escalation_id"],
            stated_at=_datetime(row["stated_at"]),
            created_at=_datetime(row["created_at"]),
            scope=_row_scope(row),
            author_id=_row_author(row),
            origin=effective_origin(_row_origin(row), _row_scope(row)),
        )


def _row_scope(row: sqlite3.Row) -> str:
    # Rows read before migration 0037 have no scope column: they were general.
    try:
        return stored_scope(row["scope"])
    except (IndexError, KeyError):
        return GLOBAL_SCOPE


def _row_author(row: sqlite3.Row) -> str:
    # Rows read before migration 0038 have no author: the local owner answered them.
    try:
        return str(row["author_id"] or OWNER_ID)
    except (IndexError, KeyError):
        return OWNER_ID


def _row_origin(row: sqlite3.Row) -> str:
    # Rows read before migration 0038 have no origin: they were the owner's own.
    try:
        return str(row["origin"] or "own")
    except (IndexError, KeyError):
        return "own"


def _scope_kind(entry: KnowledgeEntry) -> str:
    if entry.origin != "own":
        return entry.origin
    return "general" if entry.general else "project"


def knowledge_resolution(
    entry: KnowledgeEntry, *, confidence: float, escalation_id: str | None = None
) -> Resolution:
    """A tier-one answer from one knowledge entry, citing the entry and its author.

    The citation (generation ``owner_knowledge``, record the entry id) is what the
    resolution ledger stores, so a later supersession can find the resolutions
    this entry answered. An answer that is not the owner's own is ``reported``,
    never the owner's explicit or confirmed word.
    """
    basis = entry.basis if entry.own else AnswerBasis.REPORTED
    cited = TypedAnswer(
        relation=LifeRelation.PREFERENCE,
        record_kind="owner_knowledge",
        subject_entity_ids=(entry.author_id,),
        predicate_or_concept=None,
        value_kind="text",
        display_value=entry.answer,
        scope_kind=_scope_kind(entry),
        scope_label=entry.scope,
        assertion_kind=entry.origin,
        basis=basis,
        status="active",
        confidence=confidence,
        readiness="ready",
        stated_at=entry.stated_at,
        sensitivity="personal",
        provenance=QueryProvenance(generation_id=KNOWLEDGE_GENERATION, record_id=entry.id),
    )
    return Resolution(
        disposition=QueryDisposition.ANSWERED,
        basis=basis,
        answer=entry.answer,
        tier=1,
        confidence=confidence,
        life_result=LifeQueryResult(
            disposition=QueryDisposition.ANSWERED, generation_id=None, answers=(cited,)
        ),
        escalation_id=escalation_id if escalation_id is not None else entry.escalation_id,
        resolved_at=entry.stated_at,
        monotonic_seq=entry.monotonic_seq,
    )


def knowledge_provenance(result: Resolution) -> dict[str, Any] | None:
    """Who a tier-one answer came from, or None when no knowledge entry answered.

    An answer that is not the owner's own carries a label saying so, for agents
    to repeat rather than present someone else's taste as the owner's.
    """
    if result.tier != 1 or result.answer is None or result.life_result is None:
        return None
    for answer in result.life_result.answers:
        if answer.provenance.generation_id != KNOWLEDGE_GENERATION:
            continue
        origin = answer.assertion_kind or "own"
        scope = answer.scope_label or GLOBAL_SCOPE
        provenance: dict[str, Any] = {
            "knowledge_id": answer.provenance.record_id,
            "author_id": answer.subject_entity_ids[0] if answer.subject_entity_ids else OWNER_ID,
            "origin": origin,
            "scope": scope,
        }
        if origin == "org":
            provenance["label"] = f"Organisation answer from {scope}, not the owner's own answer"
        elif origin == "published":
            provenance["label"] = f"Published taste from {scope}, not the owner's own answer"
        return provenance
    return None


OwnerKnowledgeStore = SQLiteKnowledgeStore


__all__ = [
    "GLOBAL_SCOPE",
    "KNOWLEDGE_GENERATION",
    "ORIGINS",
    "OWNER_ID",
    "KnowledgeEntry",
    "OwnerKnowledgeStore",
    "SQLiteKnowledgeStore",
    "effective_origin",
    "knowledge_provenance",
    "knowledge_resolution",
    "normalize_question",
    "normalize_scope",
    "owner_knowledge_denial",
    "scope_applies",
    "scope_origin",
]
