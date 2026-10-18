"""Whole conversations as evidence, when the local model has cleared them.

`recall` answers with entries, and on a chat connector an entry is one message.
The median one in this archive is 47 characters: half a sentence, no context,
no reply. Searching for something that was unmistakably arranged over WhatsApp
returns the question without the answer, or the answer without the question,
or -- far more often -- nothing at all, because 91% of those messages carry the
fail-closed source default and never leave the machine.

This path returns the conversation instead, and it is the conversation pass
that makes that possible. A window a local model read and called `personal` is
ordinary life: logistics, plans, jokes, arrangements. It can be quoted like any
other personal entry. A window it called `sensitive`, and a window nobody has
read yet, stay shut -- absence of a judgement is not permission.

Nothing here rewrites `entries.sensitivity`. The widening lives in this module
and the label table it reads, so undoing it is dropping a path rather than
restoring forty-five thousand rows to values that may since have moved.
"""

from __future__ import annotations

import sqlite3

from varyz.domain import Sensitivity
from varyz.retrieval.capsule import DEFAULT_SENSITIVITY, Capsule
from varyz.retrieval.search import fts_escape

# Tokens of conversation returned around the match. Enough for both sides of an
# exchange to be present, which is the entire reason for preferring a window to
# a message; the caller's own `max_chars` still has the last word.
SNIPPET_TOKENS = 64

# How many releasable conversations the cross-encoder reads, and so also how
# far one can move. Reading twenty and reading eight win the same top-1 --
# 49.2% to 55.8% on 120 conversations of the real archive, replicated on a
# second sample -- but twenty also drops a conversation the words had at rank
# five out of the answer entirely, taking top-8 from 76.7% down to 72.5%, and
# costs 2.6s a question against 1.2s. Eight is the depth at which the second
# look can only reorder what the caller was going to be shown anyway, so it
# cannot lose an answer at any depth. Everything past it keeps its first-stage
# order, behind what was scored.
RERANK_DEPTH = 8

# How much of each conversation the cross-encoder reads. A third of this
# archive's exchanges run past it and are cut, so it is tempting to cut them
# harder and answer faster -- halving it to 600 costs more than it saves:
# top-1 falls from 55.8% to 52.5% and top-3 from 67.5% to 62.5%, which is
# below the words on their own. What makes an exchange the right one is often
# the reply, and the reply is at the end.
RERANK_CHARS = 1200

# Conversations only the vectors found, added to that reading rather than
# taking a word match's place in it. See `_slate`.
MEANING_EXTRA = 8

# Whether an added conversation must clear the model's own calibrated floor to
# keep a place in front of the word matches. See `_defensible`.
MEANING_FLOOR = True


def capsules(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int,
    max_chars: int,
    allowed: tuple[Sensitivity, ...] = DEFAULT_SENSITIVITY,
    embedder: object | None = None,
    reranker: object | None = None,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[Capsule], int]:
    """Conversations bearing on the question, best first, plus a withheld count.

    Ranked by the words of the exchange, then extended by its meaning: a
    conversation the words cannot reach at all is offered below the ones they
    can. See the comment on the ordering for why the two are not fused as
    peers. An archive whose conversations have not been embedded yet simply
    has nothing to append, so this degrades rather than fails.

    Over-fetches, because the gate runs after ranking: a sensitive conversation
    that outranks a releasable one must not also take its place. What was held
    back is counted and returned rather than dropped, so an agent can be told
    the archive has an answer it is not allowed to read -- a different thing
    from the archive having no answer.
    """
    if not _indexed(conn):
        return [], 0
    pool = max(limit * 4, limit)
    excluded = {
        source.strip().casefold() for source in excluded_sources if source.strip()
    }
    worded, excerpts = _by_words(
        conn, question, pool, excluded_sources=excluded_sources
    )
    # A reader asks the vector arm for more than the backfill needs: the
    # conversations it is given to defend come off the top of this list, and at
    # the limit an agent actually uses -- eight -- a pool of thirty-two mostly
    # contains the same conversations the words already found. The search
    # itself is hundredths of a second; only what the reader reads is paid for.
    meant = _by_meaning(
        conn,
        question,
        embedder,
        pool if reranker is None else max(pool, RERANK_DEPTH + MEANING_EXTRA * 3),
    )
    if excluded:
        meant = [
            window_id
            for window_id in meant
            if (row := _window(conn, window_id)) is not None
            and row["source"].casefold() not in excluded
        ]
    if not worded and not meant:
        return [], 0

    # Words rank; meaning only adds. Measured on 120 conversations of the real
    # archive, fusing the two lists the way the passage index fuses its own
    # (RRF, equal weight) was worse at every depth that matters -- top-1 fell
    # from 48.3% to 35.0%, top-8 from 69.2% to 62.5% -- because the vector arm
    # on its own reaches 13.3% top-1 and a second, much weaker opinion given
    # equal weight mostly promotes distractors. What it does do is find
    # conversations the words never find at all: 23 unreachable at any depth
    # became 17. So it is appended below the words, never mixed into them.
    order = worded + [window_id for window_id in meant if window_id not in set(worded)]

    names = {str(level) for level in allowed}
    releasable: list[tuple[str, sqlite3.Row, str]] = []
    withheld = 0
    for window_id in order:
        row = _window(conn, window_id)
        if row is None:
            continue
        if row["source"].casefold() in excluded:
            continue
        # No label means the pass has never read this conversation. Unread is
        # unclassified is sensitive, in that order and for the same reason.
        label = row["label"] or "sensitive"
        if label not in names:
            withheld += 1
            continue
        releasable.append((window_id, row, label))

    # The gate runs first and the reranker only reorders what survived it, so
    # a second look can never open a conversation the label keeps shut, and
    # the withheld count does not depend on the order anything came back in.
    if reranker is not None:
        meaning_only = {window_id for window_id in meant if window_id not in set(worded)}
        releasable = _reranked(conn, question, reranker, releasable, meaning_only)

    found: list[Capsule] = []
    for rank, (window_id, row, label) in enumerate(releasable[:limit]):
        found.append(
            Capsule(
                text=_excerpt(conn, window_id, excerpts, max_chars),
                source=row["source"],
                kind="conversation",
                title=None,
                occurred=row["started_at"],
                # The window, not a message: what the owner opens to check this
                # is the exchange, and a single message id would address one
                # line of it.
                external_id=window_id,
                entry_id=row["first_entry"] or "",
                score=1.0 / (rank + 1),
                sensitivity=label,
                # Both sides are in here by construction, so this is somebody
                # else's words as much as the owner's.
                authored_by_user=False,
            )
        )
    return found, withheld


def _reranked(
    conn: sqlite3.Connection,
    question: str,
    reranker: object,
    candidates: list[tuple[str, sqlite3.Row, str]],
    meaning_only: set[str],
) -> list[tuple[str, sqlite3.Row, str]]:
    """Reorder by reading each conversation against the question.

    The first stage ranks a window by the words it shares with the question
    and, failing that, by one vector for the whole exchange. Neither reads the
    conversation. Measured on 120 conversations of the real archive, the right
    one is first 49% of the time and somewhere in the first fifty 83% -- so a
    third of the answers are present and misplaced, which is exactly what a
    cross-encoder is for and is not something another index can fix.

    The same model `recall` already uses, on the same CPU, and it reads the
    exchange itself rather than the snippet: what makes a conversation the
    right one is often the reply, which the matched fragment need not contain.
    A model that fails to load or score leaves the first-stage order alone --
    a slower answer is better than none, and this path already degrades that
    way when there is no embedder.

    See `_slate` for what it is given to read, and why the vector arm gets a
    hearing there that the first-stage ordering cannot give it.
    """
    from varyz.pipeline.windows import window_text

    head, tail, extras = _slate(candidates, meaning_only)
    if len(head) < 2:
        return candidates
    try:
        scores = reranker.score(  # type: ignore[attr-defined]
            question,
            [window_text(conn, window_id)[:RERANK_CHARS] for window_id, _, _ in head],
        )
    except Exception:
        return candidates
    if len(scores) != len(head):
        return candidates
    ordered = sorted(zip(scores, head, strict=True), key=lambda pair: -pair[0])
    kept, demoted = _defensible(reranker, ordered, extras)
    return kept + demoted + tail


def _defensible(
    reranker: object,
    ordered: list[tuple[float, tuple[str, sqlite3.Row, str]]],
    extras: set[str],
) -> tuple[list[tuple[str, sqlite3.Row, str]], list[tuple[str, sqlite3.Row, str]]]:
    """Which of the added conversations earned the place they were scored into.

    A word match is where retrieval put it and stays there; this decides only
    about the conversations the vectors brought, which had no claim on a place
    to begin with. The model's own calibration says where its scores stop
    meaning anything -- a floor below which nothing is evidence, and a margin
    beyond which a candidate is noise next to the best one -- and those are the
    same two numbers `recall` already trusts on the passage path.

    Nothing is dropped. What cannot defend its place goes behind the scored
    slate, in the order the model put it, which is still ahead of everything
    the model never read.
    """
    if not MEANING_FLOOR or not extras or not ordered:
        return [item for _, item in ordered], []
    calibration = getattr(reranker, "calibration", None)
    if calibration is None:
        return [item for _, item in ordered], []
    best = ordered[0][0]
    kept: list[tuple[str, sqlite3.Row, str]] = []
    demoted: list[tuple[str, sqlite3.Row, str]] = []
    for score, item in ordered:
        if item[0] in extras and (
            score < calibration.floor or best - score > calibration.margin
        ):
            demoted.append(item)
        else:
            kept.append(item)
    return kept, demoted


def _slate(
    candidates: list[tuple[str, sqlite3.Row, str]], meaning_only: set[str]
) -> tuple[
    list[tuple[str, sqlite3.Row, str]],
    list[tuple[str, sqlite3.Row, str]],
    set[str],
]:
    """What the cross-encoder reads, what keeps its first-stage order, and
    which of the first were added rather than ranked.

    The ordering above can only append a vector-only conversation below every
    word match, and on this archive that buries it: of 20 questions whose
    conversation the channel missed entirely, the vectors ranked all 20 inside
    their own first sixty and seven of them in their first four, behind two
    hundred word matches that were all wrong. Nothing could be done about that
    while the order had to be decided blind.

    A reader changes it -- but only if it is asked. Vector-only conversations
    are *added* to what it reads rather than taking a word match's place, so
    one is kept only where the model prefers it to a word match it read beside
    it. Displacing word matches unconditionally was measured first and
    rejected: worth 2.5 points of top-1 on one sample of 120, and on a second
    it lost 0.8 of top-1, 3.4 of top-3 and 4.2 of top-8.

    How many to add was measured rather than guessed, on two samples of 120:

        added      0      2      4      8     16
        top-1   55.8   57.5   57.5   58.3   58.3   (sample A)
        top-3   67.5   69.2   69.2   70.8   70.0
        top-8   76.7   77.5   77.5   79.2   77.5
        top-1   57.5   57.5   57.5   58.3   59.2   (sample B)
        top-3   74.2   74.2   74.2   75.0   75.8
        top-8   77.5   77.5   77.5   79.2   79.2

    Eight is where both samples agree; sixteen is better on one and worse on
    the other and costs twice as much to read. It also recovers conversations
    no ordering had ever reached: the share found nowhere in the first fifty
    fell from 16.7% to 12.5% and 15.8%.
    """
    head = candidates[:RERANK_DEPTH]
    chosen = {item[0] for item in head}
    extra = [
        item
        for item in candidates
        if item[0] in meaning_only and item[0] not in chosen
    ][:MEANING_EXTRA]
    head = head + extra
    chosen = {item[0] for item in head}
    return (
        head,
        [item for item in candidates if item[0] not in chosen],
        {item[0] for item in extra},
    )


def _by_words(
    conn: sqlite3.Connection,
    question: str,
    pool: int,
    *,
    excluded_sources: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """Window ids by BM25, best first, with the snippet each one matched on."""
    match = fts_escape(question)
    if not match:
        return [], {}
    excluded = tuple(
        sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
    )
    source_clause = ""
    if excluded:
        source_clause = (
            " AND lower(w.source) NOT IN (" + ",".join("?" for _ in excluded) + ")"
        )
    rows = conn.execute(
        "SELECT window_id,"
        "       snippet(conversation_windows_fts, 1, '', '', '…', ?) AS excerpt"
        " FROM conversation_windows_fts"
        " JOIN conversation_windows w ON w.id = conversation_windows_fts.window_id"
        f" WHERE conversation_windows_fts MATCH ?{source_clause}"
        " ORDER BY bm25(conversation_windows_fts) LIMIT ?",
        (SNIPPET_TOKENS, match, *excluded, pool),
    ).fetchall()
    return (
        [row["window_id"] for row in rows],
        {row["window_id"]: row["excerpt"] for row in rows},
    )


def _by_meaning(
    conn: sqlite3.Connection, question: str, embedder: object | None, pool: int
) -> list[str]:
    """Window ids by vector distance, closest first. Empty without an embedder.

    A caller with no embedder is not a failure: the CLI's offline paths and
    every test that does not want a model land here, and the words index still
    answers them.
    """
    if embedder is None:
        return []
    from varyz.pipeline import window_vectors

    try:
        vector = embedder.embed_query(question)  # type: ignore[attr-defined]
    except Exception:
        # A missing or broken embedding model must not take the words index
        # down with it.
        return []
    return [window_id for window_id, _ in window_vectors.nearest(conn, vector, pool)]


def _window(conn: sqlite3.Connection, window_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT w.source, w.started_at, w.turns, l.label,"
        "       (SELECT entry_id FROM conversation_window_entries"
        "         WHERE window_id = w.id ORDER BY seq LIMIT 1) AS first_entry"
        " FROM conversation_windows w"
        " LEFT JOIN conversation_window_labels l ON l.window_id = w.id"
        " WHERE w.id = ?",
        (window_id,),
    ).fetchone()


def _excerpt(
    conn: sqlite3.Connection, window_id: str, excerpts: dict[str, str], max_chars: int
) -> str:
    """The matched snippet where there is one, the opening of the exchange else.

    A conversation found by meaning alone matched no word, so there is no
    snippet to centre on and nothing honest to point at but the beginning.
    """
    if window_id in excerpts:
        return excerpts[window_id][:max_chars]
    from varyz.pipeline.windows import window_text

    return window_text(conn, window_id)[:max_chars]


def _indexed(conn: sqlite3.Connection) -> bool:
    """Whether this archive has a conversation index at all.

    An archive older than the migration has no such table, and an archive that
    has never run the partition has an empty one. Neither is an error and
    neither is a question without an answer; both simply have nothing to add.
    """
    try:
        row = conn.execute("SELECT 1 FROM conversation_windows_fts LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None
