"""Per-entry extraction reuse across graph generations.

Appending one entry names a new generation. These tests pin the rule that a new
name is not a reason to re-read the archive, and — more importantly — the four
things that *are*: the entry's text, its sensitivity label, the conversation
window an extractor reads around it, and the identity of the extractor itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.conftest import FakeLLM
from varyz.db import dao
from varyz.deletion import forget_entry
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.graph.cache import (
    SELF_ONLY,
    ExtractionCache,
    WindowFingerprints,
    cache_scope,
    window_bucket,
)
from varyz.graph.extract import DeterministicExtractor
from varyz.graph.semantic import ConversationWindowReader
from varyz.graph.service import build_extraction_cache, rebuild_graph

FACET_REPLY = """{"facets":[{"target_id":"%s","kind":"belief",
"statement":"Privacy matters more than convenience.",
"evidence_quote":"I think privacy matters more than convenience.",
"supporting_context_ids":[],"confidence":0.95}]}"""


def _note(conn, external_id: str, body: str, *, sensitivity=Sensitivity.PERSONAL):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=external_id,
                body=body,
                captured_at=datetime(2026, 3, 1, 12, tzinfo=UTC),
            ),
            source="manual",
            sensitivity=sensitivity,
        )
    return dao.get_entry(conn, entry_id)


def _message(
    conn,
    external_id: str,
    text: str,
    *,
    sender: str = "me",
    when: datetime,
    thread: str = "wa:test",
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=external_id,
                body=f"From: {sender}\nChat: Test\n\n{text}",
                occurred_start=when,
                thread_id=thread,
            ),
            source="whatsapp",
            sensitivity=sensitivity,
        )
    return dao.get_entry(conn, entry_id)


def _deterministic(cfg):
    cfg.graph.extractor = "deterministic"
    return cfg


def _claims(conn):
    return [
        (row["predicate"], row["object_surface"])
        for row in conn.execute(
            """SELECT c.predicate, c.object_surface
               FROM claims c JOIN graph_runs r ON r.id = c.graph_generation_id
               WHERE r.state='active' ORDER BY c.predicate, c.object_surface"""
        )
    ]


# --- the archive stops being re-read -----------------------------------------


def test_appending_one_entry_re_extracts_only_that_entry(conn, cfg):
    _deterministic(cfg)
    for index in range(4):
        _note(conn, f"old-{index}", f"I moved to Berlin in 201{index}.")
    first = rebuild_graph(conn, cfg, version="1")
    assert first.entries_reused == 0

    _note(conn, "new", "I moved to Lisbon in 2026.")
    second = rebuild_graph(conn, cfg, version="1")

    # A new generation, because the corpus changed — but only the new entry was
    # read to build it.
    assert second.generation_key != first.generation_key
    assert second.entries_processed == 5
    assert second.entries_reused == 4


def test_reused_extraction_produces_the_same_graph(conn, cfg):
    _deterministic(cfg)
    _note(conn, "one", "I moved to Berlin in 2019.")
    _note(conn, "two", "I decided to learn Portuguese.")
    rebuild_graph(conn, cfg, version="1")
    before = _claims(conn)

    _note(conn, "three", "The weather is fine.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 2
    assert _claims(conn) == before


def test_cache_is_off_when_configured_off(conn, cfg):
    _deterministic(cfg)
    cfg.graph.extraction_cache = False
    _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")

    _note(conn, "two", "I moved to Lisbon in 2026.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 0
    assert not conn.execute("SELECT 1 FROM graph_extraction_cache").fetchall()


# --- what must invalidate ----------------------------------------------------


def test_edited_text_is_read_again(conn, cfg):
    _deterministic(cfg)
    _note(conn, "one", "I moved to Berlin in 2019.")
    _note(conn, "two", "I decided to learn Portuguese.")
    rebuild_graph(conn, cfg, version="1")

    _note(conn, "two", "I decided to learn Greek.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 1
    assert ("decided_to", "learn Greek") in _claims(conn)


def test_relabelled_sensitivity_is_read_again(conn, cfg):
    _deterministic(cfg)
    entry = _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")

    # Sensitivity decides which broker — and therefore which model — may read an
    # entry at all, and it changes without the text changing.
    with conn:
        conn.execute(
            "UPDATE entries SET sensitivity=? WHERE id=?",
            (Sensitivity.SENSITIVE.value, entry.id),
        )
    _note(conn, "two", "I moved to Lisbon in 2026.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 0


def test_a_different_extractor_version_reads_everything_again(conn, cfg):
    _deterministic(cfg)
    _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")

    result = rebuild_graph(conn, cfg, version="2")

    assert result.entries_reused == 0


def test_changed_neighbour_invalidates_its_conversation_window(conn, cfg):
    when = datetime(2026, 4, 1, 9, tzinfo=UTC)
    _message(conn, "ctx", "What do you make of the privacy stuff?", sender="Anna", when=when)
    target = _message(
        conn,
        "target",
        "I think privacy matters more than convenience.",
        when=when + timedelta(minutes=1),
    )
    cfg.graph.extractor = "contextual_facets"
    provider = FakeLLM(FACET_REPLY % target.id)
    rebuild_graph(conn, cfg, version="1", provider=provider)
    calls = len(provider.calls)
    assert calls

    # Same target text, same day, same thread — the neighbour changed.
    _message(conn, "ctx", "Actually, forget what I said.", sender="Anna", when=when)
    result = rebuild_graph(conn, cfg, version="1", provider=provider)

    assert result.entries_reused == 0
    assert len(provider.calls) > calls


def test_an_unrelated_conversation_does_not_invalidate(conn, cfg):
    when = datetime(2026, 4, 1, 9, tzinfo=UTC)
    target = _message(
        conn,
        "target",
        "I think privacy matters more than convenience.",
        when=when,
    )
    cfg.graph.extractor = "contextual_facets"
    provider = FakeLLM(FACET_REPLY % target.id)
    rebuild_graph(conn, cfg, version="1", provider=provider)
    calls = len(provider.calls)

    # Another thread, and the same thread on another day: neither is a window
    # the extractor may read around the target.
    _message(conn, "other-thread", "Different chat entirely.", when=when, thread="wa:other")
    _message(conn, "other-day", "Same chat, next week.", when=when + timedelta(days=7))
    result = rebuild_graph(conn, cfg, version="1", provider=provider)

    assert result.entries_reused == 1
    # Neither new entry carries a cue, and the target's answer was already
    # paid for: the whole rebuild costs no model call at all.
    assert len(provider.calls) == calls


def test_window_bucket_covers_everything_session_around_reads(conn, cfg):
    """The fingerprint must be at least as wide as the window it protects."""
    when = datetime(2026, 5, 4, 20, tzinfo=UTC)
    for index in range(6):
        _message(conn, f"m{index}", f"Message {index}.", when=when + timedelta(minutes=index))
    _message(conn, "other", "Elsewhere.", when=when, thread="wa:other")
    _message(conn, "tomorrow", "Later.", when=when + timedelta(days=1))
    target = dao.get_entry(
        conn,
        conn.execute("SELECT id FROM entries WHERE external_id='m3'").fetchone()["id"],
    )

    session = ConversationWindowReader(conn).session_around(target, max_messages=160)

    assert len(session) >= 6
    assert all(window_bucket(record) == window_bucket(target) for record in session)


def test_an_entry_with_no_thread_has_no_window_to_fingerprint(conn, cfg):
    entry = _note(conn, "doc", "I think privacy matters.")

    assert window_bucket(entry) is None
    assert WindowFingerprints(conn).of(entry) == SELF_ONLY


# --- the stored answer -------------------------------------------------------


def _staged(conn):
    """Everything a claim carries that the cache had to store and restore."""
    return [
        tuple(row)
        for row in conn.execute(
            """SELECT c.predicate, c.object_surface, c.modality, c.polarity,
                      c.observed_at, c.valid_from, c.valid_to,
                      c.temporal_precision, c.confidence, c.origin,
                      e.quote, e.char_start, e.char_end, e.evidence_role
               FROM claims c JOIN claim_evidence e ON e.claim_id = c.id
               JOIN graph_runs r ON r.id = c.graph_generation_id
               WHERE r.state='active'
               ORDER BY c.predicate, c.object_surface, e.char_start"""
        )
    ]


def test_cached_claims_survive_the_round_trip_intact(conn, cfg):
    _deterministic(cfg)
    _note(conn, "one", "I moved to Berlin in 2019.")
    _note(conn, "two", "I decided to learn Portuguese in March.")
    rebuild_graph(conn, cfg, version="1")
    before = _staged(conn)
    assert before

    _note(conn, "three", "Nothing to see here.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 2
    assert _staged(conn) == before


def test_an_unreadable_cached_row_is_a_miss_not_a_crash(conn, cfg):
    _deterministic(cfg)
    _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")
    with conn:
        conn.execute("UPDATE graph_extraction_cache SET batch_json='{oops'")

    _note(conn, "two", "I moved to Lisbon in 2026.")
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == 0
    assert ("moved_to", "Berlin") in _claims(conn)


def test_forgetting_an_entry_forgets_what_was_extracted_from_it(conn, cfg, vault):
    _deterministic(cfg)
    entry = _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")
    assert conn.execute("SELECT count(*) FROM graph_extraction_cache").fetchone()[0]

    forget_entry(conn, vault, entry.id)

    assert not conn.execute(
        "SELECT 1 FROM graph_extraction_cache WHERE entry_id=?", (entry.id,)
    ).fetchall()


def test_sweep_keeps_the_current_scope_and_drops_abandoned_ones(conn, cfg):
    _deterministic(cfg)
    entry = _note(conn, "one", "I moved to Berlin in 2019.")
    rebuild_graph(conn, cfg, version="1")
    with conn:
        conn.execute(
            """INSERT INTO graph_extraction_cache
               (cache_key, entry_id, scope, content_hash, window_fingerprint,
                batch_json, created_at, last_used_at)
               VALUES ('stale', ?, 'retired-extractor', 'h', '-', '{}',
                       '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')""",
            (entry.id,),
        )

    cache = build_extraction_cache(conn, cfg, DeterministicExtractor(), "1")
    dropped = cache.sweep()

    assert dropped == 1
    scopes = {
        row["scope"] for row in conn.execute("SELECT scope FROM graph_extraction_cache")
    }
    assert "retired-extractor" not in scopes
    assert scopes


def test_scope_changes_with_any_setting_that_changes_the_reading():
    base = dict(
        extractor_name="contextual_facets",
        extractor_version="7",
        model="gemma",
        prompt_version="semantic-facets-v4",
    )
    reference = cache_scope(**base, parameters={"batch_max_targets": 20})

    assert cache_scope(**base, parameters={"batch_max_targets": 20}) == reference
    assert cache_scope(**base, parameters={"batch_max_targets": 8}) != reference
    assert cache_scope(**{**base, "model": "other"}, parameters={}) != reference


def test_cache_counts_hits_and_misses(conn, cfg):
    entry = _note(conn, "one", "I moved to Berlin in 2019.")
    cache = ExtractionCache(conn, scope="scope-1")

    assert cache.get(entry) is None
    assert (cache.hits, cache.misses) == (0, 1)

    batch = DeterministicExtractor().extract(entry)
    cache.put(entry, batch)
    cache.flush()
    restored = cache.get(entry)

    assert cache.hits == 1
    assert restored is not None
    assert [claim.object_surface for claim in restored.claims] == [
        claim.object_surface for claim in batch.claims
    ]
