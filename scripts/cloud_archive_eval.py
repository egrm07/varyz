"""Offline synthetic retrieval comparison; immutable dev/held-out/replication splits."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import statistics
import time
from pathlib import Path

from cryptography.fernet import Fernet

from varyz.cloud.archive import MODEL, Archive, CachedEmbedder, Change, Chunk, Document
from varyz.cloud.store import Store
from varyz.retrieval.search import fts_escape


def evaluate(cache: Path, output: Path) -> dict:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/cloud_archive_retrieval.json"
    )
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    output.mkdir(parents=True, exist_ok=True)
    embedder = CachedEmbedder(str(cache.resolve()))
    report = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "model": MODEL,
        "metrics": "source retrieval, not generated-answer accuracy",
        "bootstrap": "paired document clusters, 2000 resamples, seed 1207",
        "semantic_threshold": 0.30,
        "pooling": "fastembed mean pooling; regenerate document vectors with this runtime",
        "splits": {},
    }
    for split, records in fixture.items():
        db_path = output / (split + ".sqlite")
        if db_path.exists():
            raise ValueError("Use a fresh evaluation output directory")
        store = Store(db_path)
        with store.connection() as db:
            db.execute(
                "INSERT INTO accounts(id,email,password,status) "
                "VALUES('fixture','fixture@example.test','unused','active')"
            )
        archive = Archive(
            store,
            "http://localhost",
            embedder=embedder,
            encryption_key=Fernet.generate_key().decode(),
        )
        baseline = sqlite3.connect(":memory:")
        baseline.execute("CREATE VIRTUAL TABLE documents USING fts5(id UNINDEXED,title,text)")
        vectors = embedder.embed_documents([record[1] for record in records])
        for record, vector in zip(records, vectors, strict=True):
            identity, text, _, _ = record
            doc = Document(
                id=identity,
                project="fixture",
                source="documents",
                external_id=identity,
                title=identity,
                text=text,
                occurred_at="2026-10-01T00:00:00Z",
                sensitivity="personal",
                model=MODEL,
                chunks=[Chunk(text=text, vector=vector)],
            )
            archive.push("fixture", [Change(id=identity, base_version=0, payload=doc)])
            baseline.execute("INSERT INTO documents VALUES(?,?,?)", (identity, identity, text))
        results = []
        timings = []
        for identity, _, exact, paraphrase in records:
            for kind, query in (("exact", exact), ("paraphrase", paraphrase)):
                local = [
                    r[0]
                    for r in baseline.execute(
                        "SELECT id FROM documents WHERE documents MATCH ? ORDER BY "
                        "bm25(documents),id LIMIT 3",
                        (fts_escape(query),),
                    )
                ]
                archive.embedder = None
                lexical = [r["id"] for r in archive.search("fixture", query, limit=3)["results"]]
                archive.embedder = embedder
                start = time.perf_counter()
                hybrid = [r["id"] for r in archive.search("fixture", query, limit=3)["results"]]
                timings.append((time.perf_counter() - start) * 1000)
                results.append(
                    {
                        "id": identity,
                        "kind": kind,
                        "query": query,
                        "local_keyword": local,
                        "cloud_keyword": lexical,
                        "cloud_hybrid": hybrid,
                    }
                )
        metrics = {}
        for arm in ("local_keyword", "cloud_keyword", "cloud_hybrid"):
            metrics[arm] = {
                "top1": sum(r[arm][:1] == [r["id"]] for r in results) / len(results),
                "recall3": sum(r["id"] in r[arm] for r in results) / len(results),
            }
        # Exact/paraphrase questions from one document form a cluster.
        diffs = [
            sum(
                int(r["id"] in r["cloud_hybrid"]) - int(r["id"] in r["local_keyword"])
                for r in results
                if r["id"] == identity
            )
            / 2
            for identity, _, _, _ in records
        ]
        rng = random.Random(1207)
        draws = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(2000))
        negatives = ["purple submarine quantum unicorn", "my dog's veterinary invoice amount"]
        abstentions = [not archive.search("fixture", q)["results"] for q in negatives]
        report["splits"][split] = {
            "documents": len(records),
            "queries": len(results),
            "chance_top1": 1 / len(records),
            "chance_recall3": 3 / len(records),
            "current_cloud_recall3": 0,
            "scores": metrics,
            "paired_recall3_lift_ci95": [draws[49], draws[1949]],
            "unanswerable_abstentions": sum(abstentions),
            "unanswerable_queries": len(negatives),
            "hybrid_median_ms": statistics.median(timings),
            "hybrid_max_ms": max(timings),
            "encrypted_db_bytes": db_path.stat().st_size,
            "failures": [r for r in results if r["cloud_hybrid"][:1] != [r["id"]]],
        }
    report["replicated_recall3_nonregression"] = all(
        report["splits"][s]["scores"]["cloud_hybrid"]["recall3"]
        >= report["splits"][s]["scores"]["local_keyword"]["recall3"]
        for s in ("held_out", "replication")
    )
    (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.cache, args.output), indent=2))
