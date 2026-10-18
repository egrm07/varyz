"""A second look at what tier two is about to answer with.

Tier two answers from the archive when some returned record holds half the
question's words in one place (``varyz.retrieval.search.evidence_coverage``).
Words the archive never uses cannot be held by any record, so they drop out of
that count: "How much did I pay for the kayak?" in an archive with no kayak in
it is "much" and "pay", and a note that says "paid much more than the quote"
holds both. The subject is exactly the word left out.

So before tier two answers, the local model (loopback only, as in
``varyz.resolution.local_classifier``) reads each of the top few records against
the question: is this record about the thing asked? One yes answers, as one
covering record does. None leaves the question to the owner. When the model
cannot answer (Ollama down, the model not pulled, the card busy), tier two
answers as it did before, on the coverage check alone.

Measured with recall_bench_v1 and TRR (tier two alone, keyword retrieval),
coverage alone against coverage and this check. Everything was set on dev.

held_out:
  - off-topic answers to unanswerable questions 2/5 -> 0/5
  - wrong answers 11 -> 4
  - right answers 9 -> 11 of 27
  - TRR unchanged at 8/8

sealed:
  - wrong answers 11 -> 5
  - right answers 9 -> 9

No right answer was lost on any split.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from varyz.resolution.local_classifier import ClassifierUnavailable, LocalModel, Question

EVIDENCE_PROMPT = (
    "An agent asks a question about its owner's own past, work or preferences. Shown is "
    "one record from the owner's archive that a keyword search returned. Answer A if the "
    "record is about the specific thing the question asks about (the same object, person, "
    "project, event or decision) and says something an answer needs, even if only one part "
    "of it or in other words. Answer B if the record only shares some words or a general "
    "topic with the question, or is about a different thing. Answer with the single "
    "letter A or B."
)


def pair_text(question: str, record: str) -> str:
    return f"Question: {' '.join(question.split())} | Record: {' '.join(record.split())[:900]}"


# Authored for this check and taken from no benchmark and no owner data.
EVIDENCE_FEW_SHOT: tuple[tuple[str, str], ...] = (
    (pair_text("How much did I pay for the new tent?",
               "Paid the plumber, it was much more than the quote. Need to sort the "
               "bathroom tiles."), "B"),
    (pair_text("What did I promise Lena?",
               "Lena asked for the launch draft by the 12th, told her I'd have it done."), "A"),
    (pair_text("Which hotel did we book in Lisbon?",
               "Booked the train to Porto for Friday; the hotel there is by the river."), "B"),
    (pair_text("Why did we switch the build to GitHub Actions?",
               "Moved CI off the self-hosted runner: it kept running out of disk and nobody "
               "owned it."), "A"),
    (pair_text("Which colour did we pick for the Lumen landing page?",
               "Lumen pricing page: three tiers, annual discount 20%, FAQ at the bottom."), "B"),
    (pair_text("When is the dentist appointment?",
               "Calendar: Dr Okafor (dental), Thu 14 Nov 09:30, 12 High St."), "A"),
)

EVIDENCE_QUESTION = Question(
    name="evidence", instruction=EVIDENCE_PROMPT, few_shot=EVIDENCE_FEW_SHOT,
    input_label="Pair",
)

TOP_K = 3
# P(on topic) one record must reach; on dev every right answer had a record at
# 0.94 or more, and 0.3 to 0.7 all decided the same cases.
THRESHOLD = 0.5


class EvidenceVerifier:
    """Say whether any of the top records is about the question."""

    def __init__(
        self, model: LocalModel, *, top_k: int = TOP_K, threshold: float = THRESHOLD
    ) -> None:
        self.model = model
        self.top_k = top_k
        self.threshold = threshold
        self.skipped: list[str] = []
        self.last: list[float] = []  # P(on topic) of each record the last call read

    def on_topic(self, question: str, records: Sequence[str]) -> bool | None:
        """True at the first record on the question, False if none is, None if the
        model could not answer."""
        self.last = []
        try:
            for record in records[: self.top_k]:
                p = self.model.p_a(EVIDENCE_QUESTION, pair_text(question, record))
                self.last.append(p)
                if p >= self.threshold:
                    return True
        except ClassifierUnavailable as exc:
            self.skipped.append(str(exc))
            return None
        return False


def evidence_verifier_from_config(cfg: Any) -> EvidenceVerifier | None:
    """`[workflows] evidence_verifier`: the verifier, or None for the coverage check only."""
    if cfg.workflows.evidence_verifier != "model":
        return None
    from varyz.resolution.local_classifier import local_model_from_config

    return EvidenceVerifier(local_model_from_config(cfg))


__all__ = [
    "EVIDENCE_QUESTION",
    "THRESHOLD",
    "TOP_K",
    "EvidenceVerifier",
    "evidence_verifier_from_config",
    "pair_text",
]
