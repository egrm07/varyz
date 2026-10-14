"""Offline historical report contracts, including privacy revocation and retries."""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from varyz.config import Config
from varyz.db.connection import open_db
from varyz.session_miner import MISSING, mine
from varyz.session_progress import Checkpoint, HistoryQuery, forget, history, pause_path, record


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    cfg = Config(
        base_dir=tmp_path,
        connectors={
            "ai_conversations": {
                "approved": True,
                "adapters": ["claude_code", "codex"],
                "paths": [str(root)],
            }
        },
    )
    conn = open_db(tmp_path / "archive.db")
    yield conn, cfg, root
    conn.close()


def claude(path, messages, *, session="seo", project="varyz", date="2026-10-01"):
    data = [
        {
            "type": role,
            "uuid": f"turn-{i}",
            "sessionId": session,
            "cwd": f"C:/projects/{project}",
            "timestamp": f"{date}T12:00:{i:02d}Z",
            "message": {"role": role, "content": text},
        }
        for i, (role, text) in enumerate(messages)
    ]
    path.write_text("".join(json.dumps(d) + "\n" for d in data), encoding="utf-8")


def reports(conn, cfg, **kwargs):
    return history(conn, HistoryQuery(project="varyz", **kwargs), cfg=cfg)


def test_dry_run_then_apply_citations_no_preferences_and_retry(archive):
    conn, cfg, root = archive
    claude(
        root / "seo.jsonl",
        [
            ("user", "Prepare SEO for Varyz."),
            ("assistant", "Added sitemap.\n12 tests passed.\nBing verification pending."),
        ],
    )
    preview = mine(conn, cfg, project="varyz")
    assert preview["reports"] == 1 and preview["previews"][0]["done"] == ["Added sitemap."]
    assert conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM session_mining_files").fetchone()[0] == 0
    assert mine(conn, cfg, project="varyz", apply=True)["reports"] == 1
    report = reports(conn, cfg)["reports"][0]
    assert report["provenance"] == "mined_report"
    assert report["agent"] == "history:claude_code"
    assert report["checks"] == ["12 tests passed."]
    assert report["pending"] == ["Bing verification pending.", MISSING]
    assert report["status"] == "working" and not report["independently_verified"]
    assert report["occurred_at"].startswith("2026-10-01")
    assert report["evidence"]["response_reference"] == "claude_code:seo:turn-1"
    assert report["evidence"]["facts"][0]["line"] == 2
    assert "approval_scope" not in report["evidence"]
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM owner_knowledge").fetchone()[0] == 0
    again = mine(conn, cfg, project="varyz", apply=True)
    assert again["reports"] == 0 and again["unchanged"] >= 1


def test_negative_product_capabilities_are_not_unfinished_work(archive):
    conn, cfg, root = archive
    claude(root / "benefits.jsonl", [("user", "Prepare SEO."), ("assistant",
        "Built synchronized FAQ data so the sources can't drift apart.\n"
        "Added protection so credentials cannot leak.\n"
        "Implemented stable snapshots that won't overwrite newer work.\n"
        "Cannot deploy until owner verification.\n"
        "Didn't run tests.\n"
        "Unable to connect to Bing.")])
    mine(conn, cfg, project="varyz", apply=True)
    report = reports(conn, cfg)["reports"][0]
    assert len(report["done"]) == 3
    assert report["pending"] == ["Cannot deploy until owner verification.",
                                 "Didn't run tests.", "Unable to connect to Bing.", MISSING]


def test_latest_mined_report_includes_gated_prior_outcomes_within_budget(archive):
    conn, cfg, root = archive
    claude(root / "sequence.jsonl", [
        ("user", "Prepare SEO."), ("assistant", "Added sitemap.\n12 tests passed."),
        ("user", "Check final checklist."), ("assistant", "Google verification pending.")])
    mine(conn, cfg, project="varyz", apply=True)
    query = HistoryQuery(project="varyz", topic="SEO", agent="claude_code")
    current = history(conn, query, cfg=cfg)["reports"][0]
    context = current["earlier_session_outcomes"]
    assert current["task"] == "Check final checklist."
    assert context[0]["task"] == "Prepare SEO."
    assert context[0]["done"] == ["Added sitemap."]
    assert context[0]["checks"] == ["12 tests passed."]
    assert "pending" not in context[0]
    single = history(conn, query.model_copy(update={"limit": 1}), cfg=cfg)["reports"]
    assert len(single) == 1 and len(single[0]["earlier_session_outcomes"]) == 1
    budget = len(json.dumps({k: v for k, v in current.items()
                            if k != "earlier_session_outcomes"}, ensure_ascii=False))
    smaller = history(conn, query, cfg=cfg, max_chars=budget)["reports"][0]
    assert "earlier_session_outcomes" not in smaller
    conn.execute("UPDATE session_progress SET sensitivity='sensitive' WHERE seq=(SELECT MIN(seq) "
                 "FROM session_progress)")
    conn.commit()
    hidden = history(conn, HistoryQuery(project="varyz", agent="claude_code"), cfg=cfg)
    assert "earlier_session_outcomes" not in hidden["reports"][0]


def test_original_dates_not_backfill_order_and_firsthand_wins(archive):
    conn, cfg, root = archive
    claude(
        root / "a-new.jsonl",
        [("user", "New SEO task."), ("assistant", "Added new sitemap.")],
        session="new",
        date="2026-10-06",
    )
    claude(
        root / "z-old.jsonl",
        [("user", "Old SEO task."), ("assistant", "Added old sitemap.")],
        session="old",
        date="2026-09-01",
    )
    mine(conn, cfg, project="varyz", apply=True)
    assert reports(conn, cfg)["reports"][0]["conversation_id"] == "new"
    record(
        conn,
        Checkpoint(
            project="varyz",
            conversation_id="new",
            checkpoint_id="live",
            task="New SEO task",
            status="blocked",
            pending=["Deploy blocked"],
        ),
        agent_id="claude",
    )
    assert reports(conn, cfg)["reports"][0]["pending"] == ["Deploy blocked"]


def test_limited_resume_and_append_latest_snapshot(archive):
    conn, cfg, root = archive
    messages = [
        ("user", "Prepare SEO."),
        ("assistant", "Added sitemap."),
        ("user", "Deploy SEO."),
        ("assistant", "Deployed sitemap."),
    ]
    path = root / "session.jsonl"
    claude(path, messages)
    one = mine(conn, cfg, project="varyz", apply=True, limit=1)
    assert one["limited"] and one["reports"] == 1
    two = mine(conn, cfg, project="varyz", apply=True, limit=1)
    assert two["reports"] == 1 and two["duplicates"] == 1
    assert reports(conn, cfg)["reports"][0]["done"] == ["Deployed sitemap."]
    claude(
        path, messages + [("user", "Fix deploy."), ("assistant", "Deployment failed; needs login.")]
    )
    assert mine(conn, cfg, project="varyz", apply=True)["reports"] == 1
    current = reports(conn, cfg, topic="Prepare SEO")["reports"][0]
    assert current["done"] == [] and current["pending"][0].startswith("Deployment failed")


def test_pause_erasure_and_approval_withdrawal(archive):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    pause_path(cfg.data_dir).touch()
    assert mine(conn, cfg, project="varyz", apply=True)["paused"]
    pause_path(cfg.data_dir).unlink()
    mine(conn, cfg, project="varyz", apply=True)
    cfg.connectors["ai_conversations"]["approved"] = False
    assert reports(conn, cfg)["found"] == 0
    with pytest.raises(ValueError, match="Approve"):
        mine(conn, cfg, project="varyz")
    cfg.connectors["ai_conversations"]["approved"] = True
    forget(conn, "varyz", "seo")
    claude(root / "other-copy.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    assert mine(conn, cfg, project="varyz", apply=True)["erased"] > 0
    assert reports(conn, cfg)["found"] == 0


def test_sensitive_source_and_policy_change_never_bypass_gate(archive):
    conn, cfg, root = archive
    claude(
        root / "session.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap for therapy services.")],
    )
    assert mine(conn, cfg, project="varyz", apply=True)["sensitive"] == 1
    assert reports(conn, cfg)["found"] == 0
    claude(
        root / "clean.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap.")],
        session="clean",
        date="2026-10-02",
    )
    mine(conn, cfg, project="varyz", apply=True)
    assert reports(conn, cfg)["found"] == 1
    cfg.sensitivity.rules.patterns.append("sitemap")
    assert reports(conn, cfg)["found"] == 0
    assert mine(conn, cfg, project="varyz", apply=True)["sensitive"] >= 1
    assert reports(conn, cfg)["found"] == 0


def test_secrets_not_stored_and_no_completion_from_prompt(archive):
    conn, cfg, root = archive
    token = "sk-" + "a" * 48
    claude(
        root / "session.jsonl",
        [
            ("user", "Deploy SEO using " + token),
            ("assistant", "I will deploy the sitemap tomorrow."),
        ],
    )
    mine(conn, cfg, project="varyz", apply=True)
    raw = conn.execute("SELECT payload_json,evidence_json FROM session_progress").fetchone()
    assert token not in str(tuple(raw))
    report = reports(conn, cfg)["reports"][0]
    assert report["done"] == [] and "will deploy" in report["pending"][0]


def test_codex_hidden_reasoning_tools_and_subagents_excluded(archive):
    conn, cfg, root = archive
    path = root / "rollout-session.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "codex-session", "cwd": "C:/projects/varyz"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Prepare SEO."},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "channel": "analysis",
                "content": "Deployed SECRET-REASONING.",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "role": "assistant",
                "content": "Deployed TOOL-TEXT.",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-10-02T12:00:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "channel": "final",
                "content": "Added sitemap.",
            },
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    child = root / "rollout-child.jsonl"
    child_rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "cwd": "C:/projects/varyz",
                "source": {"subagent": "thread"},
            },
        },
        *rows[1:],
    ]
    child.write_text("".join(json.dumps(r) + "\n" for r in child_rows), encoding="utf-8")
    mine(conn, cfg, project="varyz", apply=True, adapter_id="codex")
    report = reports(conn, cfg)["reports"][0]
    assert report["done"] == ["Added sitemap."]
    assert "REASONING" not in json.dumps(report) and "TOOL-TEXT" not in json.dumps(report)
    assert conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 1


def test_user_only_no_date_and_malformed_tail_explicitly_unknown(archive):
    conn, cfg, root = archive
    path = root / "session.jsonl"
    claude(path, [("user", "Deploy SEO.")])
    with path.open("a") as handle:
        handle.write('{"broken":\n')
    result = mine(conn, cfg, project="varyz", apply=True)
    assert result["malformed_lines"] == 1
    report = reports(conn, cfg)["reports"][0]
    assert report["done"] == [] and report["evidence"]["source_incomplete"]
    assert report["evidence"]["response_reference"] is None
    assert conn.execute("SELECT COUNT(*) FROM session_mining_files").fetchone()[0] == 0


def test_local_selection_is_exact_and_invalid_indices_are_retryable(archive):
    conn, cfg, root = archive
    claude(
        root / "session.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap.\nDeployed sitemap.")],
    )
    broken = mine(conn, cfg, project="varyz", method="local", apply=True, select=lambda _: [99])
    assert broken["errors"] == 1 and broken["reports"] == 0
    good = mine(conn, cfg, project="varyz", method="local", apply=True, select=lambda _: [1])
    assert good["reports"] == 1
    assert reports(conn, cfg)["reports"][0]["done"] == ["Deployed sitemap."]
    assert (
        mine(
            conn,
            cfg,
            project="varyz",
            method="local",
            apply=True,
            select=lambda _: pytest.fail("retry must not invoke inference"),
        )["reports"]
        == 0
    )


def test_latest_current_snapshot_uses_source_time_even_backfilled_later(archive):
    conn, cfg, root = archive
    claude(
        root / "first.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Deployed sitemap.")],
        date="2026-10-06",
    )
    mine(conn, cfg, project="varyz", apply=True)
    claude(
        root / "copy-old.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap.")],
        date="2026-09-01",
    )
    mine(conn, cfg, project="varyz", apply=True)
    assert reports(conn, cfg)["reports"][0]["done"] == ["Deployed sitemap."]


def test_configured_roots_since_and_project_are_limits(archive):
    conn, cfg, root = archive
    claude(
        root / "old.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap.")],
        date="2026-09-01",
    )
    claude(
        root / "other.jsonl",
        [("user", "Prepare SEO."), ("assistant", "Added sitemap.")],
        project="other",
    )
    cfg.connectors["ai_conversations"]["since"] = "2026-10-01"
    assert mine(conn, cfg, project="varyz", apply=True)["reports"] == 0
    cfg.connectors["ai_conversations"]["since"] = "invalid"
    with pytest.raises(ValueError, match="ISO"):
        mine(conn, cfg, project="varyz")


def test_cli_help_has_all_options_and_defaults_to_preview(archive, tmp_path):
    from varyz.session_progress_cli import app

    _, _, root = archive
    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\ndata_dir="cli-data"\n[connectors.ai_conversations]\n'
        f'approved=true\nadapters=["claude_code"]\npaths=[{json.dumps(str(root))}]\n'
    )
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    result = CliRunner().invoke(app, ["mine", "varyz", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["apply"] is False
    help_text = CliRunner().invoke(app, ["mine", "--help"], terminal_width=160).output
    assert "Maximum" in help_text and "reports" in help_text and "Store" in help_text


def test_pause_during_inference_prevents_write(archive):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])

    def select(_):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        pause_path(cfg.data_dir).touch()
        return [0]

    assert mine(conn, cfg, project="varyz", method="local", apply=True, select=select)["paused"]
    assert conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 0


def test_changed_paths_withhold_earlier_report(archive):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    mine(conn, cfg, project="varyz", apply=True)
    cfg.connectors["ai_conversations"]["paths"] = [str(root / "selected")]
    assert reports(conn, cfg)["found"] == 0


def test_no_remote_local_model_endpoint(archive):
    from varyz.session_miner import local_selector

    _, cfg, _ = archive
    cfg.local_model.backend = "ollama"
    cfg.ollama.url = "https://example.com"
    with pytest.raises(ValueError, match="loopback"):
        local_selector(cfg)


def test_fact_budget_keeps_report_retrievable(archive):
    conn, cfg, root = archive
    text = "\n".join("Added sitemap item " + str(i) + " for " + "x" * 130 for i in range(20))
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", text)])
    mine(conn, cfg, project="varyz", apply=True)
    result = reports(conn, cfg)
    assert result["found"] == 1 and result["reports"][0]["pending"] == [MISSING]


def test_mcp_miner_authentication_and_pause(tmp_path, monkeypatch):
    from varyz.mcp_server import TOOLS, VaryzServer, handle
    from varyz.trust import register_agent, revoke_agent

    root = tmp_path / "transcripts"
    root.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\ndata_dir="data"\n[connectors.ai_conversations]\n'
        f'approved=true\nadapters=["claude_code"]\npaths=[{json.dumps(str(root))}]\n'
    )
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    server = VaryzServer(config, agent_token="")
    monkeypatch.setattr(server, "_promotion_available", lambda: False)
    try:
        assert "error" in server.call("mine_sessions", {"project": "varyz", "apply": True})
        credential = register_agent(server.conn, agent_id="claude", display_name="Claude")
        server._agent_token = credential.token
        response = handle(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "mine_sessions",
                    "arguments": {"project": "varyz", "apply": True},
                },
            },
        )
        assert response["result"]["structuredContent"]["reports"] == 1
        assert server.call("session_history", {"project": "varyz"})["found"] == 1
        config.write_text(config.read_text().replace("approved=true", "approved=false"))
        assert server.call("session_history", {"project": "varyz"})["found"] == 0
        config.write_text(config.read_text().replace("approved=false", "approved=true"))
        pause_path(server.cfg.data_dir).touch()
        assert server.call("mine_sessions", {"project": "varyz", "apply": True})["paused"]
        revoke_agent(server.conn, "claude")
        assert "error" in server.call("mine_sessions", {"project": "varyz"})
        assert not server._embedder_loaded
        assert "mine_sessions" in {t["name"] for t in TOOLS}
    finally:
        server.close()


def test_rewritten_same_size_mtime_and_content_retry(archive):
    conn, cfg, root = archive
    path = root / "session.jsonl"
    claude(path, [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    mine(conn, cfg, project="varyz", apply=True)
    before = path.stat()
    path.write_text(path.read_text().replace("Added sitemap", "Fixed sitemap"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert mine(conn, cfg, project="varyz", apply=True)["reports"] == 1
    assert reports(conn, cfg)["reports"][0]["done"] == ["Fixed sitemap."]


@pytest.mark.parametrize(
    "text",
    [
        "Not deployed yet.",
        "No changes made; only proposed a fix.",
        "I haven't completed deployment.",
        "Added metadata, except canonical links.",
    ],
)
def test_negations_and_caveats_never_become_done(archive, text):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", text)])
    mine(conn, cfg, project="varyz", apply=True)
    assert reports(conn, cfg)["reports"][0]["done"] == []


def test_policy_revocation_does_not_make_old_topic_discoverable(archive):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    mine(conn, cfg, project="varyz", apply=True)
    cfg.sensitivity.rules.patterns.append("SEO")
    result = reports(conn, cfg, topic="sitemap")
    assert result["found"] == 0 and result["withheld_sensitive"] == 0


def test_headings_and_negative_problem_names_are_not_false_pending(archive):
    conn, cfg, root = archive
    claude(
        root / "session.jsonl",
        [
            ("user", "Prepare SEO."),
            (
                "assistant",
                "**What's live now (I checked each address):**\n"
                "**No more redirects:** the sitemap now points to final pages.\n"
                "Added a page not found screen with HTTP 404.\n**Not done:**\n"
                "Google still needs owner verification.",
            ),
        ],
    )
    mine(conn, cfg, project="varyz", apply=True)
    report = reports(conn, cfg)["reports"][0]
    assert len(report["done"]) == 2
    assert report["pending"] == ["Google still needs owner verification.", MISSING]
    assert all("What's live" not in fact for fact in report["done"])


def test_permission_change_during_selection_stops_without_preview_or_write(archive):
    conn, cfg, root = archive
    claude(root / "session.jsonl", [("user", "Prepare SEO."), ("assistant", "Added sitemap.")])
    permitted = True

    def select(_):
        nonlocal permitted
        permitted = False
        return [0]

    value = mine(
        conn,
        cfg,
        project="varyz",
        apply=True,
        method="local",
        select=select,
        permit=lambda: permitted,
    )
    assert value["permission_changed"] and value["previews"] == [] and value["reports"] == 0
    assert conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0] == 0


def test_findings_from_earlier_messages_are_kept_after_the_final_answer(archive):
    conn, cfg, root = archive
    claude(root / "speed.jsonl", [
        ("user", "Why is search slow?"),
        ("assistant", "Let me look at the query plan."),
        ("assistant", "The cause is a full table scan because the FTS index was never built.\n"
                      "Search takes 900 ms on 40k rows."),
        ("assistant", "Added the FTS index.\nSearch tests passed."),
    ])
    mine(conn, cfg, project="varyz", apply=True)
    report = reports(conn, cfg)["reports"][0]
    assert report["task"] == "Why is search slow?"
    assert report["done"] == ["Added the FTS index."]
    assert report["checks"] == [
        "Search tests passed.",
        "The cause is a full table scan because the FTS index was never built.",
        "Search takes 900 ms on 40k rows.",
    ]
    assert "query plan" not in json.dumps(report)
