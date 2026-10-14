"""Guard the frozen experiment and its evidence/negative-result accounting."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "title_boost_eval", ROOT / "scripts" / "evaluate_title_boost.py"
)
assert SPEC and SPEC.loader
study = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = study
SPEC.loader.exec_module(study)


def evaluate(case, conn):
    embedder = study.DeterministicHashEmbedder()
    texts = [doc["title"] + "\n" + doc["body"] for doc in case["documents"]]
    vectors = dict(zip(texts, embedder.embed_documents(texts), strict=True))
    return study.evaluate_case(case, embedder, conn, vectors, embedder.embed_query(case["query"]))


def test_frozen_sets_have_separate_ids_balanced_strata_and_valid_gold():
    cases = study.load_cases()
    assert Counter(case["split"] for case in cases) == {
        "dev": 48,
        "held_out": 48,
        "replication": 48,
    }
    assert len({case["id"] for case in cases}) == 144
    for split in study.TOPICS:
        assert Counter(case["category"] for case in cases if case["split"] == split) == {
            category: 4 for category in study.CATEGORIES
        }
    for case in cases:
        documents = {doc["id"]: doc for doc in case["documents"]}
        assert set(case["expected"]) <= documents.keys()
        assert all(documents[ref]["sensitivity"] == "public" for ref in case["expected"])


def test_frozen_challenge_exposes_harm_without_removing_correct_evidence(conn):
    path = ROOT / "evals" / "title_boost_challenges_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = json.loads(
        path.with_name("title_boost_challenges_v1_manifest.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(study.canonical(payload)).hexdigest() == manifest["fixture_sha256"]
    row = evaluate(payload["cases"][0], conn)
    assert row["baseline"]["references"][0] == "answer"
    assert row["title"]["references"][0] == "wrong"
    assert row["harmed"] is True
    assert row["lost_complete@3"] is False


def test_sensitive_and_unclassified_title_matches_never_reach_output(conn):
    cases = [
        case
        for case in study.load_cases()
        if case["split"] == "dev" and case["category"] == "privacy"
    ]
    for case in cases:
        row = evaluate(case, conn)
        for arm in ("baseline", "title"):
            assert row[arm]["forbidden_returned"] == []
            assert "wrong" not in row[arm]["references"]
        if not case["expected"]:
            assert row["baseline"]["references"] == row["title"]["references"] == []
            assert row["chance_first_relevant"] == 1


def test_explicit_source_exclusion_precedes_title_boost(conn):
    case = next(
        case
        for case in study.load_cases()
        if case["split"] == "dev" and case["category"] == "excluded_source"
    )
    row = evaluate(case, conn)
    assert "wrong" not in row["boosted"]
    assert row["title"]["forbidden_returned"] == []
    assert row["title"]["references"][0] == "answer"
