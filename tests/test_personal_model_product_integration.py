"""Cross-surface and forward-upgrade acceptance for the compiled life model."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from varyz.db.connection import _iter_migrations, connect, migrate
from varyz.domain import Sensitivity
from varyz.personal_model.models import (
    DerivationKind,
    Person,
    ProducerKind,
    ProducerMetadata,
    Readiness,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def test_migration_0021_to_0022_preserves_active_model_and_adds_immutable_state(
    tmp_path,
) -> None:
    database = connect(tmp_path / "pre-compiler.db")
    database.execute(
        """CREATE TABLE schema_migrations
           (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
    )
    for version, _name, sql in _iter_migrations():
        if version >= 22:
            continue
        database.executescript(sql)
        database.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)",
            (version, NOW.isoformat()),
        )
    database.execute(
        """INSERT INTO graph_runs
           (id,generation_key,extractor_name,extractor_version,predicate_version,
            identity_version,input_content_hash,state,started_at,completed_at,activated_at)
           VALUES ('graph-pre-compiler','key','test','1','1','1','hash','active',?,?,?)""",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    # Staged first: an activated generation is append-only from migration 0020 on, so
    # the pre-0022 archive is seeded the way the compiler writes one — records while
    # staged, then the single activation.
    database.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at)
           VALUES ('model-pre-compiler','graph-pre-compiler','test','test','model-hash',
                   'staged',?)""",
        (NOW.isoformat(),),
    )
    person = Person(
        id="person-owner-pre-compiler",
        generation_id="model-pre-compiler",
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.USER_CONFIRMED,
        producer=ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1"),
        readiness=Readiness.READY,
        created_at=NOW,
        canonical_key="person:owner",
        display_name="Archive owner",
        is_archive_owner=True,
    )
    encoded = json.dumps(
        person.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    record_hash = hashlib.sha256(encoded.encode()).hexdigest()
    database.execute(
        """INSERT INTO personal_model_records
           (generation_id,record_id,record_kind,record_json,canonical_json_hash,
            dependency_hash,readiness,sensitivity,created_at)
           VALUES ('model-pre-compiler','person-owner-pre-compiler','person',?,?,?,?,?,?)""",
        (encoded, record_hash, "owner-dependency", "ready", "personal", NOW.isoformat()),
    )
    database.execute(
        "UPDATE personal_model_generations SET state='current', activated_at=? WHERE id=?",
        (NOW.isoformat(), "model-pre-compiler"),
    )
    database.execute(
        """INSERT INTO personal_model_active_generation
           (singleton,generation_id,activated_at)
           VALUES (1,'model-pre-compiler',?)""",
        (NOW.isoformat(),),
    )
    database.commit()

    applied = migrate(database)
    # Derived rather than listed. What this test is about is that every
    # migration from 0022 on applies over a live pre-compiler model without
    # losing it; a literal list restates the directory and turns each new
    # migration into an unrelated red test.
    assert applied == [name for version, name, _sql in _iter_migrations() if version >= 22]
    assert applied[0] == "0022_personal_model_compiler.sql"
    assert database.execute(
        "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
    ).fetchone()[0] == "model-pre-compiler"
    preserved = database.execute(
        """SELECT canonical_json_hash,record_json FROM personal_model_records
           WHERE generation_id='model-pre-compiler'
             AND record_id='person-owner-pre-compiler'"""
    ).fetchone()
    assert preserved["canonical_json_hash"] == record_hash
    assert preserved["record_json"] == encoded

    generation_tables = {
        "personal_model_compiler_checkpoints",
        "personal_model_target_episode_coverage",
        "personal_model_action_handoffs",
        "personal_model_relationship_histories",
        "personal_model_relationship_history_dependencies",
        "personal_model_offered_questions",
        "personal_model_source_identity_memberships",
        "personal_model_project_candidate_memberships",
        "personal_model_goal_candidate_memberships",
        "personal_model_observation_progress",
    }
    actual_tables = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert generation_tables <= actual_tables
    for table in generation_tables:
        foreign_targets = {
            row["table"] for row in database.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert foreign_targets & {"personal_model_generations", "personal_model_records"}, table
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []

    immutable_inserts = {
        "personal_model_observation_progress": (
            """INSERT INTO personal_model_observation_progress
               (generation_id,state,episodes_annotated,episodes_total,episodes_remaining,
                entries_reached,annotation_calls,cache_hits,annotated_episode_ids_json,
                reached_entry_ids_json,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "model-pre-compiler",
                "complete",
                0,
                0,
                0,
                0,
                0,
                0,
                "[]",
                "[]",
                NOW.isoformat(),
            ),
        ),
        "personal_model_compiler_checkpoints": (
            """INSERT INTO personal_model_compiler_checkpoints
               (generation_id,stage,state,input_hash) VALUES (?,?,?,?)""",
            ("model-pre-compiler", "test", "pending", "hash"),
        ),
        "personal_model_target_episode_coverage": (
            """INSERT INTO personal_model_target_episode_coverage
               (generation_id,target_entry_id,ordinal,episode_id,context_complete)
               VALUES (?,?,?,?,?)""",
            ("model-pre-compiler", "entry", 0, "episode", 1),
        ),
        "personal_model_action_handoffs": (
            """INSERT INTO personal_model_action_handoffs
               (generation_id,candidate_id,title,node_kind,target_layer,
                observation_ids_json,claim_ids_json,source_entry_ids_json,
                project_ids_json,parent_goal_ids_json)
               VALUES (?,?,?,?,?,'[]','[]','[]','[]','[]')""",
            ("model-pre-compiler", "action", "Action", "action", "action"),
        ),
        "personal_model_relationship_histories": (
            """INSERT INTO personal_model_relationship_histories
               (generation_id,snapshot_id,archive_owner_person_id,other_person_id,
                period_start,period_end,fact_ids_json,preference_ids_json,
                shared_event_ids_json,commitment_observation_ids_json,
                relationship_role_observation_ids_json)
               VALUES (?,?,?,?,?,?,'[]','[]','[]','[]','[]')""",
            ("model-pre-compiler", "snapshot", "owner", "other", "2026", "2026"),
        ),
        "personal_model_relationship_history_dependencies": (
            """INSERT INTO personal_model_relationship_history_dependencies
               (generation_id,snapshot_id,ordinal,record_kind,record_id,dependency_hash)
               VALUES (?,?,?,?,?,?)""",
            ("model-pre-compiler", "snapshot", 0, "fact", "fact", "hash"),
        ),
        "personal_model_offered_questions": (
            """INSERT INTO personal_model_offered_questions
               (generation_id,question_id,interpretation_id,display_text,
                normalized_text,offered_at,state) VALUES (?,?,?,?,?,?,?)""",
            (
                "model-pre-compiler",
                "question",
                "interpretation",
                "Question?",
                "question",
                NOW.isoformat(),
                "active",
            ),
        ),
        "personal_model_source_identity_memberships": (
            """INSERT INTO personal_model_source_identity_memberships
               (generation_id,person_id,source_identity_key,source,source_account,
                source_key,membership_hash) VALUES (?,?,?,?,?,?,?)""",
            (
                "model-pre-compiler",
                "person-owner-pre-compiler",
                "source:key",
                "source",
                "account",
                "key",
                "hash",
            ),
        ),
        "personal_model_project_candidate_memberships": (
            """INSERT INTO personal_model_project_candidate_memberships
               (generation_id,project_id,candidate_id,membership_hash) VALUES (?,?,?,?)""",
            ("model-pre-compiler", "project", "candidate", "hash"),
        ),
        "personal_model_goal_candidate_memberships": (
            """INSERT INTO personal_model_goal_candidate_memberships
               (generation_id,goal_id,candidate_id,membership_hash) VALUES (?,?,?,?)""",
            ("model-pre-compiler", "goal", "candidate", "hash"),
        ),
    }
    for _table, (statement, params) in immutable_inserts.items():
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute(statement, params)
        database.rollback()

    assert migrate(database) == []
    assert database.execute(
        "SELECT COUNT(*) FROM personal_model_active_generation"
    ).fetchone()[0] == 1
    database.close()
