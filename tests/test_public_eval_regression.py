"""Frozen, fully offline public retrieval regression gate."""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

from varyz.pipeline.embed import chunk_text

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_public_regression.py"
SPEC = importlib.util.spec_from_file_location("evaluate_public_regression", SCRIPT)
assert SPEC and SPEC.loader
public_eval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = public_eval
SPEC.loader.exec_module(public_eval)


def test_public_fixture_is_frozen_balanced_and_non_private():
    tasks = public_eval.load_tasks()
    validated = public_eval.validate_fixture(tasks)

    assert len(tasks) == 60
    assert sum(task.split == "dev" for task in tasks) == 36
    assert sum(task.split == "held_out" for task in tasks) == 24
    assert validated["manifest"]["privacy"].startswith("All people")

    targets = Counter(target for task in tasks for target in task.expected)
    assert max(targets.values()) / len(tasks) <= 0.05
    assert {task.id for task in tasks if task.split == "dev"}.isdisjoint(
        task.id for task in tasks if task.split == "held_out"
    )


def test_each_split_has_late_cross_source_and_no_answer_cases():
    tasks = public_eval.load_tasks()
    for split in ("dev", "held_out"):
        subset = [task for task in tasks if task.split == split]
        assert any("no-answer" in task.tags for task in subset)
        cross = [task for task in subset if "cross-source" in task.tags]
        assert cross
        assert all(len(task.expected) >= 2 for task in cross)
        assert all(len(set(task.source_types)) >= 2 for task in cross)

        for task in (task for task in subset if "mid-file" in task.tags):
            question_terms = set(public_eval.DeterministicHashEmbedder._terms(task.question))
            for source in task.expected:
                path = public_eval.CORPUS / source
                text = f"{path.stem}\n\n{path.read_text(encoding='utf-8')}"
                chunks = chunk_text(text, 45 * 4)
                assert len(chunks) >= 2, f"{task.id}/{source} is not a late-passage case"
                first_terms = set(public_eval.DeterministicHashEmbedder._terms(chunks[0]))
                first_overlap = len(question_terms & first_terms)
                later_overlap = max(
                    len(question_terms & set(public_eval.DeterministicHashEmbedder._terms(chunk)))
                    for chunk in chunks[1:]
                )
                assert later_overlap > first_overlap, f"{task.id}/{source} answer leaks early"


def test_hash_embedder_is_stable_and_label_free():
    first = public_eval.DeterministicHashEmbedder()
    second = public_eval.DeterministicHashEmbedder()
    query = "Combine the Aurora appointment with its rollback control."
    assert first.embed_query(query) == second.embed_query(query)
    assert len(first.embed_query(query)) == 384


def test_held_out_regression_uses_real_ingest_embed_and_search_pipeline():
    report = public_eval.evaluate("held_out")

    assert report["tasks"] == 24
    assert report["corpus_entries"] == 48
    assert report["embedded_chunks"] > report["corpus_entries"]
    assert report["hybrid"]["source_recall@5"] >= 0.90
    assert report["hybrid"]["task_success@10"] >= 0.90
    assert report["abstention"]["false_evidence_rate"] == 0.0
    assert not public_eval.gate_failures(report)

    cross_records = [record for record in report["records"] if "cross-source" in record["tags"]]
    assert cross_records
    assert all(set(record["expected"]) <= set(record["top"][:10]) for record in cross_records)
    assert report["floors"]["chance_random_sources"]["source_recall@5"] < 0.11
    assert len(report["caveats"]) >= 5
