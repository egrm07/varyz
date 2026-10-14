"""Held-out graph extraction evaluation; never reads the active archive."""

from __future__ import annotations

from dataclasses import dataclass

from varyz.domain import Entry
from varyz.graph.models import ExtractionBatch, GraphExtractor


@dataclass(frozen=True)
class Score:
    correct: int
    predicted: int
    expected: int

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else float(self.expected == 0)

    @property
    def recall(self) -> float:
        return self.correct / self.expected if self.expected else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0


@dataclass(frozen=True)
class GraphEvaluation:
    entities: Score
    claims: Score
    modality_accuracy: float
    temporal_accuracy: float
    speaker_accuracy: float
    evidence_quote_validity: float
    unsupported_claims: int


def _claim_key(claim) -> tuple[str, str]:
    return claim.predicate, claim.object_surface.casefold()


def evaluate(
    extractor: GraphExtractor,
    corpus: list[tuple[Entry, dict]],
) -> GraphEvaluation:
    predicted_entities: set[tuple[str, str]] = set()
    expected_entities: set[tuple[str, str]] = set()
    predicted_claims: set[tuple[str, str]] = set()
    expected_claims: set[tuple[str, str]] = set()
    modality_correct = 0
    temporal_correct = 0
    speaker_correct = 0
    matched = 0
    quote_total = 0
    quote_valid = 0
    unsupported = 0

    for entry, gold in corpus:
        batch: ExtractionBatch = extractor.extract(entry)
        for alias in batch.aliases:
            predicted_entities.add(
                (str(alias["entity_type"]), str(alias["label"]).casefold())
            )
        for entity in gold.get("entities", []):
            expected_entities.add(
                (str(entity["entity_type"]), str(entity["label"]).casefold())
            )
        by_key = {_claim_key(claim): claim for claim in batch.claims}
        predicted_claims.update(by_key)
        for claim in batch.claims:
            if claim.origin != "user" and not claim.evidence:
                unsupported += 1
            for evidence in claim.evidence:
                quote_total += 1
                quote_valid += int(
                    evidence.entry_id == entry.id and evidence.quote in entry.body
                )
        for expected in gold.get("claims", []):
            key = (expected["predicate"], expected["object_surface"].casefold())
            expected_claims.add(key)
            actual = by_key.get(key)
            if actual is None:
                continue
            matched += 1
            modality_correct += int(str(actual.modality) == expected["modality"])
            temporal_correct += int(
                (
                    actual.valid_from.isoformat()[:10]
                    if actual.valid_from
                    else None
                )
                == expected.get("valid_from")
            )
            speaker_correct += int(actual.asserted_by_key == expected.get("asserted_by_key"))

    entity_correct = len(predicted_entities & expected_entities)
    claim_correct = len(predicted_claims & expected_claims)
    return GraphEvaluation(
        entities=Score(entity_correct, len(predicted_entities), len(expected_entities)),
        claims=Score(claim_correct, len(predicted_claims), len(expected_claims)),
        modality_accuracy=modality_correct / matched if matched else 0.0,
        temporal_accuracy=temporal_correct / matched if matched else 0.0,
        speaker_accuracy=speaker_correct / matched if matched else 0.0,
        evidence_quote_validity=quote_valid / quote_total if quote_total else 1.0,
        unsupported_claims=unsupported,
    )
