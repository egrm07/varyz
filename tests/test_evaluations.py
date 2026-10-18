"""Held-out quality metrics and the adversarial privacy release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from varyz.domain import Entry, Kind, Sensitivity
from varyz.graph.evaluation import evaluate as evaluate_graph
from varyz.graph.extract import DeterministicExtractor
from varyz.privacy.evaluation import evaluate as evaluate_privacy
from varyz.privacy.models import (
    ContextItem,
    ContextRequest,
    Interaction,
    LabelSource,
)
from varyz.privacy.policy import PolicyEngine

FIXTURES = Path(__file__).parent / "fixtures"


def test_held_out_graph_corpus_clears_deterministic_baseline():
    raw = json.loads((FIXTURES / "graph_held_out.json").read_text(encoding="utf-8"))
    corpus = []
    for item in raw:
        entry = Entry(
            id=item["id"],
            kind=Kind.MESSAGE if item["body"].startswith("From:") else Kind.NOTE,
            source="manual",
            external_id=item["id"],
            content_hash=hashlib.sha256(item["body"].encode()).hexdigest(),
            body=item["body"],
            ingested_at="2026-01-01T00:00:00+00:00",
            sensitivity=Sensitivity.PERSONAL,
        )
        corpus.append(
            (
                entry,
                {"entities": item["entities"], "claims": item["claims"]},
            )
        )
    report = evaluate_graph(DeterministicExtractor(), corpus)
    assert report.claims.precision == 1.0
    assert report.claims.recall == 1.0
    assert report.modality_accuracy == 1.0
    assert report.evidence_quote_validity == 1.0
    assert report.unsupported_claims == 0


def test_adversarial_privacy_fixture_has_zero_unauthorized_egress():
    raw = json.loads(
        (FIXTURES / "privacy_adversarial.json").read_text(encoding="utf-8")
    )
    intent = "What time is the train?"
    request = ContextRequest(
        request_id="adversarial",
        actor="local_user",
        purpose="answer_question",
        destination="cloud:fake",
        intent=intent,
        intent_hash=hashlib.sha256(intent.encode()).hexdigest(),
        requested_types=("evidence_excerpt",),
        requested_scope={},
        max_items=20,
        max_chars=10000,
        interaction=Interaction.INTERACTIVE,
        user_present=True,
        reversible=False,
    )
    items = tuple(
        ContextItem(
            item_id=item["id"],
            resource_type="evidence_excerpt",
            resource_id=item["id"],
            content=item["content"],
            sensitivity=Sensitivity(item["sensitivity"]),
            label_source=LabelSource.DETERMINISTIC_RULE,
        )
        for item in raw
    )
    report = evaluate_privacy(PolicyEngine(), [(request, items, (), ())])
    assert report.release_gate_passed
    assert report.unauthorized_sensitive_egress == 0
    assert report.allowed_items == 1
