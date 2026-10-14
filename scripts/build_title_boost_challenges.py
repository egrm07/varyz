"""Freeze an adversarial follow-up audit before its first score; no retrieval calls."""

from __future__ import annotations

import hashlib

from evaluate_title_boost import ROOT, canonical, document

SPECS = [
    (
        "stale",
        "Current Falcon storage quota",
        "Falcon quota amendment",
        "Current Falcon storage quota is 40 GB. This current Falcon storage quota "
        "is the active signed amendment; the former 10 GB quota is withdrawn.",
        "Historical page. Superseded 10 GB allowance. Withdrawn in September.",
    ),
    (
        "stale",
        "Latest Heron certificate policy",
        "Heron security revision",
        "Latest Heron certificate policy requires rotation every week. "
        "The latest Heron certificate policy supersedes the obsolete annual rotation rule.",
        "Old annual rotation rule. This document is obsolete; consult the revision.",
    ),
    (
        "draft",
        "Approved Juniper operating budget",
        "Juniper finance sign-off",
        "Approved Juniper operating budget is 800 credits. The approved Juniper operating "
        "budget is final and signed. The 900-credit draft was rejected.",
        "UNAPPROVED. Draft proposal: 900 credits. No signature; this is not a final decision.",
    ),
    (
        "draft",
        "Final Hazel deployment plan",
        "Hazel final approval record",
        "Final Hazel deployment plan uses staging first. The final Hazel deployment plan "
        "was approved on September 29; the earlier production-first draft was rejected.",
        "DRAFT ONLY. Proposed direct production deployment. The owner rejected this draft.",
    ),
    (
        "related_not_answer",
        "Can Kestrel backups run without encryption?",
        "Security answer",
        "Can Kestrel backups run without encryption? Answer: No. Kestrel backups cannot run "
        "without encryption; the approved security policy requires encryption.",
        "Question only. This page contains no answer or decision.",
    ),
    (
        "related_not_answer",
        "Why did Sparrow imports fail?",
        "Sparrow incident conclusion",
        "Why did Sparrow imports fail? A missing permission caused the failure. "
        "The investigation establishes why Sparrow imports fail and records the fix.",
        "Incident question. The cause has not been investigated in this note.",
    ),
    (
        "related_not_answer",
        "Confirmed Bramble deployment decision",
        "Owner approval",
        "Owner confirmed Bramble deployment decision: staging must pass first. "
        "The confirmed Bramble deployment decision rejects an immediate production release.",
        "Third-party speculation. Owner approval is absent; the author guesses immediate release.",
    ),
    (
        "related_not_answer",
        "Confirmed Maple retention period",
        "Maple signed policy",
        "Confirmed Maple retention period is fourteen days. The confirmed Maple retention "
        "period was signed by the owner and replaces the vendor's ninety-day suggestion.",
        "External vendor suggestion: ninety days. The owner did not confirm this suggestion.",
    ),
    (
        "conflict",
        "Compare Rowan import decisions",
        "Rowan decision January",
        "Compare Rowan import decisions: the January version accepted CSV only. "
        "This source records the first Rowan import decision, before the later change.",
        "Navigation page only. Links to both versions; no decision text is included.",
    ),
    (
        "conflict",
        "Compare Birch backup policies",
        "Birch policy January",
        "Compare Birch backup policies: the January policy required daily snapshots. "
        "This source records the original Birch backup policy.",
        "Index only. The actual policy statements are in the linked source documents.",
    ),
    (
        "exact_lookup",
        "Merlin Relay Handbook",
        "Merlin Relay Handbook",
        "Merlin Relay Handbook defines the canonical operating procedure. "
        "The Merlin Relay Handbook owner is Mara.",
        "Meeting index. The handbook was mentioned, but this is not its text.",
    ),
    (
        "phrase_lookup",
        "Finch Maintenance Guide",
        "Detailed Finch Maintenance Guide",
        "Finch Maintenance Guide: Mara checks the pump on Tuesday. "
        "This is the Finch Maintenance Guide's canonical reference text.",
        "Meeting index. The guide was mentioned; this page does not contain the guide.",
    ),
]


def main() -> None:
    fixture = ROOT / "evals" / "title_boost_challenges_v1.json"
    manifest = ROOT / "evals" / "title_boost_challenges_v1_manifest.json"
    if fixture.exists() or manifest.exists():
        raise ValueError("Challenge audit is frozen; do not overwrite it after scoring.")
    cases = []
    for index, (category, query, title, body, wrong_body) in enumerate(SPECS):
        expected = ["answer"]
        docs = [
            {"id": "answer", **document(title, body, date="2026-09-29T12:00:00Z")},
            {
                "id": "wrong",
                **document(
                    query if category not in {"exact_lookup", "phrase_lookup"} else "Meeting index",
                    wrong_body,
                    date="2025-01-01T12:00:00Z",
                ),
            },
            {"id": "noise", **document("Office notice", "The office meeting starts at noon.")},
        ]
        if category == "conflict":
            expected.append("second")
            docs.append(
                {
                    "id": "second",
                    **document(
                        "September revision",
                        f"{query}: the September version explicitly changes the original decision.",
                    ),
                }
            )
        cases.append(
            {
                "id": f"challenge-{index}",
                "split": "challenge",
                "category": category,
                "variant": index,
                "query": query,
                "goal": "Return the source establishing the requested fact, not an obsolete, "
                "unapproved, unanswered or merely navigational title match.",
                "expected": expected,
                "excluded_sources": [],
                "documents": docs,
            }
        )
    payload = {"version": "title-boost-challenges-v1", "cases": cases}
    fixture.write_bytes(canonical(payload))
    manifest.write_bytes(
        canonical(
            {
                "fixture_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
                "n": len(cases),
                "frozen_before_scoring": True,
                "policy": "Designed after v2 results as a targeted counterexample "
                "audit. Not a representative or held-out rate estimate.",
            }
        )
    )
    print(manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
