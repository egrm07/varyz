"""A second look at a reworded question the strict matcher could not place.

Tier one answers a question from the owner's stored decisions: exactly, or through
a strict word matcher that never answers wrong but misses about half of the
rewordings. When it finds nothing, this verifier ranks the owner's own applicable
decisions by the archive's sentence embedding, drops any that name a project or
product the question does not, and asks the local model (loopback only, as in
``varyz.resolution.local_classifier``) of each of the few nearest: does this
decision answer the new question? Only a single clear yes answers; two yeses, or
none, leave the question to the owner. A wrong owner answer is worse than asking,
Identical answers from independently verified records are not a conflict;
different answers still remain ambiguous. The bar is set on development data
to keep wrong answers at zero
(reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from varyz.resolution.local_classifier import (
    ClassifierUnavailable,
    LocalModel,
    Question,
    dot,
    unit,
)

VERIFY_PROMPT = (
    "An agent asks its owner's preference service a question. Shown is a decision "
    "the owner made earlier, as the question they were asked and their answer. "
    "Answer A only if that decision directly answers the new question: the same "
    "subject, for the same project, product or kind of work, and its answer settles "
    "what is asked. Answer B if it is about a different project, product, platform "
    "or kind of thing, if the new question asks why or when, or if the decision is "
    "only related. Answer with the single letter A or B."
)


def pair_text(query: str, stored_question: str, stored_answer: str) -> str:
    return (
        f"Decision: {' '.join(stored_question.split())} -> {' '.join(stored_answer.split())}"
        f" | New question: {' '.join(query.split())}"
    )


# Development pairs from evals/varyz_vision_v1_queries.jsonl (split "dev") against
# the synthetic knowledge in evals/varyz_vision_v1_knowledge.jsonl, and nothing
# else. No owner data.
VERIFY_FEW_SHOT: tuple[tuple[str, str], ...] = (
    (pair_text("What database is Kestrel built on?",
               "Which database does Kestrel use?", "Postgres hosted on Supabase."), "A"),
    (pair_text("Which database does Lumen use?",
               "Which database does Kestrel use?", "Postgres hosted on Supabase."), "B"),
    (pair_text("Where should I host the app?",
               "Which cloud provider should be used for hosting?",
               "Fly.io for applications and Cloudflare R2 for object storage."), "A"),
    (pair_text("Which cloud provider does the owner use for email?",
               "Which cloud provider should be used for hosting?",
               "Fly.io for applications and Cloudflare R2 for object storage."), "B"),
    (pair_text("Can I schedule a meeting with the owner at 9am?",
               "Does the owner prefer morning meetings?",
               "No. No meetings before 11:00; afternoons are preferred."), "A"),
    (pair_text("Should notifications be sent at night?",
               "Should notifications be sent on weekends?",
               "No weekend notifications, except outage alerts."), "B"),
    (pair_text("Should I pull in a library or use the standard library?",
               "Is it okay to add new third-party dependencies?",
               "Only with a clear reason. Prefer the standard library and minimal "
               "dependencies."), "A"),
    (pair_text("Which package manager should JavaScript projects use?",
               "Which package manager should Python projects use?",
               "uv, with a checked-in lockfile."), "B"),
)

VERIFY_QUESTION = Question(
    name="paraphrase", instruction=VERIFY_PROMPT, few_shot=VERIFY_FEW_SHOT,
    input_label="Pair",
)

TOP_K = 3
# P(answers) the model must reach; set on development data (v1 to v3) only.
THRESHOLD = 0.9


def consistent_verified_answer(keys: list[str], answers: dict[str, str]) -> str | None:
    """Keep the full verified answer when copies only add an explanation.

    No approximate value merging: shorter answers must be literal prefixes at
    an explanation boundary. Conditions, permissions and negative assertions
    keep the records ambiguous. All keys must already pass pair verification.
    """
    if not keys:
        return None
    values = {key: " ".join(answers[key].split()) for key in keys}
    if len(set(values.values())) == 1:
        return keys[0]
    longest = max(keys, key=lambda key: len(values[key]))
    full = values[longest]
    if re.search(
        r"\b(?:if|unless|only|except|provided|when|until|before|after|without|"
        r"no|not|never|must|cannot|approve|permission|authorize|commit|push|"
        r"publish\w*|upload\w*|spend|pay|delete)\b", full, re.I,
    ):
        return None
    for value in values.values():
        if value == full:
            continue
        remainder = full[len(value):] if full.startswith(value) else ""
        if not remainder.lstrip().startswith(("(", ":")):
            return None
    return longest


class ParaphraseVerifier:
    """Pick the one stored decision that answers ``query``, or None."""

    def __init__(
        self,
        model: LocalModel,
        embed: Callable[[list[str]], list[list[float]]],
        *,
        top_k: int = TOP_K,
        threshold: float = THRESHOLD,
    ) -> None:
        self.model = model
        self.embed = embed
        self.top_k = top_k
        self.threshold = threshold
        self._vectors: dict[str, list[float]] = {}
        self.skipped: list[str] = []
        self.last: list[tuple[str, float]] = []  # (candidate key, P(answers)) of the last call

    def rank(
        self, query: str, candidates: Sequence[tuple[str, str, str]]
    ) -> list[tuple[str, str, str]]:
        """Candidates (key, question, answer), nearest to the query first.

        A decision's vector is computed once per wording and kept for the process.
        """
        if len(self._vectors) > 5000:
            self._vectors.clear()
        keyed = [("\x1f".join(candidate), candidate) for candidate in candidates]
        missing = [(cache_key, c) for cache_key, c in keyed if cache_key not in self._vectors]
        if missing:
            vectors = self.embed([f"{c[1]} {c[2]}" for _, c in missing])
            for (cache_key, _), vector in zip(missing, vectors, strict=True):
                self._vectors[cache_key] = unit(vector)
        target = unit(self.embed([query])[0])
        keyed.sort(key=lambda item: dot(target, self._vectors[item[0]]), reverse=True)
        return [candidate for _, candidate in keyed]

    def pick(self, query: str, candidates: Sequence[tuple[str, str, str]]) -> str | None:
        """The key of the single candidate the model says answers ``query``."""
        self.last = []
        if not candidates:
            return None
        try:
            nearest = self.rank(query, candidates)[: self.top_k]
            for key, question, answer in nearest:
                p = self.model.p_a(VERIFY_QUESTION, pair_text(query, question, answer))
                self.last.append((key, p))
        except ClassifierUnavailable as exc:
            self.skipped.append(str(exc))
            return None
        yes = [key for key, p in self.last if p >= self.threshold]
        answers = {key: " ".join(answer.split()) for key, _, answer in candidates}
        # Imported copies of one confirmed decision can have distinct record IDs.
        # Every accepted pair must still clear the verifier threshold; never merge
        # similar answers, omit conditions or resolve genuinely different answers.
        return consistent_verified_answer(yes, answers)


def paraphrase_verifier_from_config(cfg: Any) -> ParaphraseVerifier | None:
    """`[workflows] paraphrase_verifier`: the verifier, or None for the strict matcher only."""
    if cfg.workflows.paraphrase_verifier != "model":
        return None
    from varyz.resolution.local_classifier import embedder_from_config, local_model_from_config

    return ParaphraseVerifier(local_model_from_config(cfg), embedder_from_config(cfg))


__all__ = [
    "THRESHOLD",
    "TOP_K",
    "VERIFY_QUESTION",
    "ParaphraseVerifier",
    "pair_text",
    "paraphrase_verifier_from_config",
]
