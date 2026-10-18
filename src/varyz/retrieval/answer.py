"""The extractive `recall` and `brief` half of the agent read surface.

Two tools, because they answer two different shapes of request:

  recall(question)  one question, the passages that bear on it
  brief(task)       one task, the context needed before starting it

`brief` is not `recall` with a bigger limit. A question has one answer to find;
a task has several things worth knowing that share no vocabulary with each
other — who owns this, what broke last time, what was decided and when. So
`brief` fans the task out into several queries, runs each, and merges the
results, which surfaces material that no single ranking of the original
sentence would reach.

The fan-out is local and lexical. It could be done better by a model, and it
deliberately is not: a read path that needs an LLM is a read path that needs a
key, a network, and a bill, and the whole point is that this one needs none of
them. The one model `recall` does consult, the judge in `judge.py`, is the
owner's own local model, and `recall` answers without it when it is absent.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from varyz.config import Config
from varyz.db.dao import row_to_entry
from varyz.domain import SearchHit, Sensitivity
from varyz.pipeline.embed import Embedder
from varyz.retrieval import conversation_evidence
from varyz.retrieval.capsule import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_CHARS,
    Capsule,
    Evidence,
    allowed_sensitivity,
    to_capsules,
)
from varyz.retrieval.judge import Judge, Reading
from varyz.retrieval.rerank import Reranker, asks_for_current_state
from varyz.retrieval.search import (
    _STOPWORDS,
    RRF_K,
    _fallback_chunk,
    hybrid_search,
    is_keyword_query,
    is_supported,
    legacy_hybrid_search,
    lexically_grounded,
    names_the_archive_has_never_seen,
)

# Words that carry no retrieval signal in a task description. Not a general
# stop list: "how", "what" and "when" are kept, because they change which
# entries answer a question, while "the" and "please" never do.
# Written as prose rather than a list literal so the list stays readable and
# reviewable; ruff would rather see 64 quoted strings on one line.
_TASK_NOISE_WORDS = (
    "a an and are as at be been being but by can could do does doing for from "
    "get getting go going had has have having i if in into is it its me my "
    "need needs of on or our out please should so some that the their them "
    "then there these they this those to us want we will with would you your"
)
_TASK_NOISE = frozenset(_TASK_NOISE_WORDS.split())

_WORD = re.compile(r"[A-Za-z0-9_./\\-]+")

# Above this many words a task reads as a paragraph rather than a request, and
# the whole-text query stops being a useful ranking of anything.
_LONG_TASK_WORDS = 25

# Sensitive entries are dropped after ranking, so retrieving exactly `limit`
# hits returned fewer than `limit` capsules whenever any of them were sensitive
# — and the ones that would have filled the gap were sitting just below the
# cut. Retrieve past the limit and truncate after filtering instead. Additive
# rather than a multiplier: the cost is a few more rows from an index that is
# already open, and it should not grow with a limit an agent chose.
_SENSITIVITY_HEADROOM = 12


def _salient(task: str) -> list[str]:
    """The words in a task that a search could plausibly match on."""
    seen: list[str] = []
    for word in _WORD.findall(task):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in _TASK_NOISE or lowered in seen:
            continue
        seen.append(lowered)
    return seen


def subqueries(task: str, max_queries: int = 4) -> list[str]:
    """Split a task into the handful of searches worth running for it.

    The task as written comes first: it is the only query that carries the
    user's phrasing intact, and for a short task it is usually the best one.
    After that come sentence clauses, then pairs of salient words — pairs
    rather than single words because one word retrieves a topic while two
    retrieve a situation.
    """
    task = task.strip()
    if not task:
        return []
    queries = [task]
    words = _salient(task)

    clauses = [c.strip() for c in re.split(r"[.;?!\n]+|\band\b|\bthen\b", task) if c.strip()]
    for clause in clauses:
        if len(queries) >= max_queries:
            break
        # A single-clause task would otherwise re-run itself under a new name.
        if clause.lower() != task.lower() and len(_salient(clause)) >= 2:
            queries.append(clause)

    if len(words) >= 2:
        for index in range(0, len(words) - 1, 2):
            if len(queries) >= max_queries:
                break
            pair = " ".join(words[index : index + 2])
            if pair.lower() not in {q.lower() for q in queries}:
                queries.append(pair)
    return queries[:max_queries]


def _fuse(ranked_lists: list[list[SearchHit]], limit: int) -> list[SearchHit]:
    """Reciprocal-rank fusion over several queries' results.

    The same constant as the fts/vector fusion inside a single search, and for
    the same reason: rank position is comparable across rankings while raw
    scores are not, and a passage that several sub-queries independently rank
    highly is more likely to matter than one that a single sub-query loves.
    """
    scores: dict[str, float] = {}
    best: dict[str, tuple[int, SearchHit]] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits):
            key = hit.entry.id
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            # Keep the excerpt from whichever query ranked it highest: that is
            # the passage most likely to explain why it surfaced at all.
            if key not in best or rank < best[key][0]:
                best[key] = (rank, hit)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [best[entry_id][1].model_copy(update={"score": score}) for entry_id, score in ordered]


def recall(
    conn: sqlite3.Connection,
    cfg: Config,
    question: str,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    judge: Judge | None = None,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_sensitive: bool = False,
    excluded_sources: tuple[str, ...] = (),
) -> Evidence:
    """Passages from the user's own archive that bear on `question`.

    Abstention is on. A person searching their own notes can judge a weak
    result for themselves and will simply search again; an agent cannot, and
    will write the weak result into its answer as though it were evidence.

    With a reranker, every candidate is reread against the question, the
    strongest are followed to the records they belong with, and anything far
    below the best is dropped rather than padding the answer to `limit`.
    With a judge as well, the owner's local model decides which of the
    candidates answer, and the records its choices lead to are read and
    judged once more.
    """
    question = question.strip()
    if not question:
        return Evidence(query=question)
    allowed = allowed_sensitivity(include_sensitive)
    judged = False
    if reranker is not None and reranker.calibration is not None:
        hits, judged = _checked_evidence(
            conn,
            cfg,
            question,
            embedder,
            reranker,
            judge,
            excluded_sources=excluded_sources,
        )
    else:
        hits = hybrid_search(
            conn,
            question,
            embedder,
            limit=limit + _SENSITIVITY_HEADROOM,
            abstain=True,
            max_distance=cfg.retrieval.abstain_distance,
            rare_term_max_entries=cfg.retrieval.rare_term_max_entries,
            excluded_sources=excluded_sources,
        )
    capsules, withheld = to_capsules(hits, max_chars=max_chars, allowed=allowed)
    spoken, spoken_withheld = conversation_evidence.capsules(
        conn,
        question,
        limit=limit,
        max_chars=max_chars,
        allowed=allowed,
        embedder=embedder,
        # The same second look the passage channel gets, on the conversations
        # it was going to return anyway: words alone put the right exchange
        # first for about half of these questions, and reading the exchange
        # against the question takes that to 56-58% without dropping anything
        # from the answer. See RERANK_DEPTH for why it reads only that far.
        reranker=reranker,
        excluded_sources=excluded_sources,
    )
    # Conversations take the slots the passage channel leaves empty, and at
    # least a few regardless.
    #
    # Merging the two by score would be arithmetic on numbers that do not mean
    # the same thing -- one ranks a passage against passages, the other a whole
    # exchange against exchanges -- so the split is by room rather than by
    # rank. On this archive it matters: for a question about something arranged
    # over chat the passage channel returns nothing at all, every message
    # behind it carrying the fail-closed source default, and a fixed three-slot
    # reservation then throws away the answer at rank four while five slots sit
    # empty. Measured on 120 such questions, three slots found the conversation
    # 62.5% of the time and the empty ones took it to 73.3%. Where the passage
    # channel does have evidence -- mail, notes, commits -- it keeps its places
    # and conversations fall back to the floor.
    reserved = min(len(spoken), _CONVERSATION_SLOTS)
    take = min(len(spoken), max(reserved, limit - len(capsules)))
    kept = capsules[: limit - take] + spoken[:take]
    return Evidence(
        query=question,
        capsules=kept[:limit],
        withheld_sensitive=withheld + spoken_withheld,
        abstained=not hits and not spoken,
        judged=judged,
    )


# Slots a conversation may take from the passage channel when that channel has
# evidence of its own. Three is enough for an exchange to be corroborated by
# another exchange, and small enough that passages keep most of the answer.
_CONVERSATION_SLOTS = 3

# Candidates read before the reranker sees them. The records that answer a
# question as people phrase it sat as deep as 57th by keywords and 47th by
# meaning on the benchmark; 40 reaches most of them, and 100 only added
# distractors the reranker then preferred.
_CANDIDATE_POOL = 40
# How many of the best records are followed, and how far. Splitting the
# question into sub-queries first found nothing following these did not.
_CHAIN_SEEDS = 2
_CHAIN_POOL = 12
_CHAIN_PROBE_CHARS = 600
# How the judge's choices are followed. Records that belong with a chosen one
# share its rare words, a ticket number or a name, far more reliably than its
# overall meaning; following more than three choices, showing more than 15
# new records, or judging more than twice, did not find more on the benchmark.
_FOLLOW_PICKS = 3
_FOLLOW_POOL = 12
_FOLLOW_SHORTLIST = 15
# A judge that picks more than this is not choosing, and only the picks the
# reranker also stands behind are kept. On the benchmark answers needed at
# most five records, and every longer pick was mostly records the reranker
# scored as unrelated.
_MOST_PICKS = 6
_RARE_WORD_ENTRIES = 8
_RARE_WORD = re.compile(r"[\w'-]+")


def _passage(hit: SearchHit) -> str:
    title = hit.entry.title or ""
    text = hit.chunk or hit.entry.body
    if title and text.startswith(title):
        return text.strip()
    return f"{title}\n{text}".strip()


def _rare_words(conn: sqlite3.Connection, text: str) -> list[str]:
    """The words of `text` that only a handful of records share."""
    words: list[str] = []
    for match in _RARE_WORD.findall(text):
        word = match.strip("'-")
        if len(word) < 3 or word.isdigit() or word.lower() in _STOPWORDS or word in words:
            continue
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM"
            " (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?)",
            (_fts_phrase(word), _RARE_WORD_ENTRIES + 1),
        ).fetchone()
        if count <= _RARE_WORD_ENTRIES:
            words.append(word)
    return words


def _fts_phrase(word: str) -> str:
    return '"' + word.replace('"', '""') + '"'


def _records_sharing_rare_words(
    conn: sqlite3.Connection,
    hit: SearchHit,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> list[SearchHit]:
    words = _rare_words(conn, _passage(hit))
    if not words:
        return []
    normalized = tuple(
        sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
    )
    source_clause = ""
    if normalized:
        source_clause = (
            " AND lower(e.source) NOT IN (" + ",".join("?" for _ in normalized) + ")"
        )
    rows = conn.execute(
        f"""SELECT e.* FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid
           WHERE entries_fts MATCH ?{source_clause}
           ORDER BY bm25(entries_fts) LIMIT ?""",
        (
            " OR ".join(_fts_phrase(word) for word in words),
            *normalized,
            _FOLLOW_POOL + 1,
        ),
    ).fetchall()
    query = " ".join(words)
    return [
        SearchHit(
            entry=row_to_entry(row),
            score=0.0,
            chunk=_fallback_chunk(conn, row["id"], query),
            match_reasons=("shares rare words with a chosen record",),
        )
        for row in rows
        if row["id"] != hit.entry.id
    ][:_FOLLOW_POOL]


def _judged(
    conn: sqlite3.Connection,
    question: str,
    reranker: Reranker,
    judge: Judge,
    first: Future[Reading | None],
    candidates: dict[str, SearchHit],
    scores: dict[str, float],
    found_since: list[str],
    excluded_sources: tuple[str, ...] = (),
) -> list[str] | None:
    """The records the judge says answer, or None if it could not say.

    `first` is its reading of the first-stage candidates and `found_since` the
    records chaining added after it began. Adds the records its choices lead
    to to `candidates` and `scores`.
    """
    try:
        reading = first.result()
    except Exception:
        return None
    if reading is None:
        return None
    chosen = [hit.entry.id for hit in reading.picks]
    shown = {hit.entry.id for hit in reading.hits}
    # A chained record goes in front of the judge if the reranker would have
    # put it among the records the judge read.
    strongest = sorted(scores, key=scores.__getitem__, reverse=True)[: len(reading.hits)]
    chained = [entry_id for entry_id in strongest if entry_id in found_since]
    allowed = allowed_sensitivity(False)
    followed: dict[str, SearchHit] = {}
    for hit in [hit for hit in reading.picks if hit.entry.sensitivity in allowed][:_FOLLOW_PICKS]:
        for linked in _records_sharing_rare_words(
            conn, hit, excluded_sources=excluded_sources
        ):
            if linked.entry.id not in scores:
                followed.setdefault(linked.entry.id, linked)
    if followed:
        candidates.update(followed)
        scores.update(
            zip(
                followed,
                reranker.score(question, [_passage(hit) for hit in followed.values()]),
                strict=True,
            )
        )
    unread = [entry_id for entry_id in followed if entry_id not in shown]
    more = chained + sorted(unread, key=scores.__getitem__, reverse=True)[:_FOLLOW_SHORTLIST]
    if not more:
        return chosen
    again = judge.read_more(reading, [candidates[entry_id] for entry_id in more])
    if again is None:
        return chosen
    return [hit.entry.id for hit in again.picks]


_judge_thread: ThreadPoolExecutor | None = None


def _in_background(work: Callable[..., Reading | None], *args: Any) -> Future[Reading | None]:
    global _judge_thread
    if _judge_thread is None:
        _judge_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="varyz-judge")
    return _judge_thread.submit(work, *args)


def _checked_evidence(
    conn: sqlite3.Connection,
    cfg: Config,
    question: str,
    embedder: Embedder | None,
    reranker: Reranker,
    judge: Judge | None = None,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[SearchHit], bool]:
    """The records that answer, and whether the judge's picks decided them."""
    calibration = reranker.calibration
    assert calibration is not None
    if excluded_sources and not lexically_grounded(
        conn, question, excluded_sources=excluded_sources
    ):
        return [], False
    if judge is not None and not judge.ready():
        # A model that has to load first holds the answer for twenty seconds
        # or more. This question gets the reranker's cut now, and the model
        # loads for the next one.
        judge.warm()
        judge = None
    # A name the archive has never seen is conclusive, and so is a bare
    # keyword the archive does not contain; neither is for a model to overrule.
    if names_the_archive_has_never_seen(
        conn, question, excluded_sources=excluded_sources
    ):
        return [], False
    supported = is_supported(
        conn,
        question,
        embedder,
        max_distance=cfg.retrieval.abstain_distance,
        rare_term_max_entries=cfg.retrieval.rare_term_max_entries,
        excluded_sources=excluded_sources,
    )
    if not supported and is_keyword_query(question):
        return [], False

    candidates = {
        hit.entry.id: hit
        for hit in hybrid_search(
            conn,
            question,
            embedder,
            limit=_CANDIDATE_POOL,
            excluded_sources=excluded_sources,
        )
    }
    if not candidates:
        return [], False
    first = None
    if judge is not None:
        # The judge reads the first-stage candidates while the reranker scores
        # them: the one waits on the graphics card, the other on the processor.
        first = _in_background(judge.read, question, list(candidates.values()))
    scores = dict(
        zip(
            candidates,
            reranker.score(question, [_passage(hit) for hit in candidates.values()]),
            strict=True,
        )
    )
    best = max(scores.values())
    # The distance gate refuses questions it should answer when the words
    # differ from the record's; a cross-encoder that is sure overrules it,
    # and so does one that is only fairly sure when a judge reads the records
    # next and can still refuse.
    bar = calibration.confident if judge is None else calibration.floor
    if not supported and best < bar:
        return [], False

    # Only records the answer may contain are followed: a sensitive record's
    # neighbours are exactly what must not be handed over in its place.
    allowed = allowed_sensitivity(False)
    seeds = [
        entry_id
        for entry_id, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if candidates[entry_id].entry.sensitivity in allowed
    ][:_CHAIN_SEEDS]
    found_since: list[str] = []
    for entry_id in seeds:
        probe = _passage(candidates[entry_id])[:_CHAIN_PROBE_CHARS]
        linked = [
            hit
            for hit in legacy_hybrid_search(
                conn,
                probe,
                embedder,
                limit=_CHAIN_POOL,
                excluded_sources=excluded_sources,
            )
            if hit.entry.id not in candidates
        ]
        if not linked:
            continue
        for hit, score in zip(
            linked, reranker.score(question, [_passage(hit) for hit in linked]), strict=True
        ):
            candidates[hit.entry.id] = hit
            scores[hit.entry.id] = score
            found_since.append(hit.entry.id)
    best = max(scores.values())

    judged = None
    if judge is not None and first is not None:
        judged = _judged(
            conn,
            question,
            reranker,
            judge,
            first,
            candidates,
            scores,
            found_since,
            excluded_sources,
        )
    # The best score is taken over withheld records too: a public record far
    # below a withheld answer is not an answer, only what is left.
    cutoff = max(best - calibration.margin, calibration.floor)
    if judged is not None:
        if len(judged) > _MOST_PICKS:
            judged = [entry_id for entry_id in judged if scores[entry_id] >= cutoff]
        kept = sorted(judged, key=scores.__getitem__, reverse=True)
    else:
        kept = sorted(
            (entry_id for entry_id, score in scores.items() if score >= cutoff),
            key=lambda entry_id: scores[entry_id],
            reverse=True,
        )
        # When the best record is one the answer may not contain, the public
        # records beside it are its stand-ins, not the answer: report only
        # that something was withheld.
        if kept and candidates[kept[0]].entry.sensitivity not in allowed:
            kept = [
                entry_id
                for entry_id in kept
                if candidates[entry_id].entry.sensitivity not in allowed
            ]
    if asks_for_current_state(question):
        kept.sort(
            key=lambda entry_id: candidates[entry_id].entry.occurred_start or "", reverse=True
        )
    return [
        candidates[entry_id].model_copy(update={"score": scores[entry_id]}) for entry_id in kept
    ], judged is not None


def brief(
    conn: sqlite3.Connection,
    cfg: Config,
    task: str,
    *,
    embedder: Embedder | None = None,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_sensitive: bool = False,
    excluded_sources: tuple[str, ...] = (),
) -> Evidence:
    """Context worth having before starting `task`.

    Abstention is decided once, on the task as the user wrote it, and never on
    the sub-queries. That is not a convenience — it is where the threshold is
    valid. It was calibrated on a golden set of natural-language questions, and
    measured against this corpus the sub-queries sit systematically further
    away than the sentence they were derived from: for "add a new connector for
    reading local documents" the task scores 4.335 against a 4.4 floor while
    every derived pair scores 4.67 to 5.69. An embedding model trained on
    sentences puts two-word fragments somewhere else entirely, so a threshold
    read off sentences says nothing about them.

    Gating each sub-query separately therefore did not filter noise, it deleted
    the fan-out: on-topic tasks had every derived query abstain, leaving `brief`
    as an expensive alias for `recall`. Deciding once, up front, restores it —
    and the decision is made on the one string the threshold actually describes.
    """
    task = task.strip()
    if not task:
        return Evidence(query=task)
    if excluded_sources and not lexically_grounded(
        conn, task, excluded_sources=excluded_sources
    ):
        return Evidence(query=task, abstained=True)
    if not is_supported(
        conn,
        task,
        embedder,
        max_distance=cfg.retrieval.abstain_distance,
        rare_term_max_entries=cfg.retrieval.rare_term_max_entries,
        excluded_sources=excluded_sources,
    ):
        return Evidence(query=task, abstained=True)

    queries = subqueries(task)
    # A long task is a paragraph; ranking against the whole paragraph averages
    # every term in it into noise, so let the clauses carry the search.
    if len(_WORD.findall(task)) > _LONG_TASK_WORDS and len(queries) > 1:
        queries = queries[1:]

    # Each sub-query is allowed to contribute, so no one of them can crowd the
    # brief out; the fusion below decides what actually survives.
    per_query = max(3, limit)
    ranked = [
        hybrid_search(
            conn,
            query,
            embedder,
            limit=per_query,
            excluded_sources=excluded_sources,
        )
        for query in queries
    ]
    hits = _fuse([hits for hits in ranked if hits], limit + _SENSITIVITY_HEADROOM)
    allowed = allowed_sensitivity(include_sensitive)
    capsules, withheld = to_capsules(hits, max_chars=max_chars, allowed=allowed)

    # Conversations are context before a task as much as documents are: what
    # was agreed, by whom, and when. `brief` searched only the passage index,
    # so an agent briefed on a task that was arranged over chat was briefed on
    # everything except the arrangement. Same channel and same slot rule as
    # `recall` -- conversations take the places the passages leave empty, and
    # at least three when the passages do not fill them.
    spoken, spoken_withheld = _spoken_for(
        conn, queries, limit=limit, max_chars=max_chars, allowed=allowed,
        embedder=embedder,
        excluded_sources=excluded_sources,
    )
    reserved = min(len(spoken), _CONVERSATION_SLOTS)
    take = min(len(spoken), max(reserved, limit - len(capsules)))
    kept = capsules[: limit - take] + spoken[:take]
    return Evidence(
        query=task,
        capsules=kept[:limit],
        withheld_sensitive=withheld + spoken_withheld,
        abstained=not hits and not spoken,
    )


def _spoken_for(
    conn: sqlite3.Connection,
    queries: Sequence[str],
    *,
    limit: int,
    max_chars: int,
    allowed: tuple[Sensitivity, ...],
    embedder: Embedder | None,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[Capsule], int]:
    """The conversation channel across several sub-queries, deduplicated.

    Best-rank-first rather than fused by score: a conversation that one clause
    of the task puts first should not be demoted for being irrelevant to the
    others, which is the usual case for a task that mentions two things.
    """
    found: list[Capsule] = []
    seen: set[str] = set()
    withheld = 0
    for query in queries:
        # No reranker here, deliberately. These are sub-queries -- fragments a
        # planner derived from the task, not sentences somebody asked -- and a
        # cross-encoder trained on question-passage pairs has as little to say
        # about a two-word fragment as the embedding threshold does, which is
        # the same reason abstention is decided once on the task above. It
        # would also be paid once per sub-query, on the path that already fans
        # out the most.
        capsules, held = conversation_evidence.capsules(
            conn, query, limit=limit, max_chars=max_chars, allowed=allowed,
            embedder=embedder,
            excluded_sources=excluded_sources,
        )
        withheld += held
        for capsule in capsules:
            if capsule.external_id in seen:
                continue
            seen.add(capsule.external_id)
            found.append(capsule)
    return found, withheld
