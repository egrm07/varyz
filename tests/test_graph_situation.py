"""The room a sentence was said in, recovered from what the archive stored.

Everything here is synthetic. The shapes are the ones the connectors write —
`From:`/`Conversation:` for an AI session, `From:`/`Chat:` for a message — and
the point of the module under test is that those are enough: nothing reads the
message text to decide where it was said.
"""

from __future__ import annotations

from varyz.domain import Entry, Kind
from varyz.graph.situation import (
    Addressee,
    Setting,
    classify,
    header_block,
    situation_of,
)


def _entry(
    body: str,
    *,
    source: str,
    kind: Kind = Kind.MESSAGE,
    thread_id: str | None = None,
    authored: bool = True,
) -> Entry:
    return Entry(
        id="e1",
        kind=kind,
        source=source,
        external_id="x1",
        content_hash="h1",
        body=body,
        ingested_at="2026-01-01T00:00:00+00:00",
        thread_id=thread_id,
        authored_by_user=authored,
    )


def test_a_users_turn_in_a_coding_session_is_an_instruction_to_an_assistant():
    entry = _entry(
        "From: me\nConversation: Claude Code — notes-app\n\n"
        "I want the export screen to only show the last thirty days.",
        source="ai_conversations",
        thread_id="ai:claude_code:0d1f",
    )

    situation = situation_of(entry)

    assert situation.setting is Setting.WORK_SESSION
    assert situation.addressee is Addressee.ASSISTANT
    assert situation.surface == "Claude Code"
    assert situation.project == "notes-app"
    assert situation.session == "0d1f"
    assert situation.is_work_session


def test_the_assistants_own_turn_is_the_same_room_addressed_the_other_way():
    entry = _entry(
        "From: Claude Code assistant\nConversation: Claude Code — notes-app\n\nDone.",
        source="ai_conversations",
        thread_id="ai:claude_code:0d1f",
        authored=False,
    )

    situation = situation_of(entry)

    assert situation.setting is Setting.WORK_SESSION
    assert situation.addressee is Addressee.PERSON


def test_a_message_to_a_person_is_a_conversation():
    entry = _entry(
        "From: me\nChat: Nadia\n\nI want a garden one day.",
        source="whatsapp",
        thread_id="wa:5550100",
    )

    situation = situation_of(entry)

    assert situation.setting is Setting.CONVERSATION
    assert situation.addressee is Addressee.PERSON
    assert not situation.is_work_session
    assert situation.project == ""


def test_a_document_is_a_record_addressed_to_nobody():
    entry = _entry(
        "Chapter one. He believed the river would rise.",
        source="documents",
        kind=Kind.DOCUMENT,
    )

    situation = situation_of(entry)

    assert situation.setting is Setting.RECORD
    assert situation.addressee is Addressee.NONE


def test_a_project_name_containing_a_hyphen_survives_the_split():
    """The separator the connector writes is an em dash, so it has to be."""
    situation = classify(
        source="ai_conversations",
        kind=str(Kind.MESSAGE),
        thread_id="ai:codex:99",
        headers={"conversation": "Codex — cowork-recents"},
        authored_by_user=True,
    )

    assert situation.surface == "Codex"
    assert situation.project == "cowork-recents"


def test_an_ai_thread_still_classifies_when_the_header_is_missing():
    """A connector that stops writing headers must not silently become a chat."""
    situation = classify(
        source="something_new",
        kind=str(Kind.MESSAGE),
        thread_id="ai:codex:99",
        headers={},
        authored_by_user=True,
    )

    assert situation.setting is Setting.WORK_SESSION
    assert situation.surface == "codex"
    assert situation.project == ""


def test_the_description_is_plain_text_a_model_can_read():
    work = classify(
        source="ai_conversations",
        kind=str(Kind.MESSAGE),
        thread_id="ai:codex:99",
        headers={"conversation": "Codex — notes-app"},
        authored_by_user=True,
    ).describe()

    assert "working session" in work
    assert "notes-app" in work
    assert work.isascii()


def test_header_parsing_ignores_a_body_with_no_header_block():
    assert header_block("just a sentence with no headers") == {}
    assert header_block("From: me\nChat: Nadia\n\nhello") == {
        "from": "me",
        "chat": "Nadia",
    }
