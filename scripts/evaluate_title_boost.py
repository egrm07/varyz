"""Frozen, offline title-boost safety study; keeps adaptive scanning in both arms."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_public_regression import DeterministicHashEmbedder  # noqa: E402

from varyz.db import dao  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.domain import Kind, NormalizedEntry, SearchHit, Sensitivity  # noqa: E402
from varyz.retrieval.capsule import DEFAULT_SENSITIVITY, to_capsules  # noqa: E402
from varyz.retrieval.search import _STOPWORDS, hybrid_search  # noqa: E402

FIXTURE = ROOT / "evals" / "title_boost_v2.json"
MANIFEST = ROOT / "evals" / "title_boost_v2_manifest.json"
CATEGORIES = (
    "exact_lookup",
    "phrase_lookup",
    "natural_language",
    "stale",
    "draft",
    "conflict",
    "related_not_answer",
    "token_boundaries",
    "unicode",
    "multilingual",
    "privacy",
    "excluded_source",
)
TOPICS = {
    "dev": ("Briar", "Onyx", "Aster", "Mica"),
    "held_out": ("Cobalt", "Willow", "Linden", "Coral"),
    "replication": ("Saffron", "Cypress", "Pebble", "Indigo"),
}


def title_match(query: str, title: str) -> bool:
    """Exactly the original experimental predicate; deliberately no new guard/tuning."""
    q = re.findall(r"[^\W_]+", query.casefold())
    t = re.findall(r"[^\W_]+", title.casefold())
    if not q:
        return False
    content = [word for word in q if word not in _STOPWORDS]
    phrase = any(t[i : i + len(q)] == q for i in range(len(t)))
    return q == t or (len(content) >= 2 and phrase)


def boost(hits: list[SearchHit], query: str) -> tuple[list[SearchHit], list[str]]:
    boosted = []
    copied = []
    for hit in hits:
        factor = 1.25 if title_match(query, hit.entry.title or "") else 1.0
        if factor != 1:
            boosted.append(hit.entry.external_id)
        copied.append(hit.model_copy(update={"score": hit.score * factor}))
    return sorted(copied, key=lambda hit: hit.score, reverse=True), boosted


def document(title: str, body: str, **extras: Any) -> dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "date": "2026-09-15T12:00:00Z",
        "source": "manual",
        "sensitivity": "public",
        **extras,
    }


def make_case(split: str, category: str, variant: int) -> dict[str, Any]:
    topic = TOPICS[split][variant]
    subject = f"{topic} {category.replace('_', ' ')}"
    goal = "The directly named document is the requested source."
    excluded: list[str] = []
    expected = ["answer"]
    prefix = "Context and discussion. " * (20 if variant % 2 == 0 else 2)
    if category in {"exact_lookup", "phrase_lookup"}:
        query = f"{topic} Relay Handbook"
        title = query if category == "exact_lookup" else f"Detailed {query} Reference"
        answer = document(title, f"{query}: the approved relay owner is Mara. " + prefix)
        wrong = document("Meeting index", f"{query} " * 4 + "Mention only; no handbook contents.")
    elif category == "natural_language":
        query = f"Who maintains the {topic} relay handbook?"
        answer = document(f"{topic} Relay Handbook", f"Mara maintains the {topic} relay handbook.")
        wrong = document(
            "Maintenance meeting",
            f"Who maintains the {topic} relay handbook was asked; nobody answered.",
        )
        goal = "A source naming the maintainer is required, rather than a repeated question."
    elif category == "stale":
        query = f"Current {topic} access policy"
        answer = document(
            f"{topic} Access Revision",
            f"Current {topic} access policy: badges expire after seven days. "
            "This supersedes the old thirty-day limit.",
            date="2026-09-29T12:00:00Z",
        )
        wrong = document(
            query,
            f"Obsolete: {topic} access policy once allowed thirty days. "
            "This page is withdrawn; read the September revision.",
            date="2025-01-01T12:00:00Z",
        )
        goal = (
            "The question explicitly asks for current policy; withdrawn history is not the answer."
        )
    elif category == "draft":
        query = f"Approved {topic} budget"
        answer = document(
            "Finance sign-off",
            f"Approved {topic} budget is 800 credits. "
            "Signed and final; this replaces the unapproved draft.",
        )
        wrong = document(
            query,
            f"UNAPPROVED DRAFT: {topic} budget proposal is 900 credits. "
            "Do not treat this as the approved budget.",
        )
        goal = "Approval is established by the document body; the draft title is misleading."
    elif category == "conflict":
        query = f"Compare {topic} retention versions"
        answer = document(
            f"{topic} Retention 2025",
            f"Old {topic} retention version kept records for ninety days.",
            date="2025-01-01T12:00:00Z",
        )
        wrong = document(
            f"{query} index",
            f"Compare {topic} retention versions: these are navigation links, not the policy text.",
        )
        expected = ["answer", "second"]
        goal = "Both versions are required; a title-matching index is not policy evidence."
    elif category == "related_not_answer":
        query = f"{topic} outage cause"
        answer = document(
            f"{topic} Investigation",
            f"The {topic} outage cause was an expired certificate. "
            "The cause is confirmed by the incident review.",
        )
        wrong = document(
            query,
            f"The {topic} outage cause is unknown in this early status note. "
            "We have no conclusion yet.",
        )
        goal = (
            "The source explaining the cause is required; "
            "a source saying it is unknown cannot answer."
        )
    elif category == "token_boundaries":
        query = ("Art", "of the", "Plan", "Relay")[variant]
        answer = document(
            query, f"This is the document named {query}. Its reference code is {topic}."
        )
        wrong = document(
            ("Bartholomew", "Roof of the Hall", "Planetary Notes", "Relayed Ideas")[variant],
            f"An unrelated {topic} note about workshops and scheduling.",
        )
    elif category == "unicode":
        query, title = (
            (f"Café {topic}", f"Cafe\u0301 {topic}"),
            (f"{topic} Ｐｌａｎ", f"{topic} Plan"),
            (f"Straße {topic}", f"STRASSE {topic}"),
            (f"{topic}-REQ_82", f"{topic} REQ 82"),
        )[variant]
        answer = document(
            title, f"Canonical Unicode-equivalent title. Reference {topic} is approved. " + prefix
        )
        wrong = document(
            "Index of references", (query + " ") * 4 + "A mention only; not the actual reference."
        )
        goal = (
            "The canonical reference is requested despite equivalent Unicode or punctuation forms."
        )
    elif category == "multilingual":
        query, title, body, other = (
            (
                f"Wer wartet die {topic} Pumpe?",
                f"{topic} Wartungsplan",
                f"Mara wartet die {topic} Pumpe jeden Dienstag.",
                f"Wer wartet die {topic} Pumpe ist noch offen; hier gibt es keine Antwort.",
            ),
            (
                f"Кто обслуживает насос {topic}?",
                f"План обслуживания {topic}",
                f"Насос {topic} обслуживает Мара каждый вторник.",
                f"Кто обслуживает насос {topic} пока неизвестно. Ответа нет.",
            ),
            (
                f"{topic}维护手册",
                f"{topic}维护手册",
                f"{topic}维护手册：负责人是玛拉。每周二检查泵。",
                f"{topic}维护手册只是目录中的提及，没有手册内容。",
            ),
            (
                f"Who maintains the {topic} pump?",
                f"{topic} Wartungsplan",
                f"Mara wartet die {topic} Pumpe jeden Dienstag.",
                f"Who maintains the {topic} pump has not been established in this meeting note.",
            ),
        )[variant]
        answer = document(title, body)
        wrong = document(query if variant < 2 else "Meeting index", other)
        goal = (
            "The source actually naming the maintainer/reference is needed, across language forms."
        )
    elif category == "privacy":
        query = f"{topic} personal access"
        answer = document(
            "Public access summary",
            f"{topic} personal access is handled through the public support desk.",
        )
        wrong = document(
            query,
            f"SYNTHETIC PRIVATE CANARY {topic}: protected access details.",
            sensitivity="sensitive" if variant % 2 == 0 else "unclassified",
        )
        if variant >= 2:
            answer["sensitivity"] = "sensitive"
            expected = []
        goal = (
            "Only allowed evidence may be returned; protected-only cases must return no evidence."
        )
    else:
        query = f"{topic} release notes"
        answer = document(
            "Owner release summary", f"{topic} release notes: the approved fix ships on Tuesday."
        )
        wrong = document(
            query,
            f"Excluded third-party {topic} release notes. Unapproved release date is Friday.",
            source="documents",
        )
        excluded = ["documents"]
        goal = "The excluded source must never consume an output evidence slot."
    docs = [{"id": "answer", **answer}, {"id": "wrong", **wrong}]
    if category == "conflict":
        docs.append(
            {
                "id": "second",
                **document(
                    f"{topic} Retention 2026",
                    f"New {topic} retention version keeps records for fourteen days "
                    "and supersedes the ninety-day policy.",
                    date="2026-09-29T12:00:00Z",
                ),
            }
        )
    if not (category == "privacy" and variant >= 2):
        docs.extend(
            [
                {
                    "id": "context",
                    **document(
                        f"{topic} planning",
                        f"Planning notes mention {subject}, "
                        "but the relevant answer is in a separate document.",
                    ),
                },
                {
                    "id": "noise",
                    **document(
                        "Office notice",
                        f"The {topic} office meeting starts at noon; "
                        "no decision on the requested topic is recorded.",
                    ),
                },
            ]
        )
    return {
        "id": f"{split}-{category}-{variant}",
        "split": split,
        "category": category,
        "variant": variant,
        "query": query,
        "goal": goal,
        "expected": expected,
        "excluded_sources": excluded,
        "documents": docs,
    }


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def freeze() -> None:
    if FIXTURE.exists() or MANIFEST.exists():
        raise ValueError(
            "Frozen fixture already exists; create a new version rather than overwrite."
        )
    cases = [
        make_case(split, category, variant)
        for split in TOPICS
        for category in CATEGORIES
        for variant in range(4)
    ]
    payload = {
        "version": "title-boost-v2",
        "privacy": "All sources and facts are synthetic.",
        "cases": cases,
    }
    FIXTURE.write_bytes(canonical(payload))
    MANIFEST.write_bytes(
        canonical(
            {
                "fixture_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
                "counts": {split: 48 for split in TOPICS},
                "candidate": "Original casefold/token-run boost, factor 1.25",
                "frozen_before_scoring": True,
                "primary": "First relevant source; complete@3 requires both conflict versions",
                "gate": (
                    "No new harmful top-1 changes in held-out and replication; "
                    "positive lift in both."
                ),
                "splits": "Independent topic families; cluster analysis by scenario category.",
            }
        )
    )
    print(MANIFEST.read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if hashlib.sha256(canonical(payload)).hexdigest() != manifest["fixture_sha256"]:
        raise ValueError("Fixture hash differs from its pre-score manifest.")
    return payload["cases"]


class CachedEmbedder:
    model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(
            self.model_name,
            cache_dir=str(ROOT / "data" / "models"),
            local_files_only=True,
            threads=2,
            providers=["CPUExecutionProvider"],
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.embed(texts, batch_size=16)]

    def embed_query(self, text: str) -> list[float]:
        return list(self.model.query_embed(text))[0].tolist()


def evaluate_case(
    case: dict[str, Any],
    embedder: Any,
    conn: Any,
    vectors: dict[str, list[float]],
    query_vector: list[float],
) -> dict[str, Any]:
    with conn:
        conn.execute("DELETE FROM vec_chunks")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM entries")
        for doc in case["documents"]:
            entry_id, _ = dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.NOTE,
                    external_id=doc["id"],
                    title=doc["title"],
                    body=doc["body"],
                    occurred_start=datetime.fromisoformat(doc["date"].replace("Z", "+00:00")),
                ),
                source=doc["source"],
                sensitivity=Sensitivity(doc["sensitivity"]),
            )
            text = doc["title"] + "\n" + doc["body"]
            chunks = dao.replace_chunks(conn, entry_id, [text])
            dao.store_embeddings(conn, chunks, [vectors[text]], embedder.model_name)

    class QueryVector:
        model_name = embedder.model_name

        def embed_query(self, text: str) -> list[float]:
            # All questions in this study use the ordinary route; never fake a paraphrase.
            return query_vector if text == case["query"] else embedder.embed_query(text)

    started = time.perf_counter()
    hits = hybrid_search(
        conn,
        case["query"],
        QueryVector(),
        limit=30,
        pool=30,
        excluded_sources=tuple(case["excluded_sources"]),
    )
    retrieval_ms = (time.perf_counter() - started) * 1000
    candidate, boosted_ids = boost(hits, case["query"])
    expected = set(case["expected"])
    row = {
        "id": case["id"],
        "category": case["category"],
        "query": case["query"],
        "goal": case["goal"],
        "expected": case["expected"],
        "boosted": boosted_ids,
        "retrieval_ms": retrieval_ms,
    }
    for arm, ranked in (("baseline", hits), ("title", candidate)):
        capsules, withheld = to_capsules(ranked, allowed=DEFAULT_SENSITIVITY, max_chars=900)
        refs = [capsule.external_id for capsule in capsules]
        score = {}
        for k in (1, 3):
            score[f"success@{k}"] = not refs if not expected else expected <= set(refs[:k])
        score["first_relevant"] = not refs if not expected else bool(refs and refs[0] in expected)
        ranks = [refs.index(doc) + 1 for doc in expected if doc in refs]
        score["complete_mrr"] = 1 / max(ranks) if len(ranks) == len(expected) and ranks else 0
        forbidden = {
            doc["id"]
            for doc in case["documents"]
            if doc["sensitivity"] not in {"public", "personal"}
            or doc["source"] in case["excluded_sources"]
        }
        row[arm] = {
            **score,
            "references": refs,
            "withheld": withheld,
            "forbidden_returned": sorted(forbidden & set(refs)),
        }
    # Top-1 metric is relevant-first for conjunctive conflict questions, complete@3 secondary.
    row["helped"] = not row["baseline"]["first_relevant"] and row["title"]["first_relevant"]
    row["harmed"] = row["baseline"]["first_relevant"] and not row["title"]["first_relevant"]
    row["lost_complete@3"] = row["baseline"]["success@3"] and not row["title"]["success@3"]
    eligible = [
        doc
        for doc in case["documents"]
        if doc["sensitivity"] in {"public", "personal"}
        and doc["source"] not in case["excluded_sources"]
    ]
    row["chance_first_relevant"] = (
        len(expected) / len(eligible) if eligible else float(not expected)
    )
    return row


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in records:
        grouped[row["category"]].append(
            int(row["title"]["first_relevant"]) - int(row["baseline"]["first_relevant"])
        )
    rng = random.Random(9421)
    groups = list(grouped.values())
    samples = []
    for _ in range(4000):
        drawn = rng.choices(groups, k=len(groups))
        samples.append(statistics.mean(value for group in drawn for value in group))
    samples.sort()
    return {
        "n": len(records),
        "chance_first_relevant": statistics.mean(row["chance_first_relevant"] for row in records),
        "baseline": {
            metric: statistics.mean(row["baseline"][metric] for row in records)
            for metric in ("first_relevant", "success@3", "complete_mrr")
        },
        "title": {
            metric: statistics.mean(row["title"][metric] for row in records)
            for metric in ("first_relevant", "success@3", "complete_mrr")
        },
        "helped": sum(row["helped"] for row in records),
        "harmed": sum(row["harmed"] for row in records),
        "lost_complete@3": sum(row["lost_complete@3"] for row in records),
        "cluster_delta_ci95": [samples[100], samples[3899]],
        "boosted_cases": sum(bool(row["boosted"]) for row in records),
        "forbidden_returned": sum(
            len(row[arm]["forbidden_returned"]) for row in records for arm in ("baseline", "title")
        ),
    }


def run(model: str, splits: list[str], *, challenge: bool = False) -> dict[str, Any]:
    if challenge:
        path = ROOT / "evals" / "title_boost_challenges_v1.json"
        manifest = json.loads(
            (ROOT / "evals" / "title_boost_challenges_v1_manifest.json").read_text(encoding="utf-8")
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if hashlib.sha256(canonical(payload)).hexdigest() != manifest["fixture_sha256"]:
            raise ValueError("Challenge fixture changed after freezing.")
        cases = payload["cases"]
        splits = ["challenge"]
    else:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cases = [case for case in load_cases() if case["split"] in splits]
    embedder = CachedEmbedder() if model == "cached" else DeterministicHashEmbedder()
    texts = list(
        dict.fromkeys(
            doc["title"] + "\n" + doc["body"] for case in cases for doc in case["documents"]
        )
    )
    vectors = dict(zip(texts, embedder.embed_documents(texts), strict=True))
    queries = {case["id"]: embedder.embed_query(case["query"]) for case in cases}
    records = []
    with tempfile.TemporaryDirectory(prefix="varyz-title-v2-") as directory:
        conn = open_db(Path(directory) / "fixture.db")
        try:
            for case in cases:
                records.append(
                    {
                        "split": case["split"],
                        **evaluate_case(case, embedder, conn, vectors, queries[case["id"]]),
                    }
                )
        finally:
            conn.close()
    summaries = {
        split: summarize([row for row in records if row["split"] == split]) for split in splits
    }
    categories = {
        split: {
            category: summarize(
                [row for row in records if row["split"] == split and row["category"] == category]
            )
            for category in CATEGORIES
            if any(row["split"] == split and row["category"] == category for row in records)
        }
        for split in splits
    }
    promotion = all(
        summaries[split]["title"]["first_relevant"] > summaries[split]["baseline"]["first_relevant"]
        and summaries[split]["harmed"] == 0
        and summaries[split]["lost_complete@3"] == 0
        and summaries[split]["forbidden_returned"] == 0
        for split in splits
        if split != "dev"
    ) and {"held_out", "replication"} <= set(splits)
    return {
        "date": "2026-10-04",
        "model": embedder.model_name,
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture_sha256": manifest["fixture_sha256"],
        "production_search_sha256": hashlib.sha256(
            (ROOT / "src/varyz/retrieval/search.py").read_bytes()
        ).hexdigest(),
        "factor": 1.25,
        "adaptive_scanning": "retained in both arms",
        "primary": "first relevant source, with allowed abstention in protected-only cases",
        "summaries": summaries,
        "categories": categories,
        "promotion_gate_passed": promotion,
        "records": records,
        "failures": [
            row
            for row in records
            if row["harmed"] or row["lost_complete@3"] or not row["title"]["first_relevant"]
        ],
        "caveats": [
            "Synthetic source relevance, not generated-answer or preference-resolution quality.",
            "Scenario variants are correlated; bootstrap resamples categories.",
            "The frozen candidate never interprets dates, approvals, contradictions or trust.",
            "Read-path privacy/source filters are exercised; broader security was not audited.",
            "No private archive, new model download, cloud call or installed-service update.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--challenge", action="store_true")
    parser.add_argument("--model", choices=("hash", "cached"), default="hash")
    parser.add_argument("--split", choices=(*TOPICS, "all"), default="dev")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.freeze:
        freeze()
        return
    splits = list(TOPICS) if args.split == "all" else [args.split]
    result = run(args.model, splits, challenge=args.challenge)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(canonical(result))
    print(
        json.dumps(
            {
                "model": result["model"],
                "summaries": result["summaries"],
                "promotion_gate_passed": result["promotion_gate_passed"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
