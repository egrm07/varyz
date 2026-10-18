"""Source-native conversation metadata survives normalization and storage.

These tests intentionally pair positive fields with absent ones.  The boundary
must preserve structure the source provides without deriving speakers, replies,
quotes, or edit history from prose.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from varyz.connectors.ai_conversations import (
    ADAPTERS_BY_ID,
    AIConversationsConnector,
    parse_turn,
)
from varyz.connectors.ai_conversations import (
    _item as ai_item,
)
from varyz.connectors.documents import DocumentsConnector
from varyz.db import dao
from varyz.db.connection import _iter_migrations, connect, migrate
from varyz.domain import (
    ConversationEditState,
    ConversationMetadata,
    Kind,
    NormalizedEntry,
    RawItem,
)


def _raw(record: dict, external_id: str = "message-1") -> RawItem:
    return RawItem(
        external_id=external_id,
        payload=json.dumps(record),
        payload_ext=".json",
        metadata=record,
    )


def test_ai_conversation_preserves_native_parent_and_source_order(tmp_path):
    path = tmp_path / "session.jsonl"
    record = {
        "type": "user",
        "sessionId": "session-1",
        "uuid": "turn-2",
        "parentUuid": "turn-1",
        "timestamp": "2026-08-11T09:30:00Z",
        "message": {"role": "user", "content": "Yes, use the second option."},
    }
    turn = parse_turn("claude_code", record, 7, path)
    assert turn is not None

    raw = ai_item(ADAPTERS_BY_ID["claude_code"], turn, path, 7, 12_000)
    norm = AIConversationsConnector().normalize(raw)

    assert norm.thread_id == "ai:claude_code:session-1"
    assert norm.conversation.conversation_id == "session-1"
    assert norm.conversation.sender_id == "user"
    assert norm.conversation.addressee_ids == ("assistant",)
    assert norm.conversation.reply_to_id == "claude_code:session-1:turn-1"
    assert norm.conversation.quote_target_id is None
    assert norm.conversation.source_order == 7


def test_ai_text_that_names_a_turn_does_not_fabricate_a_reply(tmp_path):
    record = {
        "session_id": "openclaw-1",
        "timestamp": "2026-08-11T09:30:00Z",
        "message": {
            "role": "user",
            "content": "This is replying to turn-1, but the export has no reply field.",
        },
    }
    turn = parse_turn("openclaw", record, 2, tmp_path / "session.jsonl")
    assert turn is not None and turn.reply_to_id is None


def test_conversation_metadata_rejects_naive_structured_edit_time():
    with pytest.raises(ValueError, match="edited_at must include a timezone"):
        ConversationMetadata(edited_at=datetime(2026, 6, 30, 21, 20))


def test_documents_leave_conversation_structure_unknown(tmp_path):
    path = tmp_path / "notes.txt"
    raw = RawItem(
        external_id=str(path),
        payload=b"Anna likes peonies.",
        payload_ext=".txt",
        metadata={"path": str(path), "mtime": 1786440600},
    )
    norm = DocumentsConnector().normalize(raw)

    assert norm.conversation == ConversationMetadata()
    assert norm.conversation.reply_to_id is None
    assert norm.conversation.participants_complete is None
    assert norm.conversation.edit_state is ConversationEditState.UNKNOWN


def test_storage_round_trip_and_unchanged_resync_backfill(conn):
    original = NormalizedEntry(
        kind=Kind.MESSAGE,
        external_id="native-1",
        body="same bytes",
    )
    entry_id, status = dao.upsert_entry(conn, original, source="fixture")
    assert status == "new"
    assert dao.get_entry(conn, entry_id).conversation == ConversationMetadata()  # type: ignore[union-attr]

    backfilled = original.model_copy(
        update={
            "conversation": ConversationMetadata(
                conversation_id="thread-1",
                sender_id="person-1",
                source_order=3,
            )
        }
    )
    same_id, status = dao.upsert_entry(conn, backfilled, source="fixture")
    stored = dao.get_entry(conn, entry_id)

    assert same_id == entry_id and status == "unchanged"
    assert stored is not None
    assert stored.content_hash == dao.content_hash(None, "same bytes")
    assert stored.conversation.conversation_id == "thread-1"
    assert stored.conversation.sender_id == "person-1"


def test_pre_metadata_database_migrates_to_an_empty_unknown_envelope(tmp_path):
    path = tmp_path / "pre-conversation.db"
    conn = connect(path)
    conn.execute(
        """CREATE TABLE schema_migrations
           (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
    )
    for version, _name, sql in _iter_migrations():
        if version >= 19:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, "2026-08-10T00:00:00+00:00"),
        )
    conn.execute(
        """INSERT INTO entries
           (id, kind, source, external_id, content_hash, body, ingested_at)
           VALUES ('old-entry', 'message', 'fixture', 'old-1', 'hash', 'hello',
                   '2026-08-10T00:00:00+00:00')"""
    )
    conn.commit()

    applied = migrate(conn)
    assert applied[:2] == [
        "0019_conversation_metadata.sql",
        "0020_personal_model.sql",
    ]
    assert applied == sorted(applied)
    row = conn.execute("SELECT * FROM entries WHERE id='old-entry'").fetchone()
    entry = dao.row_to_entry(row)

    assert row["conversation_metadata_json"] == "{}"
    assert entry.conversation == ConversationMetadata()
    conn.close()
