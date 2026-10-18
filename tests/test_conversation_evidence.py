"""A conversation reaches an agent only once a local model has cleared it.

The gate under test is the one that decides whether somebody's chat history
leaves the machine, so most of these assert about refusal: an unread
conversation is refused, a sensitive one is refused, and neither refusal is
allowed to look like an archive that simply had nothing to say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.pipeline.windows import build_windows
from varyz.retrieval import conversation_evidence
from varyz.retrieval.answer import brief, recall

START = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)

TURNS = [
    (False, "are we still on for the climbing gym on saturday morning"),
    (True, "yes, booked the eleven o'clock slot for climbing, bring your shoes"),
]


def _window(conn, thread: str = "gym") -> str:
    for index, (authored, body) in enumerate(TURNS):
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=f"{thread}-{index}",
                body=body,
                occurred_start=START + timedelta(minutes=index),
                thread_id=thread,
                authored_by_user=authored,
            ),
            source="ai_conversations",
            # The fail-closed source default every chat message arrives with,
            # and the reason this path has to exist at all.
            sensitivity=Sensitivity.SENSITIVE,
        )
    build_windows(conn)
    return conn.execute(
        "SELECT id FROM conversation_windows WHERE thread_id = ?", (thread,)
    ).fetchone()["id"]


def _label(conn, window_id: str, label: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO conversation_window_labels"
            " (window_id, label, agreed, votes_json, prompt_version, content_hash, created_at)"
            " VALUES (?, ?, 1, '[]', 'window-v1', 'h', '2026-05-06')",
            (window_id, label),
        )


def test_a_conversation_the_model_called_personal_can_be_quoted(conn):
    window_id = _window(conn)
    _label(conn, window_id, "personal")

    found, withheld = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )

    assert [capsule.external_id for capsule in found] == [window_id]
    assert "climbing" in found[0].text
    assert withheld == 0


def test_both_sides_of_the_exchange_come_back_not_one_message(conn):
    _label(conn, _window(conn), "personal")

    found, _ = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )

    assert "are we still on" in found[0].text
    assert "booked the eleven" in found[0].text


def test_a_conversation_the_model_called_sensitive_stays_shut(conn):
    _label(conn, _window(conn), "sensitive")

    found, withheld = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )

    assert found == []
    assert withheld == 1


def test_a_conversation_nobody_has_read_yet_stays_shut(conn):
    _window(conn)

    found, withheld = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )

    assert found == []
    # Reported, not silently dropped: the archive has an answer the caller is
    # not allowed to read, which is a different thing from having none.
    assert withheld == 1


def test_a_caller_allowed_sensitive_material_sees_a_sensitive_conversation(conn):
    _label(conn, _window(conn), "sensitive")

    found, withheld = conversation_evidence.capsules(
        conn,
        "climbing gym saturday",
        limit=5,
        max_chars=400,
        allowed=(Sensitivity.PUBLIC, Sensitivity.PERSONAL, Sensitivity.SENSITIVE),
    )

    assert len(found) == 1
    assert withheld == 0


def test_a_withheld_conversation_does_not_take_a_cleared_ones_place(conn):
    _label(conn, _window(conn, thread="gym"), "sensitive")
    cleared = _window(conn, thread="gym2")
    _label(conn, cleared, "personal")

    found, withheld = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=1, max_chars=400
    )

    assert [capsule.external_id for capsule in found] == [cleared]
    assert withheld == 1


def test_a_capsule_addresses_the_conversation_not_one_line_of_it(conn):
    window_id = _window(conn)
    _label(conn, window_id, "personal")

    found, _ = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )
    payload = found[0].as_dict()

    assert payload["reference"] == window_id
    assert payload["kind"] == "conversation"
    # Both sides are in the text, so varyz cannot call any of it the owner's
    # own words.
    assert payload["provenance"] == "external"


def test_a_rebuilt_partition_leaves_no_index_entry_behind(conn):
    window_id = _window(conn)
    _label(conn, window_id, "personal")

    with conn:
        conn.execute("DELETE FROM entries")
    build_windows(conn)

    assert conn.execute("SELECT count(*) FROM conversation_windows_fts").fetchone()[0] == 0
    assert conversation_evidence.capsules(conn, "climbing", limit=5, max_chars=400) == ([], 0)


def test_recall_offers_a_cleared_conversation_alongside_its_passages(conn, cfg):
    _label(conn, _window(conn), "personal")

    evidence = recall(conn, cfg, "climbing gym saturday", limit=8, max_chars=400)

    assert [
        capsule.kind for capsule in evidence.capsules if capsule.kind == "conversation"
    ] == ["conversation"]
    assert evidence.abstained is False


def test_recall_counts_a_conversation_it_may_not_read_as_withheld(conn, cfg):
    _label(conn, _window(conn), "sensitive")

    evidence = recall(conn, cfg, "climbing gym saturday", limit=8, max_chars=400)

    assert [capsule for capsule in evidence.capsules if capsule.kind == "conversation"] == []
    assert evidence.withheld_sensitive >= 1


def test_a_brief_before_a_task_includes_what_was_agreed_over_chat(conn, cfg):
    _label(conn, _window(conn), "personal")

    evidence = brief(conn, cfg, "plan the climbing trip on saturday", limit=8, max_chars=400)

    # The arrangement was made in a chat, so a brief that reads only the
    # passage index briefs an agent on everything except the arrangement.
    assert [
        capsule.kind for capsule in evidence.capsules if capsule.kind == "conversation"
    ] == ["conversation"]
    assert evidence.abstained is False


def test_a_brief_does_not_open_a_conversation_the_model_kept_shut(conn, cfg):
    _label(conn, _window(conn), "sensitive")

    evidence = brief(conn, cfg, "plan the climbing trip on saturday", limit=8, max_chars=400)

    assert [capsule for capsule in evidence.capsules if capsule.kind == "conversation"] == []
    assert evidence.withheld_sensitive >= 1


def test_a_brief_offers_one_conversation_once_however_many_clauses_match(conn, cfg):
    window_id = _window(conn)
    _label(conn, window_id, "personal")

    evidence = brief(
        conn, cfg, "book the climbing gym and bring the shoes on saturday",
        limit=8, max_chars=400,
    )

    offered = [
        capsule.external_id
        for capsule in evidence.capsules
        if capsule.kind == "conversation"
    ]
    assert offered == [window_id]


def test_an_archive_partitioned_before_the_index_existed_is_backfilled(conn):
    window_id = _window(conn)
    _label(conn, window_id, "personal")
    with conn:
        conn.execute("DELETE FROM conversation_windows_fts")

    assert conversation_evidence.capsules(conn, "climbing", limit=5, max_chars=400) == ([], 0)

    stats = build_windows(conn)

    assert stats["indexed"] == 1
    assert stats["added"] == 0
    found, _ = conversation_evidence.capsules(conn, "climbing", limit=5, max_chars=400)
    assert [capsule.external_id for capsule in found] == [window_id]


def test_conversations_take_only_a_few_slots_when_passages_fill_the_rest(conn, cfg):
    _label(conn, _window(conn), "personal")
    for index in range(12):
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=f"note-{index}",
                body=f"climbing gym notes, saturday session number {index}",
                occurred_start=START + timedelta(days=index),
            ),
            source="manual",
            sensitivity=Sensitivity.PERSONAL,
        )

    evidence = recall(conn, cfg, "climbing gym saturday", limit=8, max_chars=400)

    spoken = [capsule for capsule in evidence.capsules if capsule.kind == "conversation"]
    assert len(evidence.capsules) == 8
    assert 0 < len(spoken) <= 3


class StubReranker:
    """A cross-encoder that scores from a script and records what it read."""

    # No calibration, so `recall` leaves its own passage path alone and this
    # measures the conversation channel by itself.
    calibration = None

    def __init__(self, *scores: float, fails: bool = False) -> None:
        self.scores = list(scores)
        self.fails = fails
        self.passages: list[str] = []

    def score(self, question: str, passages: list[str]) -> list[float]:
        self.passages.extend(passages)
        if self.fails:
            raise RuntimeError("the model did not load")
        return self.scores[: len(passages)]


def test_a_second_look_can_move_a_conversation_the_words_ranked_lower(conn):
    first = _window(conn, thread="gym")
    _label(conn, first, "personal")
    second = _window(conn, thread="gym2")
    _label(conn, second, "personal")
    worded, _ = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )
    # The reranker prefers whichever the words put second.
    reranker = StubReranker(0.1, 0.9)

    found, _ = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400, reranker=reranker
    )

    assert [capsule.external_id for capsule in found] == [
        capsule.external_id for capsule in reversed(worded)
    ]
    # It scored the exchange itself, not the snippet the words matched on.
    assert any("booked the eleven" in passage for passage in reranker.passages)


def test_a_second_look_never_reads_a_conversation_the_label_keeps_shut(conn):
    shut = _window(conn, thread="gym")
    _label(conn, shut, "sensitive")
    cleared = _window(conn, thread="gym2")
    _label(conn, cleared, "personal")
    reranker = StubReranker(0.9)

    found, withheld = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400, reranker=reranker
    )

    assert [capsule.external_id for capsule in found] == [cleared]
    assert withheld == 1
    # One candidate survived the gate, so there was nothing to reorder and the
    # shut conversation was never handed to the model in the first place.
    assert reranker.passages == []


def test_a_second_look_that_fails_leaves_the_first_stage_order_alone(conn):
    _label(conn, _window(conn, thread="gym"), "personal")
    _label(conn, _window(conn, thread="gym2"), "personal")
    worded, _ = conversation_evidence.capsules(
        conn, "climbing gym saturday", limit=5, max_chars=400
    )

    found, withheld = conversation_evidence.capsules(
        conn,
        "climbing gym saturday",
        limit=5,
        max_chars=400,
        reranker=StubReranker(fails=True),
    )

    assert [capsule.external_id for capsule in found] == [
        capsule.external_id for capsule in worded
    ]
    assert withheld == 0


def test_a_second_look_reorders_only_what_the_caller_would_have_been_shown(conn):
    for index in range(conversation_evidence.RERANK_DEPTH + 2):
        _label(conn, _window(conn, thread=f"gym{index}"), "personal")
    reranker = StubReranker(*range(conversation_evidence.RERANK_DEPTH))

    found, _ = conversation_evidence.capsules(
        conn,
        "climbing gym saturday",
        limit=conversation_evidence.RERANK_DEPTH + 2,
        max_chars=400,
        reranker=reranker,
    )

    # Deeper than the reranker reads, nothing is scored and nothing moves up
    # past what was: a conversation cannot be promoted out of the tail on a
    # judgement nobody made.
    assert len(reranker.passages) == conversation_evidence.RERANK_DEPTH
    assert len(found) == conversation_evidence.RERANK_DEPTH + 2


def test_recall_gives_the_conversation_channel_the_same_second_look(conn, cfg):
    _label(conn, _window(conn, thread="gym"), "personal")
    _label(conn, _window(conn, thread="gym2"), "personal")
    worded = recall(conn, cfg, "climbing gym saturday", limit=5)
    reranker = StubReranker(0.1, 0.9)

    answer = recall(conn, cfg, "climbing gym saturday", limit=5, reranker=reranker)

    spoken = [capsule.external_id for capsule in answer.capsules if capsule.kind == "conversation"]
    assert spoken == [
        capsule.external_id
        for capsule in reversed([c for c in worded.capsules if c.kind == "conversation"])
    ]
