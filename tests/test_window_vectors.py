"""A conversation can be found by what it meant, not only by what it said.

The words index answers when the question repeats the exchange's words. These
tests are about the other half: a conversation retrieved with no word in
common, and the several ways that must still fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.pipeline.window_vectors import KNN_MAX, counts, embed_windows, nearest
from varyz.pipeline.windows import build_windows
from varyz.retrieval import conversation_evidence
from varyz.retrieval.rerank import RerankCalibration

START = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)


def _conversation(conn, thread: str, *bodies: str) -> str:
    with conn:
        for index, body in enumerate(bodies):
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=f"{thread}-{index}",
                    body=body,
                    occurred_start=START + timedelta(minutes=index),
                    thread_id=thread,
                    authored_by_user=bool(index % 2),
                ),
                source="ai_conversations",
                sensitivity=Sensitivity.SENSITIVE,
            )
    build_windows(conn)
    return conn.execute(
        "SELECT id FROM conversation_windows WHERE thread_id = ?", (thread,)
    ).fetchone()["id"]


def _label(conn, window_id: str, label: str = "personal") -> None:
    row = conn.execute(
        "SELECT content_hash FROM conversation_windows WHERE id = ?", (window_id,)
    ).fetchone()
    with conn:
        conn.execute(
            "INSERT INTO conversation_window_labels"
            " (window_id, label, agreed, votes_json, prompt_version, content_hash,"
            "  created_at)"
            " VALUES (?, ?, 1, '[]', 'window-v1', ?, '2026-05-04')",
            (window_id, label, row["content_hash"]),
        )


def test_a_conversation_is_embedded_once_and_not_again(conn, cfg, fake_embedder):
    _conversation(conn, "move", "when does the van arrive", "eight in the morning")

    first = embed_windows(conn, cfg, fake_embedder)
    second = embed_windows(conn, cfg, fake_embedder)

    assert first == 1
    assert second == 0
    assert counts(conn) == (1, 1)


def test_a_conversation_that_changed_is_embedded_again(conn, cfg, fake_embedder):
    window_id = _conversation(conn, "move", "when does the van arrive", "eight tomorrow")
    embed_windows(conn, cfg, fake_embedder)

    # The exchange continued. The stored vector describes messages that are no
    # longer all of it, which is the same thing as being wrong.
    window_id = _conversation(
        conn, "move", "when does the van arrive", "eight tomorrow", "bring cash"
    )

    assert embed_windows(conn, cfg, fake_embedder) == 1
    stored = conn.execute(
        "SELECT content_hash FROM conversation_window_chunks WHERE window_id = ?",
        (window_id,),
    ).fetchone()["content_hash"]
    current = conn.execute(
        "SELECT content_hash FROM conversation_windows WHERE id = ?", (window_id,)
    ).fetchone()["content_hash"]
    assert stored == current


def test_the_nearest_conversation_to_its_own_words_is_itself(conn, cfg, fake_embedder):
    window_id = _conversation(conn, "move", "when does the van arrive", "eight tomorrow")
    _conversation(conn, "vet", "did the cat eat anything", "half a tin, no more")
    embed_windows(conn, cfg, fake_embedder)

    text = conn.execute(
        "SELECT text FROM conversation_window_chunks WHERE window_id = ?", (window_id,)
    ).fetchone()["text"]
    found = nearest(conn, fake_embedder.embed_query(text), 5)

    assert found[0][0] == window_id


def test_one_conversation_is_one_result_however_long_it_is(conn, cfg, fake_embedder):
    # Long enough to be cut into several chunks at a tiny chunk size, so a
    # conversation with more chances to be close cannot win by having them.
    cfg.embedding.chunk_tokens = 8
    window_id = _conversation(
        conn, "move", *[f"message number {index} about the van" for index in range(12)]
    )
    embed_windows(conn, cfg, fake_embedder)

    chunks, embedded = counts(conn)
    assert chunks > 1
    assert embedded == chunks
    found = nearest(conn, fake_embedder.embed_query("message number 3 about the van"), 5)
    assert [window_id for window_id, _ in found].count(window_id) == 1


def test_a_conversation_found_only_by_meaning_still_answers(conn, cfg, fake_embedder):
    window_id = _conversation(
        conn, "move", "when does the van arrive", "eight in the morning"
    )
    _label(conn, window_id)
    embed_windows(conn, cfg, fake_embedder)
    text = conn.execute(
        "SELECT text FROM conversation_window_chunks WHERE window_id = ?", (window_id,)
    ).fetchone()["text"]

    # A question with no word in common with the exchange: the words index
    # returns nothing and the vector is the only thing that can answer.
    found, withheld = conversation_evidence.capsules(
        conn, text, limit=3, max_chars=400, embedder=fake_embedder
    )

    assert [capsule.external_id for capsule in found] == [window_id]
    assert withheld == 0
    assert found[0].text


def test_meaning_does_not_open_a_conversation_the_model_kept_shut(
    conn, cfg, fake_embedder
):
    window_id = _conversation(
        conn, "clinic", "how did the appointment go", "they want to run it again"
    )
    _label(conn, window_id, "sensitive")
    embed_windows(conn, cfg, fake_embedder)
    text = conn.execute(
        "SELECT text FROM conversation_window_chunks WHERE window_id = ?", (window_id,)
    ).fetchone()["text"]

    found, withheld = conversation_evidence.capsules(
        conn, text, limit=3, max_chars=400, embedder=fake_embedder
    )

    assert found == []
    assert withheld == 1


def test_meaning_does_not_open_a_conversation_nobody_has_read(conn, cfg, fake_embedder):
    _conversation(conn, "move", "when does the van arrive", "eight in the morning")
    embed_windows(conn, cfg, fake_embedder)
    text = conn.execute("SELECT text FROM conversation_window_chunks").fetchone()["text"]

    found, withheld = conversation_evidence.capsules(
        conn, text, limit=3, max_chars=400, embedder=fake_embedder
    )

    assert found == []
    assert withheld == 1


def test_without_an_embedder_the_words_still_answer(conn, cfg, fake_embedder):
    window_id = _conversation(
        conn, "move", "when does the van arrive on saturday", "eight in the morning"
    )
    _label(conn, window_id)
    embed_windows(conn, cfg, fake_embedder)

    found, _ = conversation_evidence.capsules(conn, "van saturday", limit=3, max_chars=400)

    assert [capsule.external_id for capsule in found] == [window_id]


def test_an_archive_with_no_vectors_answers_by_words_alone(conn, fake_embedder):
    window_id = _conversation(
        conn, "move", "when does the van arrive on saturday", "eight in the morning"
    )
    _label(conn, window_id)

    # Nothing embedded: the vector half returns nothing and must not take the
    # words half down with it.
    found, _ = conversation_evidence.capsules(
        conn, "van saturday", limit=3, max_chars=400, embedder=fake_embedder
    )

    assert [capsule.external_id for capsule in found] == [window_id]


def test_a_request_for_more_neighbours_than_the_index_serves_still_answers(
    conn, cfg, fake_embedder
):
    _conversation(conn, "deep", "when does the van arrive", "eight in the morning")
    embed_windows(conn, cfg, fake_embedder)

    found = nearest(conn, fake_embedder.embed_query("van"), KNN_MAX * 2)

    # Clamped, not refused: sqlite-vec raises above its own k, and this path
    # turns any such error into "the archive has nothing", which would delete
    # the vector arm on precisely the deepest queries.
    assert [window_id for window_id, _ in found] != []


class _PointsAt:
    """An embedder that answers every question with one conversation's vector."""

    model_name = "fake-embedder-384"

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed_query(self, text: str) -> list[float]:
        return self.vector


MARK = "fourteenth"


class _Reader:
    """A cross-encoder that scores by what it is reading, and records it.

    Scoring by position would be scoring by whatever order the slate happened
    to come out in, which is the thing under test; this reader recognises the
    one conversation the words cannot reach and prices it deliberately.
    """

    def __init__(self, *, worded: float = 0.0, extra: float = 0.0) -> None:
        self.worded = worded
        self.extra = extra
        self.passages: list[str] = []

    def score(self, question: str, passages: list[str]) -> list[float]:
        self.passages.extend(passages)
        return [self.extra if MARK in text else self.worded for text in passages]



def _crowded(conn, cfg, fake_embedder) -> tuple[list[str], str, _PointsAt]:
    """A full slate of word matches, and one conversation only the vectors find.

    The reading has to be full for the added conversations to be added to
    anything; with two candidates the reader simply reads both, which tests
    nothing about the slate.
    """
    worded = [
        _conversation(
            conn, f"van{index}", "when does the van arrive", f"eight, number {index}"
        )
        for index in range(conversation_evidence.RERANK_DEPTH)
    ]
    meant = _conversation(conn, "sofa", "did they confirm a slot", f"yes, the {MARK}")
    for window_id in [*worded, meant]:
        _label(conn, window_id)
    embed_windows(conn, cfg, fake_embedder)
    text = conn.execute(
        "SELECT text FROM conversation_window_chunks WHERE window_id = ?", (meant,)
    ).fetchone()["text"]
    return worded, meant, _PointsAt(fake_embedder.embed_query(text))


def test_the_reader_is_shown_a_conversation_only_the_vectors_found(conn, cfg, fake_embedder):
    worded, meant, pointing = _crowded(conn, cfg, fake_embedder)
    # The word matches fill the reading; without being added, the one the words
    # cannot reach at all sits below every one of them and is never seen.
    reader = _Reader(extra=1.0)

    found, _ = conversation_evidence.capsules(
        conn, "van arrive", limit=2, max_chars=400, embedder=pointing, reranker=reader
    )

    assert len(reader.passages) == conversation_evidence.RERANK_DEPTH + 1
    assert len([text for text in reader.passages if MARK in text]) == 1
    assert [capsule.external_id for capsule in found][0] == meant
    assert len(worded) == conversation_evidence.RERANK_DEPTH


def test_a_conversation_the_reader_does_not_prefer_takes_no_ones_place(
    conn, cfg, fake_embedder
):
    worded, meant, pointing = _crowded(conn, cfg, fake_embedder)
    reader = _Reader(extra=-1.0)

    found, _ = conversation_evidence.capsules(
        conn, "van arrive", limit=2, max_chars=400, embedder=pointing, reranker=reader
    )

    # Added to the reading, not into the answer: the word matches keep the only
    # slot there was, because the reader preferred one of them.
    assert [capsule.external_id for capsule in found][0] in worded
    assert meant is not None


class _Calibrated(_Reader):
    """A reader whose scores mean something, the way the real one's do."""

    calibration = RerankCalibration(margin=4.0, confident=-6.0, floor=-8.0)


def test_an_added_conversation_below_the_floor_does_not_lead_the_answer(
    conn, cfg, fake_embedder
):
    worded, meant, pointing = _crowded(conn, cfg, fake_embedder)
    # It outscores every word match, and they are all far below the floor:
    # the model saying it has no opinion, not that this is the answer.
    reader = _Calibrated(worded=-30.0, extra=-20.0)

    found, _ = conversation_evidence.capsules(
        conn, "van arrive", limit=2, max_chars=400, embedder=pointing, reranker=reader
    )

    assert [capsule.external_id for capsule in found][0] in worded
    assert meant is not None


def test_an_added_conversation_far_behind_the_best_does_not_lead_either(
    conn, cfg, fake_embedder
):
    worded, meant, pointing = _crowded(conn, cfg, fake_embedder)
    # Above the floor, but noise next to what the reader did find.
    reader = _Calibrated(worded=0.0, extra=-6.0)

    found, _ = conversation_evidence.capsules(
        conn, "van arrive", limit=2, max_chars=400, embedder=pointing, reranker=reader
    )

    assert [capsule.external_id for capsule in found][0] in worded
    assert meant is not None


def test_the_floor_judges_only_what_the_vectors_added(conn, cfg, fake_embedder):
    worded, meant, pointing = _crowded(conn, cfg, fake_embedder)
    # Every word match is below the floor too, and they keep their places:
    # they are where retrieval put them, and this is not a filter on evidence.
    reader = _Calibrated(worded=-30.0, extra=-40.0)

    found, _ = conversation_evidence.capsules(
        conn,
        "van arrive",
        limit=conversation_evidence.RERANK_DEPTH + 1,
        max_chars=400,
        embedder=pointing,
        reranker=reader,
    )

    assert len(found) == conversation_evidence.RERANK_DEPTH + 1
    assert [capsule.external_id for capsule in found][-1] == meant
    assert set(worded) == {capsule.external_id for capsule in found[:-1]}
