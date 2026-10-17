# ruff: noqa: F811
"""Report consent, encrypted storage and retrieval through real hosted MCP routes."""

import json
from contextlib import closing

import pytest

from tests.test_cloud_archive import hosted  # noqa: F401
from tests.test_guided_cloud import setup  # noqa: F401
from varyz.cloud.archive import CONTEXT_SCOPE, PLUGIN_SCOPE
from varyz.cloud.session_report_sync import ReportSync, read_journal
from varyz.session_progress import Checkpoint, record


def report(identity="one", *, project="varyz", agent="agent:claude-code:test", **kw):
    value = dict(
        project=project,
        conversation_id="seo-session",
        agent=agent,
        task="Improve SEO metadata",
        status="working",
        done=["Updated page titles"],
        checks=["Metadata validation passed"],
        pending=["Publish changes"],
        artifacts=[],
        recorded_at="2026-10-07T12:00:00Z",
        occurred_at="2026-10-07T12:00:00Z",
        seq=1,
        reference="varyz-progress:" + identity,
        provenance="agent_report",
        independently_verified=False,
    )
    return {**value, **kw}


def upload(client, headers, payload, base_version=0):
    return client.post(
        "/api/reports/sync",
        headers=headers,
        json={
            "records": [
                {
                    "id": payload["reference"].split(":")[1],
                    "base_version": base_version,
                    "payload": payload,
                }
            ]
        },
    )


def permit(client, headers, project="varyz"):
    response = client.post("/api/reports/permit", headers=headers, json={"project": project})
    assert response.status_code == 200, response.text


def call(client, headers, arguments, endpoint="/mcp/plugin", name="session_history"):
    response = client.post(
        endpoint,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def plugin_auth(store, owner="alice"):
    token = store.credential(owner, "access", 3600, "chatgpt", scope=PLUGIN_SCOPE)
    return {"Authorization": "Bearer " + token}


def test_consent_independent_of_rules_and_raw_history(hosted):
    client, store, auth = hosted
    local = auth()
    client.post("/api/archive/permit", headers=local, json={"project": "varyz"})
    assert upload(client, local, report()).status_code == 422
    permit(client, local)
    assert upload(client, local, report()).status_code == 200
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM archive_documents").fetchone()[0] == 0


@pytest.mark.parametrize("endpoint", ["/mcp/plugin", "/mcp/context", "/mcp/archive", "/mcp"])
def test_discovery_and_use_through_hosted_mcp(hosted, endpoint):
    client, store, auth = hosted
    local = auth()
    permit(client, local)
    assert upload(client, local, report()).status_code == 200
    headers = plugin_auth(store) if endpoint == "/mcp/plugin" else auth(readonly=True)
    if endpoint == "/mcp/context":
        token = store.credential("alice", "access", 3600, "context", scope=CONTEXT_SCOPE)
        headers = {"Authorization": "Bearer " + token}
    listed = client.post(
        endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]
    tool = next(t for t in listed if t["name"] == "session_history")
    assert tool["annotations"]["readOnlyHint"]
    result = call(
        client, headers, {"project": "varyz", "agent": "claude", "topic": "SEO"}, endpoint
    )
    value = json.loads(result["content"][0]["text"])
    assert value["found"] == 1 and value["reports"][0]["pending"] == ["Publish changes"]
    assert value["reports"][0]["independently_verified"] is False
    assert client.get("/reports/one", headers=headers).status_code == 200
    for action in ("permit", "revoke", "sync"):
        assert client.post("/api/reports/" + action, headers=headers, json={}).status_code == 403


def test_exact_project_and_tenant_isolation(hosted):
    client, store, auth = hosted
    permit(client, auth(), "san7.14")
    assert upload(client, auth(), report(project="san7.15")).status_code == 422
    assert upload(client, auth(), report(project="SAN7.14")).status_code == 200
    reader = plugin_auth(store)
    assert call(client, reader, {"project": "san7"})["structuredContent"]["found"] == 0
    assert call(client, reader, {"project": "san7.14"})["structuredContent"]["found"] == 1
    assert client.get("/reports/one", headers=plugin_auth(store, "bob")).status_code == 404
    assert upload(client, auth("unpaid"), report()).status_code == 402


def test_old_topic_returns_current_state_not_old_success(hosted):
    client, store, auth = hosted
    permit(client, auth())
    upload(client, auth(), report())
    upload(
        client,
        auth(),
        report(
            "two",
            task="Release follow-up",
            seq=2,
            status="blocked",
            occurred_at="2026-10-07T13:00:00Z",
            done=[],
            checks=[],
            pending=["Deployment failed"],
        ),
    )
    upload(
        client,
        auth(),
        report("decoy", agent="agent:codex:test", seq=3, occurred_at="2026-10-07T14:00:00Z"),
    )
    value = call(
        client, plugin_auth(store), {"project": "varyz", "agent": "claude_code", "topic": "SEO"}
    )["structuredContent"]
    assert value["reports"][0]["reference"] == "varyz-progress:two"
    assert value["reports"][0]["pending"] == ["Deployment failed"]
    # Literal topic words must occur together in one checkpoint.
    assert (
        call(
            client,
            plugin_auth(store),
            {"project": "varyz", "agent": "claude", "topic": "SEO follow-up"},
        )["structuredContent"]["found"]
        == 0
    )


def test_idempotence_revocation_and_immutable_resurrection(hosted):
    client, store, auth = hosted
    local = auth()
    permit(client, local)
    first = upload(client, local, report()).json()
    assert upload(client, local, report()).json() == first
    assert upload(client, local, report(task="Changed immutable report"), 1).status_code == 422
    assert (
        client.post("/api/reports/revoke", headers=local, json={"project": "varyz"}).status_code
        == 200
    )
    assert client.get("/reports/one", headers=plugin_auth(store)).status_code == 404
    assert upload(client, local, report(), 2).status_code == 422
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM session_report_terms").fetchone()[0] == 0
        assert db.execute("SELECT payload FROM cloud_session_reports").fetchone()[0] == "null"
    permit(client, local)
    assert upload(client, local, report(task="Forged replacement"), 2).status_code == 422
    assert upload(client, local, report(), 2).status_code == 200


def test_pagination_is_bound_to_owner_and_query(hosted):
    client, store, auth = hosted
    permit(client, auth())
    for i in range(5):
        assert upload(client, auth(), report(str(i), seq=i + 1)).status_code == 200
    headers = plugin_auth(store)
    query = {"project": "varyz", "conversation_id": "seo-session", "latest": False, "limit": 2}
    first = call(client, headers, query)["structuredContent"]
    second = call(client, headers, {**query, "cursor": first["next_cursor"]})["structuredContent"]
    third = call(client, headers, {**query, "cursor": second["next_cursor"]})["structuredContent"]
    refs = [p["reference"] for v in (first, second, third) for p in v["reports"]]
    assert len(set(refs)) == 5 and third["next_cursor"] is None
    assert call(client, headers, {**query, "limit": 3, "cursor": first["next_cursor"]})["isError"]
    assert call(client, plugin_auth(store, "bob"), {**query, "cursor": first["next_cursor"]})[
        "isError"
    ]


def checkpoint(conn, i=1, sensitivity="personal", project="varyz"):
    return record(
        conn,
        Checkpoint(
            project=project,
            conversation_id="local-session",
            checkpoint_id=str(i),
            task="SEO changes",
            done=["Improved metadata"],
            pending=["Check deployment"],
            sensitivity=sensitivity,
        ),
        agent_id="agent:claude-code:local",
    )


def test_checkbox_preview_then_approved_sync_and_revoke(setup):
    wizard, conn, store, requests = setup
    saved = checkpoint(conn)
    choices = wizard.choices()
    assert choices["report_selected"] == []
    assert choices["report_projects"] == [{"name": "varyz", "count": 1}]
    preview = wizard.preview(["*"], report_projects=[])
    assert preview["report_uploads"] == 0 and requests == []
    preview = wizard.preview(["*"], report_projects=["varyz"])
    assert preview["report_uploads"] == 1
    assert "Improved metadata" not in json.dumps(preview)
    finished = wizard.finish(preview)
    assert finished["state"] == "complete" and finished["reports"]["uploaded"] == 1
    sync = ReportSync(conn, wizard.config)
    assert sync.preview()["uploads"] == 0
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM archive_permissions").fetchone()[0] == 0
        row = db.execute("SELECT * FROM cloud_session_reports").fetchone()
        assert "metadata" not in row["payload"] and row["project"] != "varyz"
        assert row["agent"] != "agent:claude-code:local"
    preview = wizard.preview(["*"], report_projects=[])
    assert preview["report_deletions"] == 1
    wizard.finish(preview)
    assert read_journal(sync.path)["projects"] == {}
    with store.connection() as db:
        assert db.execute("SELECT payload FROM cloud_session_reports").fetchone()[0] == "null"
    assert saved["recorded"]


def test_preview_changed_before_approval_sends_nothing(setup):
    from varyz.cloud.guided import PreviewChanged

    wizard, conn, store, requests = setup
    checkpoint(conn)
    preview = wizard.preview(["*"], report_projects=["varyz"])
    checkpoint(conn, 2)
    with pytest.raises(PreviewChanged):
        wizard.finish(preview)
    assert requests == [("GET", "/api/account")]
    assert not read_journal(ReportSync(conn, wizard.config).path)["enabled"]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM cloud_session_reports").fetchone()[0] == 0


def test_sensitive_latest_removes_cloud_history(setup):
    wizard, conn, store, _ = setup
    checkpoint(conn)
    wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    checkpoint(conn, 2, "sensitive")
    sync = ReportSync(conn, wizard.config)
    preview = sync.preview()
    assert preview["uploads"] == 0 and preview["deletions"] == 1
    with wizard_client(wizard) as client:
        assert sync.sync(client, "owner")["deleted"] == 1
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM session_report_terms").fetchone()[0] == 0


def wizard_client(wizard):
    from varyz.cloud import guided

    return closing(guided.CloudClient("unused", {}))


def test_unified_versioned_project_and_future_reports_respect_remote_revocation(setup):
    wizard, conn, _, requests = setup
    checkpoint(conn, 1, project="san7.14")
    checkpoint(conn, 2, project="san7.15")
    checkpoint(conn, 3, project="other")
    choices = wizard.choices()
    assert [p["name"] for p in choices["sync_projects"]] == ["other", "san7"]
    preview = wizard.preview_projects(["san7"])
    assert preview["report_projects"] == ["san7.14", "san7.15"]
    assert preview["report_uploads"] == 2 and requests == []
    wizard.finish(preview)
    sync = ReportSync(conn, wizard.config)
    assert "san7" in wizard.choices()["sync_selected"]
    checkpoint(conn, 4, project="san7.16")
    with wizard_client(wizard) as client:
        assert sync.sync(client, "owner")["uploaded"] == 1
        assert set(read_journal(sync.path)["projects"]) == {"san7.14", "san7.15", "san7.16"}
        client.request("POST", "/api/reports/revoke", json={"project": "san7.16"})
        checkpoint(conn, 5, project="san7.17")
        assert sync.sync(client, "owner")["uploaded"] == 0
        journal = read_journal(sync.path)
        assert "san7.16" not in journal["projects"] and "san7.17" not in journal["projects"]
        assert journal["project_scopes"] == []


def test_legacy_exact_report_approval_does_not_expand_to_future_versions(setup):
    wizard, conn, _, _ = setup
    checkpoint(conn, 1, project="san7.14")
    wizard.finish(wizard.preview(["*"], report_projects=["san7.14"]))
    checkpoint(conn, 2, project="san7.15")
    with wizard_client(wizard) as client:
        assert ReportSync(conn, wizard.config).sync(client, "owner")["uploaded"] == 0
    assert set(read_journal(ReportSync(conn, wizard.config).path)["projects"]) == {"san7.14"}


def test_resume_after_applied_batch_lost_ack(setup):
    wizard, conn, store, _ = setup
    for i in range(28):
        checkpoint(conn, i)
    sync = ReportSync(conn, wizard.config)
    _, _, signature = sync.draft(["varyz"])
    sync.select(["varyz"], signature, "owner", "https://cloud.varyzai.com")
    with wizard_client(wizard) as client:
        original = client.request
        lost = False

        def interrupt(method, path, **kw):
            nonlocal lost
            result = original(method, path, **kw)
            if path == "/api/reports/sync" and not lost:
                lost = True
                raise ValueError("Simulated lost acknowledgement")
            return result

        client.request = interrupt
        with pytest.raises(ValueError, match="lost acknowledgement"):
            sync.sync(client, "owner")
        client.request = original
        assert sync.sync(client, "owner")["uploaded"] == 28
        assert sync.sync(client, "owner")["uploaded"] == 0
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM cloud_session_reports").fetchone()[0] == 28
        assert db.execute("SELECT MAX(version) FROM cloud_session_reports").fetchone()[0] == 1


def test_remote_revocation_cannot_be_undone_by_autosync(setup):
    wizard, conn, store, _ = setup
    checkpoint(conn)
    wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    with wizard_client(wizard) as client:
        client.request("POST", "/api/reports/revoke", json={"project": "varyz"})
        checkpoint(conn, 2)
        assert ReportSync(conn, wizard.config).sync(client, "owner")["uploaded"] == 0
    assert read_journal(ReportSync(conn, wizard.config).path)["projects"] == {}
    with store.connection() as db:
        assert db.execute("SELECT revoked FROM session_report_permissions").fetchone()[0] == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"sensitivity": "sensitive"},
        {"independently_verified": True},
        {"task": "credential sk-" + "a" * 48},
        {"status": "complete"},
    ],
)
def test_wire_contract_rejects_private_or_unbounded_claims(hosted, extra):
    client, _, auth = hosted
    permit(client, auth())
    response = upload(client, auth(), report(**extra))
    assert response.status_code == 422
    assert "a" * 48 not in response.text


def test_mined_reports_recheck_source_consent_before_upload_and_erasure(setup, tmp_path):
    from tests.test_session_miner import claude
    from varyz.config import load_config
    from varyz.session_miner import mine

    wizard, conn, store, _ = setup
    sources = tmp_path / "transcripts"
    sources.mkdir()
    wizard.config.write_text(
        wizard.config.read_text()
        + "\n[connectors.ai_conversations]\napproved=true\n"
        + 'adapters=["claude_code"]\npaths=['
        + json.dumps(sources.as_posix())
        + "]\n"
    )
    claude(
        sources / "seo.jsonl",
        [
            ("user", "Improve SEO"),
            ("assistant", "Added sitemap.\n12 tests passed."),
            ("user", "Checklist?"),
            ("assistant", "Review pending."),
        ],
    )
    assert mine(conn, load_config(wizard.config), project="varyz", apply=True)["reports"] == 2
    result = wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    assert result["reports"]["uploaded"] == 2
    with wizard_client(wizard) as client:
        value = client.request(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "session_history",
                    "arguments": {
                        "project": "varyz",
                        "agent": "claude",
                        "topic": "SEO",
                        "limit": 1,
                    },
                },
            },
        )["result"]["structuredContent"]["reports"][0]
        assert value["occurred_at"].startswith("2026-10-01")
        assert value["earlier_session_outcomes"][0]["done"] == ["Added sitemap."]
        wizard.config.write_text(
            wizard.config.read_text().replace("approved=true", "approved=false")
        )
        assert ReportSync(conn, wizard.config).sync(client, "owner")["deleted"] == 2
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM cloud_session_reports WHERE payload!='null'"
            ).fetchone()[0]
            == 0
        )


def test_live_checkpoint_arrives_through_existing_background_sync(setup, monkeypatch):
    from varyz.cloud import background, guided
    from varyz.cloud.live_sync import LiveCloudSync

    wizard, conn, store, _ = setup
    checkpoint(conn)
    wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    checkpoint(conn, 2)
    worker = LiveCloudSync(
        wizard.config,
        wizard.config.parent / "data/varyz.db",
        client=lambda _: guided.CloudClient("unused", {}),
    )
    assert worker._sync(conn, only_if_planned=True)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM cloud_session_reports").fetchone()[0] == 2
    checkpoint(conn, 3)
    monkeypatch.setattr(background, "CloudClient", guided.CloudClient)
    monkeypatch.setattr(background, "load_tokens", lambda _: {})
    assert "1 sent" in background.sync_if_selected(wizard.config, conn)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM cloud_session_reports").fetchone()[0] == 3


def test_since_uses_source_date_and_never_approves_upload(setup):
    wizard, conn, _, _ = setup
    checkpoint(conn)
    sync = ReportSync(conn, wizard.config)
    wizard.config.write_text(
        wizard.config.read_text() + '\n[session_reports]\ncloud_since="2099-01-01"\n'
    )
    assert sync.preview()["uploads"] == 0 and not sync.preview()["enabled"]
    _, plan, _ = sync.draft(["varyz"])
    assert not plan["changes"]
    result = wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    assert result["state"] == "complete_empty" and result["reports"]["uploaded"] == 0
    wizard.config.write_text(wizard.config.read_text().replace("2099-01-01", "2000-01-01"))
    _, plan, _ = sync.draft(["varyz"])
    assert len(plan["changes"]) == 1


def test_new_owner_approval_can_renew_remote_revocation(setup):
    wizard, conn, _, _ = setup
    checkpoint(conn)
    wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    with wizard_client(wizard) as client:
        client.request("POST", "/api/reports/revoke", json={"project": "varyz"})
    # The owner explicitly reviews and finishes again, before autosync saw revocation.
    result = wizard.finish(wizard.preview(["*"], report_projects=["varyz"]))
    assert result["reports"]["uploaded"] == 1
    assert result["state"] == "complete"
    with wizard_client(wizard) as client:
        assert client.request("GET", "/api/reports/status")["records"] == 1


def test_wrong_account_and_disabled_sync_send_no_report_content(setup):
    wizard, conn, _, _ = setup
    checkpoint(conn)
    sync = ReportSync(conn, wizard.config)

    class WrongAccount:
        url = "https://cloud.varyzai.com"

        def __init__(self):
            self.calls = []

        def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            assert path == "/api/account"
            return {"id": "different-owner"}

    client = WrongAccount()
    assert sync.sync(client, "owner")["paused"] and client.calls == []
    _, _, signature = sync.draft(["varyz"])
    sync.select(["varyz"], signature, "owner", client.url)
    with pytest.raises(ValueError, match="another account"):
        sync.sync(client, "owner")
    assert client.calls == [("GET", "/api/account")]


def test_backfill_reuses_plan_but_rechecks_new_private_state_each_batch(setup):
    wizard, conn, store, _ = setup
    for i in range(28):
        checkpoint(conn, i)
    sync = ReportSync(conn, wizard.config)
    _, _, signature = sync.draft(["varyz"])
    sync.select(["varyz"], signature, "owner", "https://cloud.varyzai.com")
    planned = 0
    original_plan = sync.plan

    def count_plan(journal=None):
        nonlocal planned
        planned += 1
        return original_plan(journal)

    sync.plan = count_plan
    with wizard_client(wizard) as client:
        original_request = client.request
        written = 0

        def privacy_changes(method, path, **kwargs):
            nonlocal written
            result = original_request(method, path, **kwargs)
            if path == "/api/reports/sync" and method == "POST":
                written += 1
                if written == 1:
                    checkpoint(conn, 100, "sensitive")
            return result

        client.request = privacy_changes
        outcome = sync.sync(client, "owner")
    assert outcome["uploaded"] == 25 and outcome["deleted"] == 25
    assert planned == 2
    with store.connection() as db:
        query = "SELECT 1 FROM cloud_session_reports WHERE payload!='null'"
        assert not db.execute(query).fetchone()


def test_unchanged_backfill_plans_once_for_multiple_batches(setup):
    wizard, conn, _, _ = setup
    for i in range(28):
        checkpoint(conn, i)
    sync = ReportSync(conn, wizard.config)
    _, _, signature = sync.draft(["varyz"])
    sync.select(["varyz"], signature, "owner", "https://cloud.varyzai.com")
    planned = 0
    original_plan = sync.plan

    def count_plan(journal=None):
        nonlocal planned
        planned += 1
        return original_plan(journal)

    sync.plan = count_plan
    with wizard_client(wizard) as client:
        outcome = sync.sync(client, "owner")
    assert outcome["uploaded"] == 28 and planned == 1
