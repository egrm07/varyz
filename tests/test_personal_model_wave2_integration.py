"""Integration regressions across entry metadata, episodes, and model snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from varyz.db import dao
from varyz.db.connection import migrate
from varyz.domain import ConversationMetadata, Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.episodes import EpisodeReconstructor
from varyz.personal_model.repository import (
    PersonalModelRepository,
    required_record_dependencies,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def _add_active_graph(conn: sqlite3.Connection) -> None:
    stamp = NOW.isoformat()
    conn.execute(
        """INSERT INTO graph_runs
           (id, generation_key, extractor_name, extractor_version,
            predicate_version, identity_version, input_content_hash, state, started_at,
            completed_at, activated_at)
           VALUES ('graph-1','key-1','test','1','1','1','input','active',?,?,?)""",
        (stamp, stamp, stamp),
    )
    conn.commit()


def _generation(repo: PersonalModelRepository, generation_id: str) -> None:
    repo.create_generation(
        "graph-1",
        compiler_version="compiler-1",
        policy_version="policy-1",
        input_content_hash=f"input-{generation_id}",
        generation_id=generation_id,
    )


def _normalized(
    external_id: str,
    body: str,
    *,
    order: int,
    sender_id: str,
    reply_to_id: str | None = None,
) -> NormalizedEntry:
    return NormalizedEntry(
        kind=Kind.MESSAGE,
        external_id=external_id,
        body=body,
        occurred_start=NOW + timedelta(minutes=order),
        thread_id="thread-1",
        conversation=ConversationMetadata(
            conversation_id="conversation-1",
            participant_ids=("owner", "anna"),
            participants_complete=True,
            sender_id=sender_id,
            reply_to_id=reply_to_id,
            source_order=order,
        ),
        authored_by_user=sender_id == "owner",
    )


def _build(conn: sqlite3.Connection, generation_id: str):
    entries = [
        dao.get_entry(conn, row["id"])
        for row in conn.execute("SELECT id FROM entries ORDER BY external_id")
    ]
    assert all(entry is not None for entry in entries)
    return EpisodeReconstructor().build(
        entries,
        generation_id=generation_id,
        created_at=NOW,
    )


def test_canonical_multi_target_episode_stages_and_entry_projection_blocks_stale_reuse(
    conn: sqlite3.Connection,
) -> None:
    _add_active_graph(conn)
    first_id, _ = dao.upsert_entry(
        conn,
        _normalized("turn-1", "What flowers do you like?", order=1, sender_id="owner"),
        source="fixture-chat",
        source_account="owner-account",
        sensitivity=Sensitivity.PERSONAL,
    )
    second_id, _ = dao.upsert_entry(
        conn,
        _normalized(
            "turn-2",
            "Peonies",
            order=2,
            sender_id="anna",
            reply_to_id="turn-1",
        ),
        source="fixture-chat",
        source_account="owner-account",
        sensitivity=Sensitivity.PERSONAL,
    )
    conn.commit()

    repo = PersonalModelRepository(conn)
    _generation(repo, "model-1")
    result = _build(conn, "model-1")
    assert len(result.episodes) == 1
    assert {coverage.target_entry_id for coverage in result.target_coverage} == {
        first_id,
        second_id,
    }
    assert all(
        coverage.episode_ids == (result.episodes[0].id,)
        for coverage in result.target_coverage
    )
    episode = result.episodes[0]
    dependency_hash = repo.stage_record(
        episode,
        dependencies=required_record_dependencies(conn, "graph-1", episode),
    )
    repo.activate_generation("model-1")

    # Semantically identical metadata with a different JSON key order must not
    # defeat reuse: dependency hashing consumes the validated envelope.
    row = conn.execute(
        "SELECT conversation_metadata_json FROM entries WHERE id=?", (second_id,)
    ).fetchone()
    envelope = json.loads(row["conversation_metadata_json"])
    reordered = json.dumps(dict(reversed(tuple(envelope.items()))), separators=(",", ":"))
    conn.execute(
        "UPDATE entries SET conversation_metadata_json=? WHERE id=?",
        (reordered, second_id),
    )
    conn.commit()
    _generation(repo, "model-2")
    assert repo.clone_record_if_unchanged(
        source_generation_id="model-1",
        target_generation_id="model-2",
        record_id=episode.id,
        expected_dependency_hash=dependency_hash,
    )
    repo.activate_generation("model-2")

    # Same id/body with changed speaker, reply, or order is a different episode
    # input even though DAO's intentional title/body content_hash is unchanged.
    changed = ConversationMetadata(
        conversation_id="conversation-1",
        participant_ids=("owner", "anna"),
        participants_complete=True,
        sender_id="owner",
        reply_to_id=None,
        source_order=9,
    ).model_dump_json(exclude_none=True, exclude_defaults=True)
    conn.execute(
        "UPDATE entries SET conversation_metadata_json=? WHERE id=?",
        (changed, second_id),
    )
    conn.commit()
    _generation(repo, "model-3")
    assert not repo.clone_record_if_unchanged(
        source_generation_id="model-2",
        target_generation_id="model-3",
        record_id=episode.id,
        expected_dependency_hash=dependency_hash,
    )

    # A staged episode is also rejected at activation if time/sensitivity changes
    # after derivation, instead of silently publishing stale context.
    _generation(repo, "model-stale")
    stale = _build(conn, "model-stale").episodes[0]
    repo.stage_record(
        stale,
        dependencies=required_record_dependencies(conn, "graph-1", stale),
    )
    conn.execute(
        """UPDATE entries SET occurred_start=?, sensitivity='sensitive'
           WHERE id=?""",
        ((NOW + timedelta(days=1)).isoformat(), second_id),
    )
    conn.commit()
    with pytest.raises(ValueError, match="stale"):
        repo.activate_generation("model-stale")


def test_migrations_are_forward_only_and_idempotent_after_wave2(
    conn: sqlite3.Connection,
) -> None:
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == sorted(versions)
    assert versions == list(range(1, max(versions) + 1))
    assert max(versions) >= 20
    assert migrate(conn) == []
