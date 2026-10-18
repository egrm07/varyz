"""Offline GBrain-inspired ablations; synthetic data only, no owner archive access."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_public_regression as public  # noqa: E402

from varyz.db import dao  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.domain import Kind, NormalizedEntry, SearchHit, Sensitivity  # noqa: E402
from varyz.retrieval import search  # noqa: E402

GBRAIN_COMMIT = "8b5ed04c6c061031acd953fccd5590357e8bfe8e"
ADAPTIVE_VECTOR_SEARCH = search.vector_search


def baseline_vector_search(
    conn: sqlite3.Connection, query_vec: list[float], limit: int
) -> tuple[list[str], dict[str, str]]:
    """Frozen pre-change Varyz baseline, kept independent of production code."""
    from sqlite_vec import serialize_float32

    rows = conn.execute(
        """SELECT c.entry_id, c.text, MIN(v.distance) AS dist
           FROM (SELECT rowid, distance FROM vec_chunks
                 WHERE embedding MATCH ? ORDER BY distance LIMIT ?) v
           JOIN chunks c ON c.id = v.rowid
           GROUP BY c.entry_id ORDER BY dist""",
        (serialize_float32(query_vec), limit * 3),
    ).fetchall()[:limit]
    return [row["entry_id"] for row in rows], {row["entry_id"]: row["text"] for row in rows}


def prototype_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: public.DeterministicHashEmbedder,
    limit: int = 10,
    pool: int = 30,
    *,
    technique: str,
    effects: dict[str, int],
) -> list[SearchHit]:
    """Ranking-only ablations; never used by the installed product."""
    hits = search.legacy_hybrid_search(conn, query, embedder, limit=pool, pool=pool)
    if technique == "title":
        tokens = re.findall(r"[^\W_]+", query.casefold())
        content = [word for word in tokens if word not in search._STOPWORDS]
        for hit in hits:
            title = re.findall(r"[^\W_]+", (hit.entry.title or "").casefold())
            phrase = bool(tokens) and any(
                title[i : i + len(tokens)] == tokens for i in range(len(title))
            )
            if tokens == title or (len(content) >= 2 and phrase):
                hit.score *= 1.25
                effects["boosted_title_hits"] += 1
    elif technique == "weights":
        kw_weight, vec_weight = 1.0, 1.0
        if re.search(r"\b(who is|tell me about)\b", query, re.I):
            kw_weight = 1.15
        elif re.search(r"\b(launched|announcement|raised)\b", query, re.I):
            kw_weight, vec_weight = 1.20, 0.95
        elif re.search(r"\b(meaning|means|explain|concept)\b", query, re.I):
            kw_weight, vec_weight = 0.9, 1.2
        effects["weighted_queries"] += (kw_weight, vec_weight) != (1.0, 1.0)
        kw, _ = search.keyword_search(conn, query, pool)
        vec, _ = baseline_vector_search(conn, embedder.embed_query(query), pool)
        scores = {}
        for ids, weight in ((kw, kw_weight), (vec, vec_weight)):
            for rank, entry_id in enumerate(ids, 1):
                scores[entry_id] = scores.get(entry_id, 0) + 1 / (search.RRF_K / weight + rank)
        for hit in hits:
            hit.score = scores[hit.entry.id]
    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]


def public_ablation(technique: str, split: str) -> dict[str, Any]:
    effects = {"boosted_title_hits": 0, "weighted_queries": 0}

    def retrieve(
        conn: sqlite3.Connection,
        question: str,
        embedder: public.DeterministicHashEmbedder,
        limit: int,
    ) -> list[str]:
        if technique in {"baseline", "adaptive"}:
            hits = search.hybrid_search(conn, question, embedder, limit=limit, pool=30)
        else:
            hits = prototype_search(
                conn, question, embedder, limit, technique=technique, effects=effects
            )
        return [public._relative_identity(hit.entry.external_id) for hit in hits]

    vector = ADAPTIVE_VECTOR_SEARCH if technique == "adaptive" else baseline_vector_search
    with (
        patch.object(search, "vector_search", vector),
        patch.object(public, "_retrieve_hybrid", retrieve),
    ):
        report = public.evaluate(split)
    return {
        **{key: report[key] for key in ("hybrid", "records", "abstention", "hashes", "floors")},
        "effects": effects,
    }


def bootstrap(deltas: list[float], seed: int = 731) -> list[float]:
    rng = random.Random(seed)
    values = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(4000))
    return [values[100], values[3899]]


def ranking_window(name: str, seed: int) -> dict[str, Any]:
    """Fresh signal-activation probes, fixed before scoring; hash embeddings only."""
    rng = random.Random(seed)
    specs = []
    for index in range(16):
        topic = f"{name} Cedar {index}"
        category = ("title", "entity", "event", "concept")[index % 4]
        if category == "title":
            query, title = topic, topic
            body = f"{topic} is the canonical reference. " + "Background context. " * 50
        elif category == "entity":
            query, title = f"Who is {topic}?", topic
            body = f"{topic} is the coordinator responsible for the delivery schedule."
        elif category == "event":
            query, title = f"What launched at {topic}?", f"Launch at {topic}"
            body = f"At {topic} the team launched the Alder message scheduler."
        else:
            query, title = f"Explain the concept of {topic} queues", f"{topic} design"
            body = f"{topic} queues retain messages until processing succeeds."
        distractors = [
            f"{topic} " * rng.randint(2, 5) + "This is an index listing, not the reference.",
            f"A calendar invitation mentioning {topic}; attendance is optional.",
            f"Discussion about testing the old {topic} draft, before approval.",
        ]
        specs.append(
            {
                "category": category,
                "query": query,
                "title": title,
                "body": body,
                "distractors": distractors,
            }
        )
    records = []
    with tempfile.TemporaryDirectory(prefix="varyz-gbrain-ranking-") as directory:
        conn = open_db(Path(directory) / "fixture.db")
        embedder = public.DeterministicHashEmbedder()
        try:
            for index, spec in enumerate(specs):
                with conn:
                    conn.execute("DELETE FROM vec_chunks")
                    conn.execute("DELETE FROM chunks")
                    conn.execute("DELETE FROM entries")
                    expected = None
                    for doc, body in enumerate([spec["body"], *spec["distractors"]]):
                        title = spec["title"] if doc == 0 else f"Supporting note {doc}"
                        entry_id, _ = dao.upsert_entry(
                            conn,
                            NormalizedEntry(
                                kind=Kind.NOTE,
                                external_id=f"{name}/{index}/{doc}",
                                title=title,
                                body=body,
                            ),
                            source="manual",
                            sensitivity=Sensitivity.PUBLIC,
                        )
                        if doc == 0:
                            expected = entry_id
                        text = title + "\n" + body
                        ids = dao.replace_chunks(conn, entry_id, [text])
                        dao.store_embeddings(
                            conn, ids, embedder.embed_documents([text]), embedder.model_name
                        )
                row = {
                    "id": f"{name}-{index}",
                    "category": spec["category"],
                    "query": spec["query"],
                }
                with patch.object(search, "vector_search", baseline_vector_search):
                    for arm in ("baseline", "title", "weights"):
                        effects = {"boosted_title_hits": 0, "weighted_queries": 0}
                        hits = (
                            search.legacy_hybrid_search(conn, spec["query"], embedder)
                            if arm == "baseline"
                            else prototype_search(
                                conn, spec["query"], embedder, technique=arm, effects=effects
                            )
                        )
                        row[arm] = {
                            "success@1": bool(hits and hits[0].entry.id == expected),
                            "effects": effects,
                            "top": [hit.entry.external_id for hit in hits],
                        }
                records.append(row)
        finally:
            conn.close()
    return {
        "name": name,
        "seed": seed,
        "n": len(records),
        "chance_success@1": 0.25,
        "specs_sha256": hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest(),
        "success@1": {
            arm: statistics.mean(row[arm]["success@1"] for row in records)
            for arm in ("baseline", "title", "weights")
        },
        "paired_delta_ci95": {
            arm: bootstrap(
                [int(row[arm]["success@1"]) - int(row["baseline"]["success@1"]) for row in records]
            )
            for arm in ("title", "weights")
        },
        "records": records,
    }


def stress_window(name: str, seed: int, cases: int) -> dict[str, Any]:
    """Fresh document-density distributions, frozen before any candidate scores."""
    rng = random.Random(seed)
    specs = [
        {"crowding": rng.choice([1, 3, 12, 60, 180]), "entries": rng.randint(8, 16)}
        for _ in range(cases)
    ]
    records = []
    with tempfile.TemporaryDirectory(prefix="varyz-gbrain-stress-") as directory:
        conn = open_db(Path(directory) / "fixture.db")
        try:
            for index, spec in enumerate(specs):
                with conn:
                    conn.execute("DELETE FROM vec_chunks")
                    conn.execute("DELETE FROM chunks")
                    conn.execute("DELETE FROM entries")
                    expected = []
                    for doc in range(spec["entries"]):
                        entry_id, _ = dao.upsert_entry(
                            conn,
                            NormalizedEntry(
                                kind=Kind.NOTE,
                                external_id=f"{name}/{index}/{doc}",
                                body=f"Synthetic document {doc}",
                            ),
                            source="manual",
                            sensitivity=Sensitivity.PUBLIC,
                        )
                        expected.append(entry_id)
                        count = spec["crowding"] if doc == 0 else rng.randint(1, 4)
                        texts = [f"document {doc} passage {i}" for i in range(count)]
                        ids = dao.replace_chunks(conn, entry_id, texts)
                        vectors = [
                            [doc + i / (count + 1) * 0.25] + [0.0] * 383 for i in range(count)
                        ]
                        dao.store_embeddings(conn, ids, vectors, "controlled-distance-v1")
                row = {"id": f"{name}-{index}", **spec, "chance_recall@5": 5 / spec["entries"]}
                for arm, function in (
                    ("baseline", baseline_vector_search),
                    ("adaptive", ADAPTIVE_VECTOR_SEARCH),
                ):
                    started = time.perf_counter()
                    found, snippets = function(conn, [0.0] * 384, 5)
                    row[arm] = {
                        "recall@5": len(set(found) & set(expected[:5])) / 5,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "returned": len(found),
                        "best_chunk_correct": snippets.get(expected[0]) == "document 0 passage 0",
                    }
                records.append(row)
        finally:
            conn.close()
    deltas = [row["adaptive"]["recall@5"] - row["baseline"]["recall@5"] for row in records]
    return {
        "name": name,
        "seed": seed,
        "n": cases,
        "specs_sha256": hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest(),
        "baseline_recall@5": statistics.mean(row["baseline"]["recall@5"] for row in records),
        "adaptive_recall@5": statistics.mean(row["adaptive"]["recall@5"] for row in records),
        "chance_recall@5": statistics.mean(row["chance_recall@5"] for row in records),
        "paired_delta_ci95": bootstrap(deltas),
        "records": records,
    }


def main() -> None:
    # Design and seeds are fixed; dev, held-out and replication have independent DBs.
    result = {
        "date": "2026-10-04",
        "gbrain_commit": GBRAIN_COMMIT,
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "public": {
            arm: {split: public_ablation(arm, split) for split in ("dev", "held_out")}
            for arm in ("baseline", "adaptive", "title", "weights")
        },
        "stress": [
            stress_window("dev", 11, 12),
            stress_window("held_out", 271, 24),
            stress_window("replication", 983, 24),
        ],
        "ranking_probes": [
            ranking_window("probeheldout", 1237),
            ranking_window("probereplication", 8119),
        ],
        "caveats": [
            "Synthetic retrieval, not owner preference resolution or live-model quality.",
            "Public hash vectors are lexical execution fixtures, not semantic embeddings.",
            "Fresh stress windows test chunk crowding with controlled distances only.",
            "No owner archive, cloud model, GBrain runtime or paid service used.",
        ],
    }
    output = ROOT / "reports" / "GBRAIN_TECHNIQUES_2026-10-04.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for arm, splits in result["public"].items():
        print(arm, {split: data["hybrid"] for split, data in splits.items()})
    for window in result["stress"]:
        print(
            window["name"],
            window["baseline_recall@5"],
            window["adaptive_recall@5"],
            window["paired_delta_ci95"],
        )
    for window in result["ranking_probes"]:
        print(window["name"], window["success@1"], window["paired_delta_ci95"])
    print(output)


if __name__ == "__main__":
    main()
