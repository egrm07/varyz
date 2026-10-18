from __future__ import annotations

import json
from pathlib import Path

import pytest
from varyz.retrieval.rag import ask

from varyz.connectors.manual import ManualConnector
from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.llm.router import LLMRouter
from varyz.pipeline.embed import embed_pending
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.retrieval.behavior_search import analyze_behavior_query
from varyz.retrieval.depth import classify_depth
from varyz.retrieval.planner import RetrievalMode, plan_query
from varyz.retrieval.search import hybrid_search, legacy_hybrid_search

FIXTURES = Path(__file__).parent / "fixtures"


def _cases(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


def _seed_cases(conn, cases: list[dict]) -> None:
    for case in cases:
        for item in case["entries"]:
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=item["id"],
                    body=item["body"],
                ),
                source="eval",
                source_account="me",
                sensitivity=Sensitivity.PERSONAL,
            )


def test_exact_lie_query_is_understood_without_an_llm():
    intent = analyze_behavior_query("find me when I lied to sandra")
    assert intent is not None
    assert intent.concept.key == "deception"
    assert intent.focus_terms == ("sandra",)
    assert intent.actor == "user"
    assert "dishonesty" in intent.expanded_query


@pytest.mark.parametrize(
    "query",
    [
        # Every one of these contains a behavior keyword by coincidence.
        # Claiming them costs the whole result set, not a place in the ranking:
        # the behavioral path filters candidates through the evidence check, so
        # a technical question routed here comes back empty. Two golden-set
        # questions were being lost exactly this way.
        "How does sync avoid creating duplicate entries when run twice?",
        "How does the git connector avoid re-reading commits it has already seen?",
        "How do I avoid sending duplicate emails to users?",
        "Why did sync ignore the rate limit?",
        "Did Postgres ignore the index hint?",
        "Which library helped with PDF parsing?",
        "What does the roadmap promise for Q3?",
        "Why did the worker ignore my SIGTERM handler?",
        "What support does the API have for streaming?",
        "What conflict does git report when I rebase onto main?",
    ],
)
def test_technical_questions_are_not_claimed_as_behavioral(query):
    assert analyze_behavior_query(query) is None


@pytest.mark.parametrize(
    "query,expected_focus",
    [
        # A person named by a verb needs no pronoun in the question: the verb
        # has already established that the next word is a person.
        ("when Sandra lied to Marta", ("marta",)),
        # ...and a lower-case name still resolves through that same route.
        ("find me when I lied to sandra", ("sandra",)),
    ],
)
def test_named_people_still_reach_the_ontology(query, expected_focus):
    intent = analyze_behavior_query(query)
    assert intent is not None
    assert intent.focus_terms == expected_focus


@pytest.mark.parametrize(
    "fixture_name",
    ["behavior_search_dev.json", "behavior_search_replication.json"],
)
def test_behavioral_search_passes_locked_windows_without_embeddings(conn, fixture_name):
    cases = _cases(fixture_name)
    for case in cases:
        with conn:
            conn.execute("DELETE FROM entries")
        _seed_cases(conn, [case])
        hits = hybrid_search(conn, case["query"], None, limit=5)
        predicted = [hit.entry.external_id for hit in hits]
        relevant = set(case["relevant"])
        if relevant:
            assert predicted, case["id"]
            assert predicted[0] in relevant, (case["id"], predicted)
            assert relevant.intersection(predicted), (case["id"], predicted)
            assert hits[0].match_reasons
        else:
            assert predicted == [], (case["id"], predicted)


def test_behavioral_reranker_excludes_wrong_person_and_wrong_actor(conn):
    case = _cases("behavior_search_dev.json")[0]
    _seed_cases(conn, [case])

    # The plain ranker cannot do this. It used to fail by returning nothing at
    # all — an artefact of FTS5's implicit AND, which required every query word
    # of "find me when I lied to Sandra" to appear in one entry. Over a
    # disjunction it fails the way keyword search actually fails: it answers,
    # and leads with d3, which is both the wrong person (Zahar, not Sandra) and
    # the wrong actor (they lied, not the user). Ranking alone cannot tell the
    # difference; that is the reranker's job.
    baseline = [hit.entry.external_id for hit in
                legacy_hybrid_search(conn, case["query"], None, limit=5)]
    assert baseline[0] == "d3"

    hits = hybrid_search(conn, case["query"], None, limit=5)
    predicted = [hit.entry.external_id for hit in hits]
    assert predicted[:2] == ["d1", "d4"]
    assert "d3" not in predicted


def test_behavioral_query_gets_deeper_mixed_retrieval():
    query = "find me when I lied to Sandra"
    assert classify_depth(query).is_deep
    plan = plan_query(query, graph_available=True)
    assert plan.mode == RetrievalMode.MIXED
    assert plan.reason_code == "behavioral_evidence_intent"


def test_behavioral_ask_sends_only_qualified_candidates(
    conn, vault, cfg, fake_embedder, fake_llm
):
    gate = SensitivityGate(cfg.sensitivity)
    for text in (
        "From: me\nChat: Sandra\n\nI lied to you about the concert and I regret it.",
        "From: me\nChat: Sandra\n\nYou recommended a great concert.",
        "From: me\nChat: Zahar\n\nYou lied using my name.",
    ):
        ingest_item(
            conn,
            vault,
            ManualConnector(),
            gate,
            ManualConnector.make_item(text),
        )
    embed_pending(conn, cfg, fake_embedder)

    result = ask(
        conn,
        cfg,
        "find me when I lied to Sandra",
        fake_embedder,
        router=LLMRouter(cfg, provider=fake_llm),
    )
    "".join(result.stream)

    assert len(result.hits) == 1
    assert "I lied to you" in result.hits[0].entry.body
    _, prompt = fake_llm.calls[0]
    assert "possible behavioral evidence" in prompt
    assert "You lied using my name" not in prompt


def test_behavioral_ask_explains_a_calibrated_empty_result(
    conn, cfg, fake_embedder, fake_llm
):
    result = ask(
        conn,
        cfg,
        "find me when I lied to Sandra",
        fake_embedder,
        router=LLMRouter(cfg, provider=fake_llm),
    )
    answer = "".join(result.stream)
    assert "no qualifying behavioral evidence" in answer.lower()
    assert "not proof it never happened" in answer.lower()
    assert fake_llm.calls == []
