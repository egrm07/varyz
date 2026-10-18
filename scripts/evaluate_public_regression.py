"""Offline, reproducible retrieval regression over the committed public corpus.

This is deliberately separate from ``evaluate_retrieval.py``.  It builds a
fresh Varyz database, runs the real documents connector, ingest, chunking,
embedding and hybrid-search paths, and uses a deterministic local test
embedder.  It never reads the user's config, archive, network, or model cache.

Usage:
    uv run python scripts/evaluate_public_regression.py --split held_out --gate
    uv run python scripts/evaluate_public_regression.py --split all --json-out report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import Config, StorageConfig  # noqa: E402
from varyz.connectors.documents import DocumentsConnector  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.pipeline.embed import embed_pending  # noqa: E402
from varyz.pipeline.ingest import sync_connector  # noqa: E402
from varyz.retrieval.search import hybrid_search, keyword_search  # noqa: E402
from varyz.vault import Vault  # noqa: E402

CORPUS = ROOT / "evals" / "public_corpus"
TASKS = ROOT / "evals" / "public_golden.jsonl"
MANIFEST = ROOT / "evals" / "public_manifest.json"
KS = (1, 5, 10)
MIN_TASKS = 46
MAX_TARGET_SHARE = 0.05
NO_ANSWER_MAX_DISTANCE = 0.75

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "about", "after", "an", "and", "are", "as", "at", "be", "been", "before",
        "by", "did", "do", "does", "for", "from", "had", "has", "have", "how", "in",
        "into", "is", "it", "of", "on", "or", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "to", "was", "were", "what", "when", "where",
        "which", "who", "why", "will", "with", "would", "your",
    }
)


@dataclass(frozen=True)
class Task:
    id: str
    question: str
    expected: tuple[str, ...]
    expect_none: bool
    split: str
    tags: tuple[str, ...]
    source_types: tuple[str, ...]


class DeterministicHashEmbedder:
    """Small, offline bag-of-terms vectors; no fitted state or golden labels.

    This is an execution fixture, not a stand-in quality claim for MiniLM.  It
    makes the vector-index branch deterministic in CI while FTS5 and RRF still
    run unchanged.  Stable SHA-256 feature hashing avoids Python hash seeds.
    """

    model_name = "public-eval-hash-384-v1"
    dimensions = 384

    @staticmethod
    def _terms(text: str) -> list[str]:
        return [term for term in _TOKEN.findall(text.lower()) if term not in _STOPWORDS]

    def _vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        terms = self._terms(text)
        features = [*terms, *(f"{a}:{b}" for a, b in zip(terms, terms[1:], strict=False))]
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign * (1.5 if ":" in feature else 1.0)
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def corpus_hash(root: Path = CORPUS) -> str:
    """Hash relative names and bytes, independent of checkout location."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def task_hash(path: Path = TASKS) -> str:
    # JSONL has no explicit .gitattributes EOL rule.  Text mode normalises a
    # Windows checkout's CRLF so the frozen task hash is portable.
    canonical = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return _sha256(canonical.encode("utf-8"))


def split_hash(tasks: list[Task], split: str) -> str:
    rows = [task.id for task in tasks if task.split == split]
    return _sha256(("\n".join(rows) + "\n").encode("utf-8"))


def load_tasks(path: Path = TASKS) -> list[Task]:
    tasks: list[Task] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        tasks.append(
            Task(
                id=raw["id"],
                question=raw["question"],
                expected=tuple(raw.get("expect_all", [])),
                expect_none=bool(raw.get("expect_none", False)),
                split=raw["split"],
                tags=tuple(raw.get("tags", [])),
                source_types=tuple(raw.get("source_types", [])),
            )
        )
    return tasks


def validate_fixture(tasks: list[Task]) -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = {
        "corpus_sha256": corpus_hash(),
        "tasks_sha256": task_hash(),
        "dev_ids_sha256": split_hash(tasks, "dev"),
        "held_out_ids_sha256": split_hash(tasks, "held_out"),
    }
    expected_hashes = {key: manifest[key] for key in actual}
    if actual != expected_hashes:
        differences = {
            key: {"expected": expected_hashes[key], "actual": actual[key]}
            for key in actual
            if actual[key] != expected_hashes[key]
        }
        raise ValueError(f"public evaluation fixture drifted: {differences}")
    if len(tasks) < MIN_TASKS:
        raise ValueError(f"public golden set has {len(tasks)} tasks; need at least {MIN_TASKS}")
    actual_files = sum(1 for path in CORPUS.rglob("*") if path.is_file())
    expected_counts = {
        "corpus_files": actual_files,
        "tasks": len(tasks),
        "dev_tasks": sum(task.split == "dev" for task in tasks),
        "held_out_tasks": sum(task.split == "held_out" for task in tasks),
    }
    for key, actual_count in expected_counts.items():
        if manifest.get(key) != actual_count:
            raise ValueError(
                f"public manifest {key}={manifest.get(key)!r}, actual={actual_count}"
            )
    ids = [task.id for task in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("public golden set contains duplicate task ids")
    if {task.split for task in tasks} != {"dev", "held_out"}:
        raise ValueError("public golden set must contain frozen dev and held_out splits")

    targets = Counter(target for task in tasks for target in task.expected)
    if targets and max(targets.values()) / len(tasks) > MAX_TARGET_SHARE:
        target, count = targets.most_common(1)[0]
        raise ValueError(f"target {target} answers {count}/{len(tasks)} tasks (>5%)")

    corpus_files = {
        path.relative_to(CORPUS).as_posix()
        for path in CORPUS.rglob("*")
        if path.is_file()
    }
    for task in tasks:
        if task.expect_none == bool(task.expected):
            raise ValueError(f"{task.id}: set exactly one of expect_none or expect_all")
        missing = set(task.expected) - corpus_files
        if missing:
            raise ValueError(f"{task.id}: expected sources missing from corpus: {sorted(missing)}")
        if "cross-source" in task.tags and (
            len(task.expected) < 2 or len(set(task.source_types)) < 2
        ):
            raise ValueError(f"{task.id}: cross-source task lacks two source classes")

    for split in ("dev", "held_out"):
        subset = [task for task in tasks if task.split == split]
        required = {"mid-file", "cross-source", "no-answer"}
        present = {tag for task in subset for tag in task.tags}
        if not required <= present:
            raise ValueError(f"{split} split lacks tags: {sorted(required - present)}")
    return {"manifest": manifest, "actual_hashes": actual, "target_counts": targets}


def _relative_identity(external_id: str) -> str:
    path = Path(external_id).resolve()
    return path.relative_to(CORPUS.resolve()).as_posix()


def _ids_to_sources(conn: Any, entry_ids: list[str]) -> list[str]:
    sources: list[str] = []
    for entry_id in entry_ids:
        row = conn.execute("SELECT external_id FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is not None:
            sources.append(_relative_identity(row["external_id"]))
    return sources


def _score_answerable(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"questions": len(records)}
    for k in KS:
        recalls: list[float] = []
        successes = 0
        for record in records:
            found = set(record["top"][:k]) & set(record["expected"])
            recalls.append(len(found) / len(record["expected"]))
            successes += found == set(record["expected"])
        summary[f"source_recall@{k}"] = round(sum(recalls) / len(recalls), 4)
        summary[f"task_success@{k}"] = round(successes / len(records), 4)

    first_ranks: list[float] = []
    complete_ranks: list[float] = []
    for record in records:
        ranks = [
            record["top"].index(target) + 1
            for target in record["expected"]
            if target in record["top"]
        ]
        first_ranks.append(1 / min(ranks) if ranks else 0.0)
        complete_ranks.append(
            1 / max(ranks) if len(ranks) == len(record["expected"]) else 0.0
        )
    summary["mrr_first_source"] = round(sum(first_ranks) / len(first_ranks), 4)
    summary["mrr_complete_evidence"] = round(sum(complete_ranks) / len(complete_ranks), 4)
    summary["missed_ids@10"] = [
        record["id"]
        for record in records
        if not set(record["expected"]) <= set(record["top"][:10])
    ]
    return summary


def _retrieve_fts(conn: Any, question: str, limit: int) -> list[str]:
    ids, _ = keyword_search(conn, question, limit)
    return _ids_to_sources(conn, ids)


def _retrieve_hybrid(
    conn: Any, question: str, embedder: DeterministicHashEmbedder, limit: int
) -> list[str]:
    hits = hybrid_search(conn, question, embedder, limit=limit, pool=30)
    return [_relative_identity(hit.entry.external_id) for hit in hits]


def evaluate(split: str = "held_out") -> dict[str, Any]:
    all_tasks = load_tasks()
    fixture = validate_fixture(all_tasks)
    selected = [task for task in all_tasks if split == "all" or task.split == split]
    if not selected:
        raise ValueError(f"split {split!r} contains no tasks")

    with tempfile.TemporaryDirectory(prefix="varyz-public-eval-") as raw_dir:
        work = Path(raw_dir)
        cfg = Config(
            storage=StorageConfig(data_dir=work / "data"),
            base_dir=work,
            connectors={
                "documents": {"approved": True, "paths": [str(CORPUS.resolve())]}
            },
        )
        # Force late passages into separate chunks so mid-file cases exercise
        # chunk retrieval instead of succeeding from a whole-file embedding.
        cfg.embedding.chunk_tokens = 45
        cfg.retrieval.distill = "off"
        conn = open_db(cfg.db_path)
        try:
            stats = sync_connector(conn, Vault(cfg.vault_dir), DocumentsConnector(), cfg)
            if stats.errors:
                raise RuntimeError(f"documents ingest produced {stats.errors} errors")
            embedder = DeterministicHashEmbedder()
            chunks = embed_pending(conn, cfg, embedder)
            entry_count = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

            records_by_arm: dict[str, list[dict[str, Any]]] = {"fts": [], "hybrid": []}
            no_answer: list[dict[str, Any]] = []
            for task in selected:
                if task.expect_none:
                    hits = hybrid_search(
                        conn,
                        task.question,
                        embedder,
                        limit=10,
                        abstain=True,
                        max_distance=NO_ANSWER_MAX_DISTANCE,
                    )
                    no_answer.append(
                        {
                            "id": task.id,
                            "question": task.question,
                            "returned": len(hits),
                            "top": [
                                _relative_identity(hit.entry.external_id) for hit in hits[:3]
                            ],
                        }
                    )
                    continue

                for arm, top in (
                    ("fts", _retrieve_fts(conn, task.question, 10)),
                    ("hybrid", _retrieve_hybrid(conn, task.question, embedder, 10)),
                ):
                    records_by_arm[arm].append(
                        {
                            "id": task.id,
                            "question": task.question,
                            "expected": list(task.expected),
                            "tags": list(task.tags),
                            "top": top,
                        }
                    )
        finally:
            conn.close()

    arm_summaries = {
        arm: _score_answerable(records) for arm, records in records_by_arm.items()
    }
    answerable_count = len(records_by_arm["hybrid"])
    corpus_size = fixture["manifest"]["corpus_files"]
    chance = {
        f"source_recall@{k}": round(min(k, corpus_size) / corpus_size, 4) for k in KS
    }
    false_count = sum(record["returned"] > 0 for record in no_answer)
    retrieval_failures = [
        {
            "type": "missing_required_source_top10",
            "id": record["id"],
            "question": record["question"],
            "missing": sorted(set(record["expected"]) - set(record["top"][:10])),
            "top": record["top"][:10],
        }
        for record in records_by_arm["hybrid"]
        if not set(record["expected"]) <= set(record["top"][:10])
    ]
    false_evidence_failures = [
        {"type": "false_evidence", **record}
        for record in no_answer
        if record["returned"] > 0
    ]
    report: dict[str, Any] = {
        "evaluation": "public_retrieval_regression_v1",
        "evaluator_sha256": _sha256(Path(__file__).read_bytes()),
        "primary_metric": "hybrid.source_recall@5",
        "split": split,
        "tasks": len(selected),
        "answerable_tasks": answerable_count,
        "no_answer_tasks": len(no_answer),
        "corpus_entries": entry_count,
        "embedded_chunks": chunks,
        "hashes": fixture["actual_hashes"],
        "configuration": {
            "embed_model": DeterministicHashEmbedder.model_name,
            "embed_dimensions": DeterministicHashEmbedder.dimensions,
            "chunk_tokens": 45,
            "retrieval_limit": 10,
            "retrieval_pool": 30,
            "no_answer_max_distance": NO_ANSWER_MAX_DISTANCE,
            "gate_min_source_recall@5": 0.90,
            "gate_min_task_success@10": 0.90,
            "gate_max_false_evidence_rate": 0.0,
        },
        "floors": {
            "chance_random_sources": chance,
            "lexical_fts": arm_summaries["fts"],
        },
        "hybrid": arm_summaries["hybrid"],
        "abstention": {
            "false_evidence_rate": round(false_count / len(no_answer), 4)
            if no_answer
            else None,
            "false_evidence_ids": [r["id"] for r in no_answer if r["returned"] > 0],
        },
        "failure_taxonomy": {
            "missing_required_source_top10": retrieval_failures,
            "false_evidence": false_evidence_failures,
        },
        "comparison_note": (
            "FTS is a paired lexical floor, not a defeated baseline; this gate makes no "
            "superiority or significance claim."
        ),
        "records": records_by_arm["hybrid"],
        "no_answer_records": no_answer,
        "caveats": [
            "Synthetic prose has cleaner lexical cues than a personal archive.",
            "Hash embeddings make CI reproducible but do not measure MiniLM semantic quality.",
            "Source classes are folders ingested through the documents connector, not live APIs.",
            "This measures evidence retrieval and abstention, not generated-answer correctness.",
            "Held-out results still need replication on a fresh, unrelated corpus before tuning.",
        ],
    }
    return report


def gate_failures(report: dict[str, Any]) -> list[str]:
    hybrid = report["hybrid"]
    failures: list[str] = []
    if hybrid["source_recall@5"] < 0.90:
        failures.append(f"source_recall@5 {hybrid['source_recall@5']:.3f} < 0.900")
    if hybrid["task_success@10"] < 0.90:
        failures.append(f"task_success@10 {hybrid['task_success@10']:.3f} < 0.900")
    false_rate = report["abstention"]["false_evidence_rate"]
    if false_rate is not None and false_rate > 0:
        failures.append(f"false_evidence_rate {false_rate:.3f} > 0.000")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "held_out", "all"), default="held_out")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--gate", action="store_true", help="fail if frozen thresholds regress")
    args = parser.parse_args(argv)

    try:
        report = evaluate(args.split)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PUBLIC EVAL INVALID: {exc}", file=sys.stderr)
        return 2

    hybrid = report["hybrid"]
    fts = report["floors"]["lexical_fts"]
    print(
        f"public retrieval  split={args.split}  tasks={report['tasks']}  "
        f"entries={report['corpus_entries']}"
    )
    print("  metric                 chance       fts    hybrid")
    for k in KS:
        key = f"source_recall@{k}"
        print(
            f"  {key:<22}{report['floors']['chance_random_sources'][key]:>7.3f}"
            f"{fts[key]:>10.3f}{hybrid[key]:>10.3f}"
        )
    print(
        f"  {'task_success@10':<22}{'-':>7}"
        f"{fts['task_success@10']:>10.3f}{hybrid['task_success@10']:>10.3f}"
    )
    print(
        f"  {'MRR complete':<22}{'-':>7}"
        f"{fts['mrr_complete_evidence']:>10.3f}{hybrid['mrr_complete_evidence']:>10.3f}"
    )
    print(
        f"  false evidence         {report['abstention']['false_evidence_rate'] or 0.0:>7.3f}"
    )
    if hybrid["missed_ids@10"]:
        print(f"  missed @10: {hybrid['missed_ids@10']}")
    print("  caveat: synthetic, deterministic retrieval gate; not a live-model quality claim")
    # The two columns invite a comparison the fixture cannot support.  The vector
    # half here is a bag-of-terms hash, so it is a weaker copy of FTS5 rather than
    # a second opinion, and fusing it can only pull the stronger ranker about.  A
    # hybrid column below the FTS one measures the fixture, not the retriever.
    print(
        "  caveat: hybrid runs on hash embeddings, which are lexical like FTS5 and "
        "not semantic like MiniLM; the two columns are a paired floor, not a contest"
    )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")

    failures = gate_failures(report) if args.gate else []
    if failures:
        print("GATE FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    if args.gate:
        print("GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
