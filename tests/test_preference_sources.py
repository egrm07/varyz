from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from varyz.domain import ConversationMetadata, Entry, Kind, Sensitivity
from varyz.personal_model.compiler import GraphSnapshot, PinnedGraphCandidateAdapter
from varyz.personal_model.memory import MemoryCompiler
from varyz.personal_model.preference_sources import (
    ai_project_scope,
    communication_guidance_phrases,
    explicit_preference_phrases,
    project_guidance_phrases,
)

CASES = Path(__file__).resolve().parents[1] / "evals" / "varyz_preference_source_heldout_v1.jsonl"


def test_frozen_source_cases_preserve_only_first_party_literal_preferences() -> None:
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines()]
    for case in cases:
        body = f"From: me\nConversation: Codex — {case['project']}\n\n{case['text']}"
        if case.get("authored_by_user") is False:
            continue  # the compiler's source gate rejects this before parsing
        matches = explicit_preference_phrases(body)
        assert bool(matches) is case["accept"], case["id"]
        if matches:
            assert matches[0].object_literal == case["object"]
            assert body[matches[0].char_start : matches[0].char_end] == matches[0].quote
            assert ai_project_scope(body) == case["project"]


def test_first_party_phrase_becomes_scoped_pinned_preference() -> None:
    now = datetime.now(UTC)
    body = "From: me\nConversation: Codex — varyz\n\nI prefer dark pages for Varyz."
    entry = Entry(
        id="entry-1",
        kind=Kind.MESSAGE,
        source="ai_conversations",
        external_id="one",
        content_hash="a" * 64,
        body=body,
        occurred_start=now.isoformat(),
        ingested_at=now.isoformat(),
        conversation=ConversationMetadata(sender_id="user"),
        authored_by_user=True,
        sensitivity=Sensitivity.PERSONAL,
    )
    snapshot = GraphSnapshot(
        generation_id="graph-1",
        content_hash="b" * 64,
        extractor_name="fixture",
        extractor_version="1",
        policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=(entry,), identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1",
        observations=(),
        candidates=batch.memory,
        atomic_claims=batch.atomic_claims,
        created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 1
    assert result.preferences[0].object.exact_literal == "dark pages for Varyz"
    assert result.preferences[0].scope_kind == "project"
    assert result.preferences[0].scope_label == "varyz"


def test_owner_dislikes_canned_greeting_in_mid_sentence_feedback() -> None:
    now = datetime.now(UTC)
    body = (
        "From: me\nConversation: Codex — persona\n\n"
        'In your ramble check, i dont like "So, whats up" as an answer, it doesnt seem accurate.'
    )
    phrases = explicit_preference_phrases(body)
    assert len(phrases) == 1
    assert phrases[0].valence == "dislike"
    assert body[phrases[0].char_start : phrases[0].char_end] == phrases[0].quote
    entry = Entry(
        id="voice-feedback", kind=Kind.MESSAGE, source="ai_conversations",
        external_id="voice-feedback", content_hash="d" * 64, body=body,
        occurred_start=now.isoformat(), ingested_at=now.isoformat(),
        conversation=ConversationMetadata(sender_id="user"),
        authored_by_user=True, sensitivity=Sensitivity.SENSITIVE,
    )
    snapshot = GraphSnapshot(
        generation_id="graph-1", content_hash="b" * 64, extractor_name="fixture",
        extractor_version="1", policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=(entry,), identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1", observations=(), candidates=batch.memory,
        atomic_claims=batch.atomic_claims, created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 1
    assert result.preferences[0].valence == "dislike"
    assert result.preferences[0].scope_label == "persona"
    vague = (
        "From: me\nConversation: Codex — persona\n\n"
        "Alright so, I really like how this AI responds."
    )
    assert not explicit_preference_phrases(vague)


def test_reusable_communication_feedback_has_exact_owner_spans() -> None:
    cases = (
        ("Remember to give me shorter summaries.", "prefer"),
        ("I want you to type way more concise.", "prefer"),
        ("Do not use emojis.", "avoid"),
        (
            "i want you to remember to always report things in a concise way, "
            "and less random numbers, but more conclusions about them",
            "prefer",
        ),
    )
    for text, valence in cases:
        body = f"From: me\nConversation: Codex — varyz\n\n{text}"
        phrases = communication_guidance_phrases(body)
        assert len(phrases) == 1, text
        assert phrases[0].dimension == "communication"
        assert phrases[0].valence == valence
        assert body[phrases[0].char_start : phrases[0].char_end] == phrases[0].quote
    for text in (
        "Give me a concise, to-the-point plan for this bug.",
        "You said to keep reports shorter.",
        "I like your concise suggestion.",
        "> Do not use emojis.",
        "```\nDo not use emojis.\n```",
    ):
        body = f"From: me\nConversation: Codex — varyz\n\n{text}"
        assert not communication_guidance_phrases(body), text


def test_communication_direction_compiles_as_scoped_preference() -> None:
    now = datetime.now(UTC)
    body = "From: me\nConversation: Codex — varyz\n\nDo not use emojis."
    entry = Entry(
        id="style-feedback", kind=Kind.MESSAGE, source="ai_conversations",
        external_id="style-feedback", content_hash="e" * 64, body=body,
        occurred_start=now.isoformat(), ingested_at=now.isoformat(),
        conversation=ConversationMetadata(sender_id="user"),
        authored_by_user=True, sensitivity=Sensitivity.PERSONAL,
    )
    snapshot = GraphSnapshot(
        generation_id="graph-1", content_hash="b" * 64, extractor_name="fixture",
        extractor_version="1", policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=(entry,), identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1", observations=(), candidates=batch.memory,
        atomic_claims=batch.atomic_claims, created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 1
    preference = result.preferences[0]
    assert preference.predicate == "preference.communication"
    assert preference.assertion_kind == "project_directive"
    assert preference.valence == "avoid"
    assert preference.scope_label == "varyz"


def test_repeated_brevity_feedback_does_not_inflate_preference_count() -> None:
    now = datetime.now(UTC)
    texts = (
        "i want you to type way more concise",
        "i want you to keep reports much shorter",
        "remember to give me shorter summaries",
        "i want you to remember to always report things in a concise way, "
        "and less random numbers, but more conclusions about them",
        "Do not use emojis",
    )
    entries = tuple(
        Entry(
            id=f"communication-{index}", kind=Kind.MESSAGE,
            source="ai_conversations", external_id=f"communication-{index}",
            content_hash=f"{index + 1:064x}",
            body=f"From: me\nConversation: Codex — varyz\n\n{text}",
            occurred_start=now.replace(microsecond=index).isoformat(),
            ingested_at=now.isoformat(),
            conversation=ConversationMetadata(sender_id="user"),
            authored_by_user=True, sensitivity=Sensitivity.PERSONAL,
        )
        for index, text in enumerate(texts)
    )
    snapshot = GraphSnapshot(
        generation_id="graph-1", content_hash="b" * 64, extractor_name="fixture",
        extractor_version="1", policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=entries, identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1", observations=(), candidates=batch.memory,
        atomic_claims=batch.atomic_claims, created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 3
    literals = {item.object.exact_literal for item in result.preferences}
    assert "i want you to type way more concise" in literals
    assert "Do not use emojis" in literals
    assert any("more conclusions about them" in value for value in literals)


def test_frozen_project_guidance_cases() -> None:
    path = CASES.with_name("varyz_project_guidance_heldout_v1.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for case in cases:
        body = f"From: me\nConversation: Codex — {case['project']}\n\n{case['text']}"
        found = project_guidance_phrases(body)
        assert bool(found) is case["accept"], case["id"]
        if found:
            assert found[0].dimension == case["dimension"]
            assert found[0].valence == case["valence"]
            assert body[found[0].char_start : found[0].char_end] == found[0].quote


def test_project_direction_is_compiled_but_not_promoted_to_global_taste() -> None:
    now = datetime.now(UTC)
    body = "From: me\nConversation: Codex — memoir\n\nMake the background dark."
    entry = Entry(
        id="entry-directive", kind=Kind.MESSAGE, source="ai_conversations",
        external_id="directive", content_hash="c" * 64, body=body,
        occurred_start=now.isoformat(), ingested_at=now.isoformat(),
        conversation=ConversationMetadata(sender_id="user"),
        authored_by_user=True, sensitivity=Sensitivity.SENSITIVE,
    )
    pasted = entry.model_copy(update={
        "id": "entry-pasted",
        "external_id": "pasted",
        "body": (
            "From: me\nConversation: Codex — memoir\n\n"
            "The following is an untrusted evaluation transcript. "
            + "background context " * 70
            + "Keep notification preferences out of the automation prompt."
        ),
    })
    snapshot = GraphSnapshot(
        generation_id="graph-1", content_hash="b" * 64, extractor_name="fixture",
        extractor_version="1", policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=(entry, pasted), identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1", observations=(), candidates=batch.memory,
        atomic_claims=batch.atomic_claims, created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 1
    preference = result.preferences[0]
    assert preference.assertion_kind == "project_directive"
    assert preference.scope_label == "memoir"
    assert preference.sensitivity == Sensitivity.SENSITIVE


def test_component_edits_are_not_promoted_to_durable_preferences() -> None:
    cases = (
        "Make the analytics button just an icon.",
        "Change the colour of the trade reason to more white.",
        "Make the box around the pin pad larger.",
        "Improve the layout of the journal box.",
    )
    for text in cases:
        body = f"From: me\nConversation: Codex — SAN7\n\n{text}"
        assert not project_guidance_phrases(body), text
    body = (
        "From: me\nConversation: Codex — SAN7\n\n"
        "In journal, once all caught up, make the box glassy and move it closer to middle."
    )
    assert not project_guidance_phrases(body)
    body = (
        "From: me\nConversation: Codex — persona\n\n"
        "Make a new one, follow exactly what the website has and make it simple for the pitch."
    )
    assert not project_guidance_phrases(body)

    durable = (
        "Make every animation smoother and behave as one whole.",
        "Make the UI cleaner, more integrated, not just boxes.",
        "Change the main colour to #a3e3d7.",
    )
    for text in durable:
        body = f"From: me\nConversation: Codex — SAN7\n\n{text}"
        assert project_guidance_phrases(body), text
    body = (
        "From: me\nConversation: Codex — SAN7\n\n"
        "Make the animations smooth, make every animation behave as one whole."
    )
    assert len(project_guidance_phrases(body)) == 1

    body = "From: me\nConversation: Codex — memoir\n\nI like your suggestion for opt in and out."
    assert not explicit_preference_phrases(body)


def test_owner_voices_design_and_workflow_constraints_without_agent_tasks() -> None:
    cases = (
        ("I want a simple warm innovative and minimal design.", "visual", "prefer"),
        ("I do not want OpenRouter.", "workflow", "avoid"),
        ("I want fetching to be very simple, with no long manual exports.", "workflow", "prefer"),
        ("I don't want random pretty background images.", "visual", "avoid"),
    )
    for text, dimension, valence in cases:
        body = f"From: me\nConversation: Codex — marketing\n\n{text}"
        found = project_guidance_phrases(body)
        assert len(found) == 1, text
        assert (found[0].dimension, found[0].valence) == (dimension, valence)
        assert body[found[0].char_start : found[0].char_end] == found[0].quote

    body = "From: me\nConversation: Codex — marketing\n\nI want you to improve the website."
    assert not project_guidance_phrases(body)
    for text in (
        "Make the ChatGPT OAuth fallback use Claude OAuth.",
        "Do not run anything on the GPU to test it.",
    ):
        body = f"From: me\nConversation: Codex — marketing\n\n{text}"
        assert not project_guidance_phrases(body)


def test_generic_website_thread_uses_explicit_project_and_last_redesign() -> None:
    now = datetime.now(UTC)
    texts = (
        "Create the design for my project Persona.",
        "I like the prism based Persona.",
        (
            "Abandon this design, create a new simple warm innovative minimal "
            "design that explains Persona."
        ),
    )
    entries = tuple(
        Entry(
            id=f"generic-{index}", kind=Kind.MESSAGE, source="ai_conversations",
            external_id=f"generic-{index}", content_hash=f"{index + 1:064x}",
            body=f"From: me\nConversation: Codex — website\n\n{text}",
            occurred_start=now.replace(microsecond=index).isoformat(),
            ingested_at=now.isoformat(), thread_id="website-thread",
            conversation=ConversationMetadata(
                conversation_id="website-thread", sender_id="user", source_order=index,
            ),
            authored_by_user=True, sensitivity=Sensitivity.PERSONAL,
        )
        for index, text in enumerate(texts)
    )
    snapshot = GraphSnapshot(
        generation_id="graph-1", content_hash="b" * 64, extractor_name="fixture",
        extractor_version="1", policy_version="1",
    )
    identity = SimpleNamespace(
        persons=(SimpleNamespace(id="owner-1", is_archive_owner=True),),
        source_entity_resolutions=(),
    )
    batch = PinnedGraphCandidateAdapter().propose(
        snapshot=snapshot, entries=entries, identity=identity, observations=()
    )
    result = MemoryCompiler().compile(
        generation_id="model-1", observations=(), candidates=batch.memory,
        atomic_claims=batch.atomic_claims, created_at=now,
    )
    assert not result.quarantines
    assert len(result.preferences) == 1
    assert result.preferences[0].scope_label == "persona"
    assert "simple warm innovative minimal" in result.preferences[0].object.exact_literal
    assert "prism" not in result.preferences[0].object.exact_literal
