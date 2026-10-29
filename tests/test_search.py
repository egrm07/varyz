"""Hybrid retrieval over a small ingested corpus with the fake embedder."""

from pathlib import Path

import pytest

from varyz.config import (
    MIN_CORPUS_VECTORS_FOR_ABSTENTION,
    MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE,
)
from varyz.connectors.manual import ManualConnector
from varyz.pipeline.embed import (
    Embedder,
    _prefer_copies_to_symlinks,
    _quiet_hub_notices,
    chunk_recipe,
    chunk_text,
    embed_pending,
)
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.retrieval.search import (
    _identifier_candidates,
    _looks_like_identifier,
    corpus_vectors,
    hybrid_search,
    is_keyword_query,
    is_supported,
    keyword_search,
    lead_snippet,
    lexically_grounded,
    names_the_archive_has_never_seen,
)

TEXTS = [
    "Ran into Lena at the gym, she's moving to Berlin in September.",
    "Finished reading the Dostoevsky novel on the train home.",
    "Dad called about fixing the cabin roof before winter.",
]


def _seed(conn, vault, cfg, fake_embedder):
    connector = ManualConnector()
    gate = SensitivityGate(cfg.sensitivity)
    for text in TEXTS:
        ingest_item(conn, vault, connector, gate, ManualConnector.make_item(text))
    embed_pending(conn, cfg, fake_embedder)


def test_keyword_search_finds_exact_terms(conn, vault, cfg, fake_embedder):
    _seed(conn, vault, cfg, fake_embedder)
    ids, snippets = keyword_search(conn, "cabin roof", limit=10)
    assert len(ids) == 1
    assert "cabin" in snippets[ids[0]]


def test_fts_query_with_special_chars_does_not_crash(conn, vault, cfg, fake_embedder):
    _seed(conn, vault, cfg, fake_embedder)
    ids, _ = keyword_search(conn, 'cabin "roof" AND (weird* NOT', limit=10)
    assert isinstance(ids, list)


def test_hybrid_exact_text_ranks_first(conn, vault, cfg, fake_embedder):
    _seed(conn, vault, cfg, fake_embedder)
    hits = hybrid_search(conn, TEXTS[1], fake_embedder, limit=3)
    assert hits
    assert "Dostoevsky" in hits[0].entry.body


def test_hybrid_works_without_embedder(conn, vault, cfg, fake_embedder):
    _seed(conn, vault, cfg, fake_embedder)
    hits = hybrid_search(conn, "Berlin", None, limit=5)
    assert len(hits) == 1
    assert "Lena" in hits[0].entry.body


def test_excluded_vector_hits_are_removed_before_hybrid_fusion(
    conn, vault, cfg, fake_embedder
):
    _seed(conn, vault, cfg, fake_embedder)
    conn.execute(
        "UPDATE entries SET source='whatsapp' WHERE body LIKE '%Dostoevsky%'"
    )
    conn.commit()

    hits = hybrid_search(
        conn,
        TEXTS[1],
        fake_embedder,
        limit=3,
        excluded_sources=("whatsapp", "telegram"),
    )

    assert all(hit.entry.source != "whatsapp" for hit in hits)
    assert all("Dostoevsky" not in hit.entry.body for hit in hits)
    assert not is_supported(
        conn,
        "Dostoevsky",
        fake_embedder,
        excluded_sources=("whatsapp", "telegram"),
    )


def test_an_excerpt_matched_only_by_meaning_says_where_it_was_cut(
    conn, vault, cfg, fake_embedder
):
    """A hit with no keyword window was shown as a hard slice of its chunk.

    There is nothing to centre on, so the excerpt is the opening of the chunk
    -- and it was cut at the two-hundredth character, mid-word, with nothing
    to mark the cut. The reader cannot tell that from a document that stops
    there, which is the one thing an archive of quotations must not get wrong.
    """
    connector = ManualConnector()
    gate = SensitivityGate(cfg.sensitivity)
    long_text = (
        "The ledger migration is the piece nobody wants to own, and every "
        "quarter it moves one column further right on the board while the "
        "vendor timeline slips again and the reconciliation lands in the "
        "same week as the audit, which is how it has gone three years running."
    )
    assert len(long_text) > 240
    ingest_item(conn, vault, connector, gate, ManualConnector.make_item(long_text))
    embed_pending(conn, cfg, fake_embedder)

    # A word the archive has never seen: nothing matches by keyword, so there
    # is no window to centre the excerpt on and the opening of the chunk has
    # to stand in for it.
    hits = hybrid_search(conn, "bookkeeping", fake_embedder, limit=5)
    hit = next(h for h in hits if long_text in h.entry.body)
    assert hit.match_reasons == ("similar meaning",)
    assert hit.snippet

    assert hit.snippet.endswith("…"), hit.snippet
    quoted = hit.snippet[:-1]
    assert quoted in hit.chunk
    # Cut between words: the archive's next character is a space, not the rest
    # of the word the excerpt stopped inside.
    assert hit.chunk[len(quoted)] == " ", hit.snippet


def test_lead_snippet_cuts_at_a_word_and_admits_it():
    """The cut is marked, and lands between words rather than inside one."""
    short = "A sentence that fits."
    assert lead_snippet(short) == short

    long_text = "word " * 200
    cut = lead_snippet(long_text)
    assert cut.endswith("\u2026")
    assert len(cut) <= 201
    assert cut[:-1].rstrip().endswith("word")


def test_an_ordinary_search_says_why_each_row_is_there(conn, vault, cfg, fake_embedder):
    """`varyz search` renders a "why" column, and only behavioral queries filled it.

    Every ordinary search -- which is nearly all of them -- printed the header
    and then a blank cell on every row. Channel membership is what this path
    actually knows, and it is the distinction that matters before `varyz embed`
    has run: a row only the vector index found is a guess about meaning, not a
    place the words appear.
    """
    _seed(conn, vault, cfg, fake_embedder)
    hits = hybrid_search(conn, "cabin roof", fake_embedder, limit=5)

    assert hits
    assert all(hit.match_reasons for hit in hits)
    named = next(hit for hit in hits if "cabin" in hit.entry.body)
    assert "your search words" in named.match_reasons


def test_a_row_somebody_else_wrote_is_not_credited_to_the_owner(
    conn, vault, cfg, fake_embedder
):
    """The keyword channel was labelled "your words" whoever wrote the row.

    On a machine holding a cloned repository, searching for a word that
    appears in its history answered with that project's commits and told the
    owner they were his own words, while the same archive recorded every one
    of them as authored by somebody else.  The channel is about the words
    that were typed into the search box; it knows nothing about authorship
    and must not sound like it does.
    """
    _seed(conn, vault, cfg, fake_embedder)
    entry_id = conn.execute(
        "SELECT id FROM entries WHERE body LIKE '%cabin%'"
    ).fetchone()["id"]
    conn.execute("UPDATE entries SET authored_by_user=0 WHERE id=?", (entry_id,))
    conn.commit()

    hits = hybrid_search(conn, "cabin roof", fake_embedder, limit=5)
    theirs = next(hit for hit in hits if hit.entry.id == entry_id)

    assert theirs.match_reasons
    assert not theirs.entry.authored_by_user
    assert all("your words" not in reason for reason in theirs.match_reasons)


def test_a_row_no_keyword_matched_is_not_called_the_users_words(
    conn, vault, cfg, fake_embedder
):
    _seed(conn, vault, cfg, fake_embedder)
    hits = hybrid_search(conn, "cabin roof", fake_embedder, limit=5)
    lexical = {hit.entry.id for hit in hits if "cabin" in hit.entry.body}

    for hit in hits:
        if hit.entry.id not in lexical:
            assert hit.match_reasons == ("similar meaning",)


def test_embedding_is_idempotent(conn, vault, cfg, fake_embedder):
    _seed(conn, vault, cfg, fake_embedder)
    assert embed_pending(conn, cfg, fake_embedder) == 0  # second run: nothing left
    assert conn.execute(
        "SELECT COUNT(*) FROM enrichment_jobs WHERE task='embed' AND status='pending'"
    ).fetchone()[0] == 0


def test_changing_the_chunk_size_re_cuts_an_archive_already_chunked(
    conn, vault, cfg, fake_embedder
):
    """Chunking used to run only on entries that had none, so an entry was cut
    once by whatever the rules were that day and never again. `chunk_tokens`
    could be edited, or the splitter fixed, and neither reached a single
    existing entry — the setting looked like it applied and silently didn't.
    """
    _seed(conn, vault, cfg, fake_embedder)
    before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert embed_pending(conn, cfg, fake_embedder) == 0  # settled

    cfg.embedding.chunk_tokens = 5  # 20 chars: every entry splits several ways
    assert embed_pending(conn, cfg, fake_embedder) > 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > before
    recipes = {r[0] for r in conn.execute("SELECT DISTINCT chunk_recipe FROM chunks")}
    assert recipes == {chunk_recipe(20)}, "one archive must not hold two chunkings"


def test_the_first_sync_does_not_report_a_download_it_completed_as_an_error(monkeypatch):
    """The embedding model arrives without two red ERROR lines.

    huggingface_hub links blobs into snapshots, Windows refuses without
    Developer Mode, and fastembed logged "Could not download model from either
    source" and slept through its retries before quietly falling back to a
    copy. The download worked; the first command a new owner ran looked like it
    had not.
    """
    import os
    import sys
    import types

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)
    constants = types.SimpleNamespace(
        HF_HUB_DISABLE_SYMLINKS=False, HF_HUB_DISABLE_SYMLINKS_WARNING=False
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", constants)

    _prefer_copies_to_symlinks()

    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
    # Already imported, so the environment alone would have come too late.
    assert constants.HF_HUB_DISABLE_SYMLINKS is True
    assert constants.HF_HUB_DISABLE_SYMLINKS_WARNING is True


def test_a_machine_that_can_make_symlinks_is_left_alone(monkeypatch):
    import os

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)

    _prefer_copies_to_symlinks()

    assert "HF_HUB_DISABLE_SYMLINKS" not in os.environ


def test_the_hubs_token_notice_is_not_shown_on_a_first_download(monkeypatch):
    import logging

    hub = logging.getLogger("huggingface_hub.utils._http")
    monkeypatch.setattr(hub, "level", logging.NOTSET)

    _quiet_hub_notices()

    # "Set a HF_TOKEN to enable higher rate limits" is a warning; errors are not.
    assert not hub.isEnabledFor(logging.WARNING)
    assert hub.isEnabledFor(logging.ERROR)


class _Registry:
    @staticmethod
    def list_supported_models():
        return [{"model": "some/model", "size_in_GB": 0.22}]


def _embedder(cfg, cache: Path) -> Embedder:
    embedder = Embedder(cfg)
    embedder.cache_dir = str(cache)
    embedder.model_name = "some/model"
    return embedder


def test_the_first_download_says_what_it_is_downloading(tmp_path, capsys, cfg):
    """All fastembed prints is "Fetching 5 files", so `varyz add` on a new
    machine stopped for a quarter of a gigabyte without saying why.
    """
    _embedder(cfg, tmp_path / "models")._announce_first_download(_Registry)

    err = capsys.readouterr().err
    assert "220 MB" in err
    assert "once" in err


def test_a_model_already_on_the_machine_is_not_announced(tmp_path, cfg):
    cache = tmp_path / "models"
    (cache / "models--some--model").mkdir(parents=True)

    _embedder(cfg, cache)._announce_first_download(_Registry)


def test_a_registry_that_moved_on_still_announces_the_wait(tmp_path, capsys, cfg):
    """The number is worth having and not worth the notice."""

    class Moved:
        @staticmethod
        def list_supported_models():
            return []

    _embedder(cfg, tmp_path / "models")._announce_first_download(Moved)

    err = capsys.readouterr().err
    assert "fetching the local search model." in err


def test_chunking_packs_and_hard_splits():
    assert chunk_text("a\n\nb\n\nc", 100) == ["a\n\nb\n\nc"]
    # No whitespace anywhere to prefer, so the budget is the only boundary.
    parts = chunk_text("x" * 250, 100)
    assert [len(p) for p in parts] == [100, 100, 50]
    assert chunk_text("", 100) == []


def test_an_overlong_paragraph_splits_between_words_not_through_one():
    """A markdown table or bullet list is one paragraph with no blank line in
    it, so it runs past the budget routinely. Cutting at exactly max_chars
    severs a word, and that fragment is both a junk token in the chunk's
    embedding and the first thing `recall` quotes back to the user.
    """
    words = " ".join(f"word{i:03d}" for i in range(200))
    parts = chunk_text(words, 300)
    assert len(parts) > 1
    for part in parts:
        for token in part.split():
            assert token.startswith("word") and len(token) == 7, token


def test_a_windows_document_is_split_into_paragraphs_like_any_other():
    r"""A CRLF file contains no `\n\n` — between two paragraphs the sequence is
    `\r\n\r\n` — so the paragraph split found nothing and the whole document
    arrived as one paragraph, cut by character budget rather than by structure.
    On Windows that was every file. The stray `\r` also travelled into the
    embedding and into the excerpt quoted back to the user.
    """
    body = "# Backup policy\r\n\r\nKept for 30 days.\r\n\r\nRestores are tested quarterly."
    parts = chunk_text(body, 40)
    assert parts == ["# Backup policy\n\nKept for 30 days.", "Restores are tested quarterly."]
    assert all("\r" not in part for part in parts)
    # And the same file with Unix endings chunks identically — which is the
    # property that was missing.
    assert parts == chunk_text(body.replace("\r\n", "\n"), 40)


def test_an_unbreakable_run_still_terminates():
    # rfind returns -1 with no whitespace to find, and a budget smaller than
    # the search window would accept that -1 as a cut point, advance zero
    # characters and never finish.
    parts = chunk_text("a" * 500, 50)
    assert "".join(parts) == "a" * 500


def test_a_budget_of_nothing_is_refused_rather_than_run():
    """Zero characters is not a small chunk, it is no room to cut at all.

    The splitter takes a piece off an over-long paragraph before it looks at
    the rest, and the size of that piece is the budget. At zero it takes
    nothing and asks the same paragraph again, forever -- a hang with no
    output and no end, which is the worst shape a bad setting can take.
    """
    for budget in (0, -1, -20):
        with pytest.raises(ValueError, match="at least one character"):
            chunk_text("one two three four five", budget)


def test_identifier_shapes_survive_query_tokenisation():
    """The escape hatch must see the marks it tests for.

    `_fts_terms` splits on `.` `/` and `::` — exactly the characters
    `_looks_like_identifier` looks for — so routing the hatch through it left
    three of its four punctuation tests unreachable and closed the gate on file
    paths, dotted names and dotfiles, which are the queries it exists to admit.
    """
    def flagged(query):
        return [w for w in _identifier_candidates(query) if _looks_like_identifier(w)]

    assert flagged("what is in src/varyz/config.py") == ["src/varyz/config.py"]
    assert flagged("what does .gitignore exclude") == [".gitignore"]
    assert flagged("open README.md") == ["README.md"]
    assert flagged("call Foo::bar") == ["Foo::bar"]
    assert flagged("san7_plus_annual pricing") == ["san7_plus_annual"]
    assert flagged("error E0308") == ["E0308"]

    # Prose is not an identifier, and a sentence-final full stop does not make
    # one — otherwise every last word of every question opens the gate.
    assert flagged("What is Egor's blood type?") == []
    assert flagged("How does the scheduler work.") == []


def test_abstention_answers_for_a_named_file_and_refuses_prose(
    conn, vault, cfg, fake_embedder
):
    """An exact hit on something the corpus names almost uniquely is worth
    answering on even when the vectors are unconvinced — and nothing else is."""
    connector = ManualConnector()
    gate = SensitivityGate(cfg.sensitivity)
    ingest_item(
        conn, vault, connector, gate,
        ManualConnector.make_item("Sensitive paths are listed in src/varyz/config.py."),
    )
    embed_pending(conn, cfg, fake_embedder)

    # The fake embedder has no calibrated threshold, so pass one explicitly;
    # its hash vectors sit far from every query, which is the case the hatch
    # exists for.
    assert is_supported(conn, "what is in src/varyz/config.py", fake_embedder,
                        max_distance=0.0)
    assert not is_supported(conn, "what is my blood type", fake_embedder,
                            max_distance=0.0)


def test_switching_to_a_wider_model_rebuilds_the_vector_index(conn, vault, cfg, fake_embedder):
    """vec0 fixes its width at CREATE time and only complains on insert, so a
    model of a different size used to fail deep inside the embed loop — after
    the download and the chunking — with a bare SQLite dimension error."""
    from varyz.db import dao

    _seed(conn, vault, cfg, fake_embedder)
    assert dao.vector_width(conn) == 384

    class WiderEmbedder:
        model_name = "test/wider-768"

        def embed_query(self, text):
            return [0.05] * 768

        def embed_documents(self, texts):
            return [[0.05] * 768 for _ in texts]

    assert embed_pending(conn, cfg, WiderEmbedder()) == len(TEXTS)
    assert dao.vector_width(conn) == 768
    assert conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == len(TEXTS)
    # Every chunk carries the new model: none is left claiming a vector that
    # the rebuild discarded.
    assert {r[0] for r in conn.execute("SELECT DISTINCT embed_model FROM chunks")} == {
        "test/wider-768"
    }

    # ...and going back is just as automatic.
    assert embed_pending(conn, cfg, fake_embedder) == len(TEXTS)
    assert dao.vector_width(conn) == 384


def test_embedding_nothing_pending_never_loads_the_model(conn, vault, cfg, fake_embedder):
    """`varyz embed` on an up-to-date corpus must not download 220MB to learn
    that it has no work."""
    _seed(conn, vault, cfg, fake_embedder)

    class ExplodingEmbedder:
        model_name = fake_embedder.model_name

        def embed_query(self, text):
            raise AssertionError("model loaded with nothing to embed")

        def embed_documents(self, texts):
            raise AssertionError("model loaded with nothing to embed")

    assert embed_pending(conn, cfg, ExplodingEmbedder()) == 0


# --- the abstention threshold has a corpus size, not just a model -----------


def _index_vectors(conn, count: int) -> None:
    """Give the corpus `count` vectors without embedding anything, so the size
    gate can be exercised without a 220MB model download."""
    from sqlite_vec import serialize_float32

    with conn:
        for rowid in range(1, count + 1):
            conn.execute(
                "INSERT OR REPLACE INTO vec_chunks (rowid, embedding) VALUES (?,?)",
                (100_000 + rowid, serialize_float32([0.0] * 384)),
            )


class FarEmbedder:
    """Every query lands nowhere near the corpus, so the distance gate — if it
    is the one being applied — always refuses."""

    model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def embed_query(self, text):
        return [9.0] * 384


def test_a_small_corpus_falls_back_to_the_lexical_test(conn, vault, cfg, fake_embedder):
    """The packaged threshold is a minimum over the whole index, so it encodes
    the size of the index it was measured on. Measured by treating the k-th
    nearest neighbour as the nearest neighbour of an index k times smaller,
    the share of answerable golden questions wrongly refused runs 0% at the
    calibration size of 11,389 vectors, 10% at 2,278, and 60% at 250.

    A user who indexes one folder lands at the bottom of that table, where
    refusing looks identical to an empty archive. So below the floor the
    distance gate is off and the lexical test decides instead.
    """
    _seed(conn, vault, cfg, fake_embedder)
    _index_vectors(conn, MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE)
    assert is_supported(conn, "Lena gym Berlin", FarEmbedder()) is True
    assert is_supported(conn, "migratory patterns of the arctic tern", FarEmbedder()) is False


def test_a_corpus_too_small_even_to_be_asked_answers_anyway(conn, vault, cfg, fake_embedder):
    """Coverage degrades with size too, just more slowly: a corpus of three
    notes holds the vocabulary of no question, on-topic or not, so the lexical
    gate below its own measured size refuses everything indiscriminately.
    Nothing has been measured this small and the archive was installed minutes
    ago — answering weakly is the only outcome that isn't guaranteed wrong.
    """
    _seed(conn, vault, cfg, fake_embedder)
    assert corpus_vectors(conn) < MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE
    assert is_supported(conn, "migratory patterns of the arctic tern", FarEmbedder()) is True


def test_a_tiny_corpus_still_refuses_a_name_it_has_never_seen(
    conn, vault, cfg, fake_embedder
):
    """Name absence stays conclusive when distance and coverage are unmeasured."""
    _seed(conn, vault, cfg, fake_embedder)
    assert corpus_vectors(conn) < MIN_CORPUS_VECTORS_FOR_LEXICAL_GATE
    assert is_supported(conn, "What did Quernwood decide?", FarEmbedder()) is False


def test_a_corpus_past_the_floor_abstains_as_measured(conn):
    _index_vectors(conn, MIN_CORPUS_VECTORS_FOR_ABSTENTION)
    assert is_supported(conn, "anything at all", FarEmbedder()) is False


def test_a_threshold_the_user_measured_themselves_ignores_the_floor(conn):
    # `evaluate_retrieval.py --calibrate` produces a number for the user's own
    # corpus, whatever its size, so the packaged floor no longer applies.
    class UnmeasuredEmbedder:
        model_name = "some/unmeasured-model"

        def embed_query(self, text):
            return [9.0] * 384

    _index_vectors(conn, 50)
    assert is_supported(conn, "anything", FarEmbedder(), max_distance=0.1) is False


def test_below_the_floor_a_subject_the_archive_never_mentions_is_still_refused(
    conn, vault, cfg, fake_embedder
):
    """The distance gate is unusable on a small index, but term presence does
    not depend on index size. Measured on a 247-vector corpus: answerable
    questions scored 1.00 coverage, unanswerable ones 0.00-0.83."""
    _seed(conn, vault, cfg, fake_embedder)
    assert lexically_grounded(conn, "the migratory patterns of the arctic tern") is False


def test_below_the_floor_a_subject_the_archive_covers_is_answered(
    conn, vault, cfg, fake_embedder
):
    _seed(conn, vault, cfg, fake_embedder)
    assert lexically_grounded(conn, "what did Lena say about Berlin") is True


def test_the_lexical_test_asks_the_index_not_a_single_entry(
    conn, vault, cfg, fake_embedder
):
    # FTS5's implicit AND would demand one entry containing every term, and no
    # entry here holds both. This asks only whether the archive knows the words.
    _seed(conn, vault, cfg, fake_embedder)
    assert lexically_grounded(conn, "Lena cabin") is True


def test_a_question_of_pure_stopwords_is_not_refused_on_coverage(
    conn, vault, cfg, fake_embedder
):
    _seed(conn, vault, cfg, fake_embedder)
    assert lexically_grounded(conn, "what is it") is True


def test_one_word_is_looked_up_rather_than_measured_for_distance(
    conn, vault, cfg, fake_embedder
):
    """The threshold was calibrated on questions, and a keyword is not one.

    On the owner's 53,910-vector archive the distance of a bare term does not
    merely fail to say whether the archive holds it -- it points the wrong way.
    Every real term measured sat between 3.74 and 5.41 and every invented one
    nearer, between 2.54 and 4.36: `trading` at 4.842 with over two hundred
    entries carrying it, `grastelle` at 2.537 with none. Nine of thirty-three
    common terms were refused while holding twelve to two hundred entries
    apiece, and `recall` reports a refusal as an answer, so an agent asked for
    "telegram" told its owner the archive had nothing on a source of 11,794 of
    its entries.

    Both embedders below are extremes -- one puts every query on top of the
    corpus, the other nowhere near it -- and neither changes the outcome,
    because at this length distance is not what decides.
    """
    _seed(conn, vault, cfg, fake_embedder)
    _index_vectors(conn, MIN_CORPUS_VECTORS_FOR_ABSTENTION)

    class NearEmbedder:
        model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

        def embed_query(self, text):
            return [0.0] * 384

    for embedder in (FarEmbedder(), NearEmbedder()):
        assert is_supported(conn, "Berlin", embedder) is True
        assert is_supported(conn, "Lena cabin", embedder) is True
        assert is_supported(conn, "Quernwood", embedder) is False
        assert is_supported(conn, "zzqwxplk", embedder) is False

    # Every term, not half of them: at this length there is no second word to
    # carry the meaning if the first one is missing.
    assert is_supported(conn, "Quernwood cabin", FarEmbedder()) is False
    # And a question is still a question. Three content words are back on the
    # scale the threshold was measured on.
    assert is_keyword_query("what did Lena say about the cabin roof") is False
    assert is_supported(conn, "what did Lena say about the cabin roof", FarEmbedder()) is False


def test_a_name_the_archive_has_never_seen_is_refused_however_close_it_sits(
    conn, vault, cfg, fake_embedder
):
    """The gap the distance gate stopped covering as the index grew.

    On the owner's 53,910-vector archive the nearest neighbour of eight
    questions naming invented people and places sat between 3.198 and 4.605,
    and of four the archive could answer between 4.167 and 4.598 -- overlapping,
    with the answerable ones farther on average. A sentence embedding measures
    the shape of the sentence, and these questions have the shape of a hundred
    the archive can answer; the one word that decides the answer is missing,
    and distance cannot see one word. Re-run over forty invented names and
    nineteen real ones: supported fell from 35/40 to 0/40 on the invented set
    and stayed at 15/19 on the real one.
    """

    _seed(conn, vault, cfg, fake_embedder)
    # Past the size floor, so it is the distance gate that would otherwise be
    # deciding -- and this embedder puts every question right on top of the
    # corpus, which is the case the name test has to survive.
    _index_vectors(conn, MIN_CORPUS_VECTORS_FOR_ABSTENTION)

    class NearEmbedder:
        model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

        def embed_query(self, text):
            return [0.0] * 384

    assert names_the_archive_has_never_seen(conn, "What did Quernwood decide?") == [
        "Quernwood"
    ]
    assert is_supported(conn, "What did Quernwood decide?", NearEmbedder()) is False
    # A name it does hold is not refused, and the sentence's own first capital
    # is grammar rather than a name.
    assert names_the_archive_has_never_seen(conn, "What did Lena say about Berlin?") == []
    assert is_supported(conn, "What did Lena say about Berlin?", NearEmbedder()) is True
    # A missing ordinary word is the author's synonym for one they did write,
    # so only names are conclusive.
    assert names_the_archive_has_never_seen(conn, "the arctic tern migration") == []


def test_a_disk_too_full_for_the_model_is_said_before_the_download(tmp_path, cfg, monkeypatch):
    """A download cut short by a full disk left a folder that looked like a model."""
    from varyz import diskspace

    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 100 * 1000**2)
    with pytest.raises(OSError, match="only 100.0 MB is free"):
        _embedder(cfg, tmp_path / "models")._announce_first_download(_Registry)
