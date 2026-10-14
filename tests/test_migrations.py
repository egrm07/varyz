"""Forward-only migration coverage from every supported schema version."""

from __future__ import annotations

from importlib import resources

import pytest

from varyz.db.connection import _iter_migrations, connect, migrate


@pytest.mark.parametrize("starting_version", [1, 2, 3, 4])
def test_each_supported_schema_upgrades_without_losing_entries(
    tmp_path, starting_version
):
    conn = connect(tmp_path / f"schema-v{starting_version}.db")
    try:
        conn.execute(
            """CREATE TABLE schema_migrations
               (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
        )
        migration_dir = resources.files("varyz.db") / "migrations"
        for version, filename in (
            (1, "0001_init.sql"),
            (2, "0002_significance.sql"),
            (3, "0003_context_platform.sql"),
            (4, "0004_destination_audit.sql"),
        ):
            if version > starting_version:
                break
            conn.executescript(
                (migration_dir / filename).read_text(encoding="utf-8")
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-01-01T00:00:00+00:00"),
            )
        conn.execute(
            """INSERT INTO entries
               (id, kind, source, external_id, content_hash, body, ingested_at)
               VALUES ('preserved', 'note', 'manual', 'source-1', 'hash-1',
                       'keep me', '2026-01-01T00:00:00+00:00')"""
        )
        conn.commit()

        migrate(conn)

        assert conn.execute(
            "SELECT body FROM entries WHERE id='preserved'"
        ).fetchone()[0] == "keep me"
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(egress_executions)")
        }
        assert {"adapter_version", "approval_scope_json"} <= columns
        request_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(context_requests)")
        }
        assert "conversation_id" in request_columns
        grant_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(consent_grants)")
        }
        assert {"scope_valid_from", "scope_valid_to"} <= grant_columns
        # Derived, not hardcoded: a list spelled out here has to be edited by
        # every migration, and the edit is indistinguishable from the mistake
        # of a migration that never ran.
        expected = [version for version, _, _ in _iter_migrations()]
        assert [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == expected
        gateway_tables = {
            row["name"]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name LIKE 'agent_%'"""
            )
        }
        assert {
            "agent_principals",
            "agent_tokens",
            "agent_capabilities",
            "agent_effects",
        } <= gateway_tables
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(entity_aliases)")
        }
        assert {
            "idx_entity_alias_source_identity",
            "idx_entity_alias_verified_identity",
        } <= indexes
    finally:
        conn.close()


def test_applied_owner_knowledge_schema_gets_caller_device_forward_only(tmp_path):
    conn = connect(tmp_path / "schema-v33.db")
    try:
        conn.execute(
            """CREATE TABLE schema_migrations
               (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
        )
        for version, _name, sql in _iter_migrations():
            if version > 33:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-09-19T00:20:32+00:00"),
            )
        conn.execute(
            """INSERT INTO owner_knowledge
               (id,question,normalized_question,answer,basis,source,
                caller_agent_id,caller_display_name,caller_purpose,caller_transport,
                device_id,monotonic_seq,stated_at,created_at)
               VALUES ('old','Question?','question','Answer','explicit','manual',
                       'owner:local','Local owner','owner_knowledge','stdio',
                       'device:legacy',1,?,?)""",
            ("2026-09-19T00:20:32+00:00", "2026-09-19T00:20:32+00:00"),
        )
        conn.commit()

        assert migrate(conn) == [
            "0034_owner_knowledge_caller_device.sql",
            "0035_owner_knowledge_retractions.sql",
            "0036_owner_principles.sql",
            "0037_scope_and_domains.sql",
            "0038_authorship_outcomes_recipients.sql",
            "0039_owner_workflows.sql",
            "0040_workflow_turn_labels.sql",
            "0041_workflow_nudges.sql",
            "0042_away_mode.sql",
            "0043_cloud_offer.sql",
            "0044_trial_rule_uses.sql",
        "0045_task_routes.sql",
        "0046_session_progress.sql",
        "0047_session_mining.sql",
        ]
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(owner_knowledge)")
        }
        assert "caller_device_id" in columns
        assert conn.execute(
            "SELECT caller_device_id FROM owner_knowledge WHERE id='old'"
        ).fetchone()[0] == "device:local"
    finally:
        conn.close()
