"""Hybrid search: FTS5 BM25 + sqlite-vec KNN, fused with Reciprocal Rank Fusion."""

from __future__ import annotations

import re
import sqlite3

from sqlite_vec import serialize_float32

from varyz.config import (
    CALIBRATED_ABSTAIN_DISTANCE,
    MIN_CORPUS_VECTORS_FOR_ABSTENTION,
    MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE,
)
from varyz.db.dao import row_to_entry
from varyz.domain import SearchHit
from varyz.pipeline.embed import Embedder
from varyz.retrieval.behavior_search import (
    BehaviorIntent,
    analyze_behavior_query,
    assess_behavior_evidence,
)

RRF_K = 60  # standard damping constant

# GBrain's bounded vector-pool expansion showed a useful failure mode here:
# a long document can occupy the entire chunk pool before entry aggregation.
# Start with the old scan size; at most three expansions and 20,000 chunks.
_VECTOR_POOL_MAX_CHUNKS = 20_000
_VECTOR_POOL_MAX_MULTIPLIER = 64

# Function words and question scaffolding. Under FTS5's implicit AND these were
# hard requirements, so "Why was memoir forked?" demanded a document containing
# "why" and "was" — which is why the keyword arm returned almost nothing for
# anything phrased as a question. Removed rather than down-weighted: they carry
# no topical signal in any arm.
_STOPWORD_TEXT = """
a about after all also am an and any are as at be because been before being
between both but by can cannot could did do does doing done down during each
few for from further had has have having he her here hers him his how i if in
into is it its just me more most my no nor not now of off on once only or other
our out over own same she should so some such than that the their them then
there these they this those through to too under until up us use used using
very was way we were what when where whether which while who whom why will with
would you your
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def _fts_terms(query: str) -> list[str]:
    """Content-bearing terms of a query, quoted for FTS5.

    Quoting neutralises FTS5's operators (AND, OR, NOT, NEAR, *, ^) so a
    question containing one of them cannot become a syntax error or, worse, a
    different query than the user asked.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD.findall(query):
        word = match.lower()
        if word in _STOPWORDS or len(word) < 2 or word in seen:
            continue
        seen.add(word)
        terms.append('"' + match.replace('"', '""') + '"')
    return terms


def fts_escape(query: str) -> str:
    """Free text as an FTS5 disjunction over content terms.

    OR, not AND. Requiring every term means one word the author happened not to
    use costs the whole match, and a natural-language question always contains
    several. BM25 already rewards documents matching more of the terms and rarer
    ones, so disjunction loses no precision at the top of the ranking — it only
    stops the arm from returning nothing at all.
    """
    terms = _fts_terms(query)
    if not terms:
        # A query of pure stopwords: fall back to the raw tokens so the caller
        # still gets whatever FTS can find rather than an empty result.
        terms = ['"' + t.replace('"', '""') + '"' for t in query.split() if t.strip()]
    return " OR ".join(terms)


# What matched, marked in the snippet. Printable brackets were read as
# terminal markup by the caller that displays them, which quietly deleted the
# very word the user searched for; control characters cannot occur in an entry
# and cannot be mistaken for anything else.
MATCH_OPEN = ""
MATCH_CLOSE = ""


def _excluded_source_clause(
    excluded_sources: tuple[str, ...], *, alias: str = "e"
) -> tuple[str, tuple[str, ...]]:
    normalized = tuple(
        sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
    )
    if not normalized:
        return "", ()
    placeholders = ",".join("?" for _ in normalized)
    return f" AND lower({alias}.source) NOT IN ({placeholders})", normalized


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """Returns (entry ids ranked best-first, snippets by entry id)."""
    fts = fts_escape(query)
    if not fts:
        return [], {}
    source_clause, source_params = _excluded_source_clause(excluded_sources)
    rows = conn.execute(
        f"""SELECT e.id, snippet(entries_fts, 1, char(2), char(3), '…', 12) AS snip
           FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid
           WHERE entries_fts MATCH ?{source_clause}
           ORDER BY bm25(entries_fts) LIMIT ?""",
        (fts, *source_params, limit),
    ).fetchall()
    return [r["id"] for r in rows], {r["id"]: r["snip"] for r in rows}


def expanded_keyword_search(
    conn: sqlite3.Connection,
    intent: BehaviorIntent,
    limit: int,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """FTS candidate generation with a person constraint and concept synonyms."""
    behavior = " OR ".join(_fts_phrase(term) for term in intent.concept.fts_terms)
    focus = " AND ".join(_fts_phrase(term) for term in intent.focus_terms)
    expression = f"({focus}) AND ({behavior})" if focus else f"({behavior})"
    source_clause, source_params = _excluded_source_clause(excluded_sources)
    rows = conn.execute(
        f"""SELECT e.id, snippet(entries_fts, 1, char(2), char(3), '…', 18) AS snip
           FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid
           WHERE entries_fts MATCH ?{source_clause}
           ORDER BY bm25(entries_fts) LIMIT ?""",
        (expression, *source_params, limit),
    ).fetchall()
    return [row["id"] for row in rows], {row["id"]: row["snip"] for row in rows}


def _fts_phrase(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def vector_search(
    conn: sqlite3.Connection, query_vec: list[float], limit: int
) -> tuple[list[str], dict[str, str]]:
    """KNN over chunks, aggregated to entries by best-matching chunk.

    Returns (ranked entry ids, best-chunk text by entry id). SQLite gives the
    bare `c.text` column the value from the MIN(distance) row, so this is
    literally the single most relevant chunk of each entry."""
    if limit <= 0:
        return [], {}
    budget = limit * 3
    cap = max(budget, min(_VECTOR_POOL_MAX_CHUNKS, budget * _VECTOR_POOL_MAX_MULTIPLIER))
    encoded = serialize_float32(query_vec)
    while True:
        rows = conn.execute(
            """SELECT c.entry_id, c.text, MIN(v.distance) AS dist, COUNT(*) AS n
               FROM (SELECT rowid, distance FROM vec_chunks
                     WHERE embedding MATCH ? AND k = ? ORDER BY distance) v
               JOIN chunks c ON c.id = v.rowid
               GROUP BY c.entry_id
               ORDER BY dist""",
            (encoded, budget),
        ).fetchall()
        # The grouped count tells us whether the index ran out, without a
        # separate corpus-wide count. Preserve the nearest chunk as evidence.
        if len(rows) >= limit or sum(row["n"] for row in rows) < budget or budget >= cap:
            break
        budget = min(cap, budget * 4)
    rows = rows[:limit]
    return (
        [r["entry_id"] for r in rows],
        {r["entry_id"]: r["text"] for r in rows},
    )


def _without_excluded_sources(
    conn: sqlite3.Connection,
    entry_ids: list[str],
    excluded_sources: tuple[str, ...],
) -> list[str]:
    """Remove excluded entries before fusion so they cannot consume result slots."""

    normalized = {
        source.strip().casefold() for source in excluded_sources if source.strip()
    }
    if not normalized or not entry_ids:
        return entry_ids
    placeholders = ",".join("?" for _ in entry_ids)
    rows = conn.execute(
        f"SELECT id, source FROM entries WHERE id IN ({placeholders})", entry_ids
    ).fetchall()
    source_by_id = {row["id"]: row["source"].casefold() for row in rows}
    return [
        entry_id
        for entry_id in entry_ids
        if source_by_id.get(entry_id) not in normalized
    ]


DEFAULT_RARE_TERM_MAX_ENTRIES = 20


def resolve_abstain_distance(
    embedder: Embedder | None, override: float | None = None
) -> float | None:
    """The abstention floor for the model actually producing these vectors.

    Keyed off the embedder rather than configuration, because the two can
    disagree — an injected or swapped embedder is the one whose distance scale
    the threshold has to match, and applying another model's number would gate
    on a scale that was never measured.
    """
    if override is not None:
        return override
    if embedder is None:
        return None
    return CALIBRATED_ABSTAIN_DISTANCE.get(getattr(embedder, "model_name", ""))


def corpus_vectors(conn: sqlite3.Connection) -> int:
    """How many embedded chunks the index holds. Which gate applies depends on it."""
    row = conn.execute("SELECT COUNT(*) AS n FROM vec_chunks").fetchone()
    return int(row["n"]) if row is not None else 0


def corpus_supports_abstention(conn: sqlite3.Connection) -> bool:
    """Is this index large enough for the measured threshold to mean anything?

    The threshold is compared against a minimum over the whole index, so it
    silently encodes how large the index was when it was measured. See
    config.MIN_CORPUS_VECTORS_FOR_ABSTENTION for the numbers; the short version
    is that on an index a fortieth the size, 60% of answerable questions fall
    the wrong side of the line.
    """
    return corpus_vectors(conn) >= MIN_CORPUS_VECTORS_FOR_ABSTENTION


# Below this share of a question's content words appearing anywhere in the
# index, the archive is not about the subject at all. Measured on a 247-vector
# corpus where the distance gate is unusable: answerable questions all scored
# 1.00, unanswerable ones 0.00 to 0.83, and the widest gap inside the
# unanswerable set falls between 0.40 and 0.80. Set in that gap rather than at
# the class boundary — nine samples per class do not justify a tighter fit, and
# demanding full coverage would refuse any question containing one synonym the
# author happened not to use.
MIN_LEXICAL_COVERAGE = 0.5


def lexically_grounded(
    conn: sqlite3.Connection,
    query: str,
    min_coverage: float = MIN_LEXICAL_COVERAGE,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> bool:
    """Does the archive contain the words this question is made of?

    A weaker test than the distance gate, and a differently-shaped one: it
    cannot tell a good answer from a bad one, only a subject the archive has
    never mentioned from one it has. In exchange it degrades far more slowly
    with corpus size, which is what makes it usable where the distance gate
    is not — but "more slowly" is not "not at all". A corpus of three notes
    contains the vocabulary of no question, on-topic or otherwise, so below
    config.MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE the caller stops asking.

    Each term is looked up across the whole index, not within one entry. "Does
    my archive know these words" is the question; "does one document contain
    all of them" is FTS5's implicit AND, which is a different and much harsher
    test that this deliberately is not.
    """
    terms = _fts_terms(query)
    if not terms:
        return True  # nothing to test; the caller's other gates still apply
    present = 0
    source_clause, source_params = _excluded_source_clause(excluded_sources)
    for term in terms:
        row = conn.execute(
            "SELECT 1 FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
            f"WHERE entries_fts MATCH ?{source_clause} LIMIT 1",
            (term, *source_params),
        ).fetchone()
        if row is not None:
            present += 1
    return present / len(terms) >= min_coverage


# How close together a record has to say a question's words for them to be
# about one thing: a sentence or two. Coverage over a whole record is the wrong
# measure on an archive of any size -- a 1,745-character commit body carried
# five of the nine words of "When setup finishes with no agent connected,
# should the last line still say GO?" and was about something else entirely,
# because a long record collects common words the way an index does. Within
# any one window it held two.
EVIDENCE_WINDOW_TOKENS = 25

# The share of a question's words, weighted below, that one window of a record
# must hold before the record counts as evidence for it. Half, the same bar
# `lexically_grounded` sets for the whole index: a record may use its own
# synonym for a word or two, not for most of the question.
MIN_EVIDENCE_COVERAGE = 0.5

# A name the question carries weighs this many ordinary words. "What did I
# promise Lena?" is answered by a message that says "Lena asked for the launch
# draft by the 12th": the name is the subject, and "promise" and "due" are the
# asker's words for it, not the record's.
NAME_TERM_WEIGHT = 2.0


def evidence_coverage(
    conn: sqlite3.Connection,
    query: str,
    *,
    entry_id: str,
    window_id: str | None = None,
    excluded_sources: tuple[str, ...] = (),
    window_tokens: int = EVIDENCE_WINDOW_TOKENS,
    name_weight: float = NAME_TERM_WEIGHT,
) -> float:
    """How much of a question one returned record says, in one place.

    `lexically_grounded` asks the whole index, which is the right question for
    "has this archive ever mentioned the subject" and the wrong one for "is
    this record about it": on an archive of a few thousand entries nearly any
    question's words each turn up somewhere, so the index-wide test passes and
    the top keyword hit is whatever shared one or two of them. Retrieval then
    reports that hit as its best, because ranking is relative and never says no.

    This reads the one record, through the same porter-stemmed index retrieval
    used, so "finishes" and "finished" count as one word here exactly as they
    do there. Words the archive never uses are left out of the count: the
    index-wide gate has already judged those, and no record could hold them.
    Nothing here depends on how large the archive is, which is what neither
    the distance gate nor the index-wide one could offer.

    A conversation is read as its whole window, since that is what the capsule
    quotes and the first message alone rarely holds the question's words.
    """
    terms = _fts_terms(query)
    if not terms:
        return 1.0  # nothing to test, as in `lexically_grounded`
    source_clause, source_params = _excluded_source_clause(excluded_sources)
    names = {name.casefold() for name in _question_names(query)}
    weights: dict[str, float] = {}
    for term in terms:
        known = conn.execute(
            "SELECT 1 FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
            f"WHERE entries_fts MATCH ?{source_clause} LIMIT 1",
            (term, *source_params),
        ).fetchone()
        if known is not None:
            bare = term[1:-1].replace('""', '"').casefold()
            weights[term] = name_weight if bare in names else 1.0
    if not weights:
        return 0.0
    positions: list[tuple[int, str]] = []
    for term in weights:
        positions.extend(
            (at, term) for at in _match_positions(conn, term, entry_id, window_id)
        )
    positions.sort()
    best = 0.0
    start = 0
    for end in range(len(positions)):
        while positions[end][0] - positions[start][0] >= window_tokens:
            start += 1
        held = {term for _, term in positions[start : end + 1]}
        best = max(best, sum(weights[term] for term in held))
    return best / sum(weights.values())


# Marks `highlight` puts around a matched token. Control characters, for the
# reason MATCH_OPEN gives: they cannot occur in an entry.
_HIGHLIGHT_OPEN = "\x01"
_HIGHLIGHT_CLOSE = "\x04"
_HIGHLIGHT_TOKEN = re.compile(r"\x01|\x04|[^\W_]+")


def _match_positions(
    conn: sqlite3.Connection, term: str, entry_id: str, window_id: str | None
) -> list[int]:
    """Token offsets at which one quoted term matches one record.

    FTS5 knows where a term matched but has no offsets() to say so, so the
    record is read back through `highlight` with its matches marked and the
    marks are counted in. A title comes before its body, as it does on screen.
    """
    if window_id is not None:
        rows = conn.execute(
            "SELECT highlight(conversation_windows_fts, 1, ?, ?)"
            " FROM conversation_windows_fts"
            " WHERE window_id = ? AND conversation_windows_fts MATCH ?",
            (_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE, window_id, term),
        ).fetchall()
        texts = [row[0] for row in rows]
    else:
        # The rowid is looked up first because FTS5 reads `rowid = NULL` as no
        # rowid constraint at all: an id the index does not hold would match
        # whichever record first carries the term.
        entry = conn.execute(
            "SELECT rowid FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        row = None if entry is None else conn.execute(
            "SELECT highlight(entries_fts, 0, ?, ?), highlight(entries_fts, 1, ?, ?)"
            " FROM entries_fts WHERE entries_fts.rowid = ? AND entries_fts MATCH ?",
            (
                _HIGHLIGHT_OPEN,
                _HIGHLIGHT_CLOSE,
                _HIGHLIGHT_OPEN,
                _HIGHLIGHT_CLOSE,
                entry[0],
                term,
            ),
        ).fetchone()
        texts = [] if row is None else [row[0], row[1]]
    found: list[int] = []
    offset = 0
    inside = False
    for text in texts:
        for token in _HIGHLIGHT_TOKEN.findall(text or ""):
            if token == _HIGHLIGHT_OPEN:
                inside = True
            elif token == _HIGHLIGHT_CLOSE:
                inside = False
            else:
                if inside:
                    found.append(offset)
                offset += 1
    return found


# At most this many content words and the query is a term to look up rather
# than a question to answer. Two, so a compound name -- "telegram bridge",
# "san7 release" -- still counts as one thing being named.
MAX_KEYWORD_TERMS = 2


def is_keyword_query(query: str) -> bool:
    """Is this a word the caller wants looked up, not a question?"""
    terms = _fts_terms(query)
    return 1 <= len(terms) <= MAX_KEYWORD_TERMS


def names_the_archive_has_never_seen(
    conn: sqlite3.Connection,
    query: str,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> list[str]:
    """Proper nouns in the question that appear nowhere in the index.

    The distance gate stops separating the two classes as an index grows. On a
    53,910-vector archive the nearest neighbour of eight questions naming
    invented people and places sat between 3.198 and 4.605, and of four the
    archive could answer between 4.167 and 4.598 -- overlapping, and with the
    answerable ones farther away on average. That is not drift a recalibrated
    threshold fixes: a sentence embedding measures the shape of the sentence,
    and "What is my view on Grastelle pricing?" has the shape of a hundred
    questions this archive can answer. Only the one word that decides the
    answer is missing, and distance cannot see one word.

    Lexical presence can, and it does not degrade with size: a passage about
    Grastelle cannot exist in an index where the string never occurs. The test
    is restricted to names because that is the only class where absence is
    conclusive. A missing ordinary word is usually the author's synonym for one
    they did write -- refusing on "licence" because the archive says "license"
    would withhold an answer that is there -- but a name has no synonyms, and
    the same eight questions carry twelve missing names between them while four
    answerable ones carry none.

    Sentence-initial capitals are skipped: every question starts with one.
    """

    missing: list[str] = []
    source_clause, source_params = _excluded_source_clause(excluded_sources)
    for word in _question_names(query):
        phrase = '"' + word.replace('"', '""') + '"'
        row = conn.execute(
            "SELECT 1 FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
            f"WHERE entries_fts MATCH ?{source_clause} LIMIT 1",
            (phrase, *source_params),
        ).fetchone()
        if row is None:
            missing.append(word)
    return missing


def _question_names(query: str) -> list[str]:
    """Capitalised words of a question that grammar did not capitalise."""
    names: list[str] = []
    raw_words = query.split()
    for position, raw in enumerate(raw_words):
        # A capital at the start of the sentence is grammar, not a name. The
        # boundary is read off the raw word, before its punctuation is stripped.
        if position == 0 or raw_words[position - 1].endswith(("?", ".", "!", ":")):
            continue
        word = raw.rstrip(_TRAILING_PUNCTUATION).lstrip("([{\"'`")
        if word[:1].isupper() and word.isalpha() and len(word) >= 3:
            names.append(word)
    return names


def best_distance(conn: sqlite3.Connection, query_vec: list[float]) -> float | None:
    """Distance to the single closest passage in the corpus, or None if empty."""
    row = conn.execute(
        "SELECT distance FROM vec_chunks WHERE embedding MATCH ? AND k = 1 ORDER BY distance",
        (serialize_float32(query_vec),),
    ).fetchone()
    return None if row is None else float(row["distance"])


def _looks_like_identifier(word: str) -> bool:
    """Is this a name a system gave something, rather than an English word?

    Rarity alone is the wrong test: "blood" and "shoe" are rarer in a developer's
    archive than `san7_plus_annual`, so a rare-word rule opens the gate for
    exactly the unanswerable questions it exists to refuse. Shape is what
    separates them — identifiers carry punctuation or mix letters with digits,
    which prose words never do.
    """
    if any(mark in word for mark in ("_", ".", "/", "\\", "::")):
        return True
    return any(c.isdigit() for c in word) and any(c.isalpha() for c in word)


# Sentence punctuation, stripped from the end of a word before its shape is
# judged. Leading marks are kept: `.gitignore` and `_internal` begin with one.
_TRAILING_PUNCTUATION = ".,;:!?)]}\"'`"


def _identifier_candidates(query: str) -> list[str]:
    """Query words as written, for shape inspection.

    Deliberately not `_fts_terms`. That splits on `.` `/` `::` — the very marks
    `_looks_like_identifier` looks for — so feeding it here would leave three of
    that function's four punctuation tests unreachable and close the hatch on
    `src/varyz/config.py`, `README.md` and `.gitignore`, which are precisely the
    queries it exists to admit. Splitting on whitespace keeps a path or a dotted
    name in one piece.

    Trailing sentence punctuation would otherwise make every sentence-final word
    look like a filename; the frequency check downstream is what ultimately
    decides, so an occasional "e.g" reaching it is harmless.
    """
    out = []
    for raw in query.split():
        word = raw.rstrip(_TRAILING_PUNCTUATION).lstrip("([{\"'`")
        if any(c.isalnum() for c in word):
            out.append(word)
    return out


def _has_identifier_match(
    conn: sqlite3.Connection,
    query: str,
    max_entries: int,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> bool:
    """Does the query name something the corpus contains almost uniquely?

    Distance is a blunt instrument for identifiers: a filename, an error code,
    or a product SKU sits far from a natural-language question in embedding
    space no matter how exactly it matches. Document frequency catches what
    the vectors cannot, and unlike a score threshold it needs no calibration.

    The probe is a quoted phrase, so FTS5 splits `src/varyz/config.py` on its
    own punctuation and matches entries carrying those tokens in that order —
    which is the frequency being asked about.
    """
    for word in _identifier_candidates(query):
        if not _looks_like_identifier(word):
            continue
        phrase = '"' + word.replace('"', '""') + '"'
        source_clause, source_params = _excluded_source_clause(excluded_sources)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT entries_fts.rowid FROM entries_fts"
            " JOIN entries e ON e.rowid = entries_fts.rowid"
            f" WHERE entries_fts MATCH ?{source_clause} LIMIT ?)",
            (phrase, *source_params, max_entries + 1),
        ).fetchone()
        if row is not None and 0 < row["n"] <= max_entries:
            return True
    return False


def is_supported(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder | None,
    *,
    max_distance: float | None = None,
    rare_term_max_entries: int = DEFAULT_RARE_TERM_MAX_ENTRIES,
    excluded_sources: tuple[str, ...] = (),
) -> bool:
    """Can this corpus answer this question at all?

    Retrieval always ranks something; ranking is relative and never says "no".
    An agent handed the least-bad match for a question the corpus knows nothing
    about cannot tell it apart from a real answer, so the judgement has to be
    made here, once, on an absolute scale.

    Absolute embedding distance is used rather than a normalised or rank-based
    score. Scale-free variants were measured and separate the two cases worse:
    when nothing is relevant, everything is uniformly far, so the *shape* of
    the distance distribution looks much like a genuine broad match. Only the
    raw distance distinguishes them — at the cost of being specific to both the
    embedding model and the size of the corpus it was measured on, which is why
    neither the threshold nor the floor below is a constant in this file.
    """
    # An explicit number is the user's own, measured on their own corpus with
    # `evaluate_retrieval.py --calibrate`, so the size floor does not apply.
    calibrated_here = max_distance is not None
    max_distance = resolve_abstain_distance(embedder, max_distance)
    vectors = corpus_vectors(conn)
    # Name absence is conclusive at every corpus size: unlike semantic distance
    # or general vocabulary coverage, a proper noun has no synonym that might
    # rescue the question. Keep this before the small-index escape hatch, which
    # otherwise turns every gate off for a new archive and hands an agent an
    # unrelated result for a person, place, or product the archive never saw.
    if names_the_archive_has_never_seen(
        conn, query, excluded_sources=excluded_sources
    ):
        return False
    # The calibrated distance describes the whole vector index. An excluded
    # nearest neighbour must not decide whether an agent may read the eligible
    # corpus, so scoped retrieval uses its scoped lexical gate instead.
    if excluded_sources:
        return lexically_grounded(
            conn, query, excluded_sources=excluded_sources
        )
    if not calibrated_here and vectors < MIN_CORPUS_VECTORS_FOR_ABSTENTION:
        # Too small for the packaged threshold to describe anything, so the
        # question becomes which weaker test still holds at this size.
        if vectors < MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE:
            # None of them. Nothing has been measured this small, and an
            # archive of a few notes is one that was installed minutes ago:
            # refusing everything is the one outcome guaranteed to be wrong.
            return True
        # Lexical coverage — it cannot judge whether an answer is good, but it
        # can still tell a subject the archive has never mentioned from one it
        # has, and it was measured at roughly this size.
        return lexically_grounded(conn, query)
    if embedder is None or max_distance is None:
        # No vectors, or no threshold measured for this model. Either way the
        # only available judgement would be a guess, and a guess that discards
        # real answers is worse than the over-answering it would replace.
        return True
    if is_keyword_query(query):
        # A keyword is not a question, and the threshold above was measured on
        # questions. One or two words embed as a fragment, and on this archive
        # the fragment's distance is not merely uninformative about whether the
        # word is there -- it points the wrong way. Every real term measured sat
        # between 3.74 and 5.41 (`trading` 4.842, with over two hundred entries
        # carrying it); every invented one sat nearer, between 2.54 and 4.36
        # (`grastelle` 2.537, with none). Nine of thirty-three common terms were
        # refused while holding twelve to two hundred entries apiece, and four
        # nonsense tokens were accepted. `recall` reports a refusal as an
        # answer, so the first kind tells the user their archive has nothing;
        # the second hands an agent the least-bad match for a word that does
        # not occur.
        #
        # Lexical presence decides instead, and it must be every term rather
        # than half of them: at this length there is no second word to carry the
        # meaning if the first one is missing.
        return lexically_grounded(conn, query, min_coverage=1.0)
    distance = best_distance(conn, embedder.embed_query(query))
    if distance is None:
        return False
    if distance <= max_distance:
        return True
    return _has_identifier_match(
        conn,
        query,
        rare_term_max_entries,
        excluded_sources=excluded_sources,
    )


# How much of a chunk stands in for it when no keyword marked a place to look.
LEAD_SNIPPET_CHARS = 200


def lead_snippet(chunk: str) -> str:
    """The opening of a chunk, cut at a word, and saying that it was cut.

    A hit matched only by meaning has no keyword window to centre on, so the
    excerpt is the start of the chunk. It was cut at the two-hundredth
    character with nothing to mark it: the excerpt ended mid-word and read as
    the place the writing stopped. In a tool whose answers are quotations, an
    excerpt that does not admit it is an excerpt is the wrong kind of wrong.
    """
    flowed = chunk.strip()
    if len(flowed) <= LEAD_SNIPPET_CHARS:
        return flowed
    cut = flowed[:LEAD_SNIPPET_CHARS]
    space = cut.rfind(" ")
    if space > LEAD_SNIPPET_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "\u2026"


def _fallback_chunk(conn: sqlite3.Connection, entry_id: str, query: str) -> str | None:
    """Best chunk for a keyword-only hit: the chunk with the most query-term
    overlap (so we still send only the relevant part, not the whole entry)."""
    chunks = conn.execute(
        "SELECT text FROM chunks WHERE entry_id=? ORDER BY seq", (entry_id,)
    ).fetchall()
    if not chunks:
        return None
    terms = {t.lower() for t in query.split() if len(t) > 2}
    if not terms:
        return chunks[0]["text"]
    return max(chunks, key=lambda c: sum(t in c["text"].lower() for t in terms))["text"]


def legacy_hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder | None,
    limit: int = 10,
    pool: int = 30,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> list[SearchHit]:
    """The original one-query hybrid ranker, retained as an evaluation baseline."""
    kw_ids, kw_snippets = keyword_search(
        conn, query, pool, excluded_sources=excluded_sources
    )
    vec_ids: list[str] = []
    vec_snippets: dict[str, str] = {}
    if embedder is not None:
        vec_ids, vec_snippets = vector_search(conn, embedder.embed_query(query), pool)
        vec_ids = _without_excluded_sources(conn, vec_ids, excluded_sources)

    scores: dict[str, float] = {}
    # Which channel put a row on the list. Behavioral searches explain
    # themselves through the evidence assessment; an ordinary search had no
    # explanation at all, so `varyz search` rendered a "why" column that was
    # empty on every row of every result. Channel membership is the honest
    # answer for this path, and the one a user needs before `varyz embed` has
    # finished: a result nothing but the vector index found is a guess about
    # meaning, not a place the searched words appear.
    #
    # The keyword channel used to be called "your words", which reads as a
    # claim about who wrote the entry rather than about what was typed into
    # the box.  Searching a machine with a cloned repository in it answered
    # with three of that project's commits, every one of them labelled the
    # owner's words, and every one of them recorded in the same archive as
    # written by somebody else.  Whose words a row holds is a separate
    # question this path does not ask.
    found_by: dict[str, list[str]] = {}
    for label, ranked in (("your search words", kw_ids), ("similar meaning", vec_ids)):
        for rank, entry_id in enumerate(ranked):
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            found_by.setdefault(entry_id, []).append(label)

    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    hits = []
    for entry_id, score in top:
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            continue
        if row["source"].casefold() in {
            source.strip().casefold() for source in excluded_sources
        }:
            continue
        chunk = vec_snippets.get(entry_id) or _fallback_chunk(conn, entry_id, query)
        hits.append(
            SearchHit(
                entry=row_to_entry(row),
                score=score,
                snippet=kw_snippets.get(entry_id) or (lead_snippet(chunk) if chunk else None),
                chunk=chunk,
                match_reasons=tuple(found_by.get(entry_id, ())),
            )
        )
    return hits


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder | None,
    limit: int = 10,
    pool: int = 30,
    abstain: bool = False,
    max_distance: float | None = None,
    rare_term_max_entries: int = DEFAULT_RARE_TERM_MAX_ENTRIES,
    excluded_sources: tuple[str, ...] = (),
) -> list[SearchHit]:
    """Hybrid retrieval with calibrated behavioral-query understanding.

    Ordinary searches keep the stable FTS/vector RRF path. Behavioral searches
    add one local ontology expansion, one optional paraphrase vector, a named
    person constraint, speaker attribution, and evidence-based reranking.

    `abstain` returns an empty list when nothing in the corpus is relevant.
    Off by default because a person browsing their own archive can judge a weak
    result for themselves; callers that feed an agent should turn it on, since
    an agent cannot.
    """
    if abstain and not is_supported(
        conn,
        query,
        embedder,
        max_distance=max_distance,
        rare_term_max_entries=rare_term_max_entries,
        excluded_sources=excluded_sources,
    ):
        return []
    intent = analyze_behavior_query(query)
    if intent is None:
        return legacy_hybrid_search(
            conn,
            query,
            embedder,
            limit=limit,
            pool=pool,
            excluded_sources=excluded_sources,
        )
    return _behavioral_search(
        conn,
        query,
        intent,
        embedder,
        limit=limit,
        pool=max(pool, min(200, limit * 8)),
        excluded_sources=excluded_sources,
    )


def _behavioral_search(
    conn: sqlite3.Connection,
    query: str,
    intent: BehaviorIntent,
    embedder: Embedder | None,
    *,
    limit: int,
    pool: int,
    excluded_sources: tuple[str, ...] = (),
) -> list[SearchHit]:
    exact_ids, exact_snippets = keyword_search(
        conn, query, pool, excluded_sources=excluded_sources
    )
    expanded_ids, expanded_snippets = expanded_keyword_search(
        conn, intent, pool, excluded_sources=excluded_sources
    )
    vector_ids: list[str] = []
    vector_snippets: dict[str, str] = {}
    paraphrase_ids: list[str] = []
    paraphrase_snippets: dict[str, str] = {}
    if embedder is not None:
        vector_ids, vector_snippets = vector_search(
            conn, embedder.embed_query(query), pool
        )
        paraphrase_ids, paraphrase_snippets = vector_search(
            conn, embedder.embed_query(intent.expanded_query), pool
        )
        vector_ids = _without_excluded_sources(conn, vector_ids, excluded_sources)
        paraphrase_ids = _without_excluded_sources(
            conn, paraphrase_ids, excluded_sources
        )

    channels = (exact_ids, expanded_ids, vector_ids, paraphrase_ids)
    retrieval_scores: dict[str, float] = {}
    for ranked in channels:
        for rank, entry_id in enumerate(ranked):
            retrieval_scores[entry_id] = retrieval_scores.get(entry_id, 0.0) + (
                1.0 / (RRF_K + rank + 1)
            )

    expanded_text = " ".join((query, *intent.concept.fts_terms))
    # Only the keyword channels return a window centred on what matched; a
    # vector channel returns the whole chunk it ranked, which is an excerpt of
    # nothing in particular and up to a thousand characters of it.
    keyword_snippets = {**expanded_snippets, **exact_snippets}
    hits = []
    for entry_id, retrieval_score in retrieval_scores.items():
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            continue
        if row["source"].casefold() in {
            source.strip().casefold() for source in excluded_sources
        }:
            continue
        entry = row_to_entry(row)
        assessment = assess_behavior_evidence(intent, entry)
        if not assessment.relevant:
            continue
        chunk = (
            vector_snippets.get(entry_id)
            or paraphrase_snippets.get(entry_id)
            or _fallback_chunk(conn, entry_id, expanded_text)
        )
        hits.append(
            SearchHit(
                entry=entry,
                score=assessment.score + retrieval_score,
                snippet=keyword_snippets.get(entry_id) or (lead_snippet(chunk) if chunk else None),
                chunk=chunk,
                match_reasons=assessment.reasons,
                match_strength=assessment.strength,
            )
        )
    return sorted(hits, key=lambda hit: (hit.score, hit.entry.id), reverse=True)[:limit]
