"""Run in the existing Cloud container: synthetic consent and hosted MCP over HTTPS."""

import json
import secrets

import httpx

from varyz.cloud.app import Settings, create_app
from varyz.cloud.archive import PLUGIN_SCOPE


def main():
    settings = Settings.from_env()
    assert settings.archive_encryption_key, "Storage encryption is required"
    app = create_app(settings)
    store = app.state.store
    identities = ["report_probe_" + secrets.token_hex(8) for _ in range(2)]
    try:
        for identity in identities:
            with store.connection() as db:
                db.execute(
                    "INSERT INTO accounts(id,email,password,status,email_verified) "
                    "VALUES(?,?,?,'active',1)",
                    (identity, identity + "@example.test", "unusable-probe-password"),
                )
        local = {
            "Authorization": "Bearer "
            + store.credential(identities[0], "access", 300, "varyz-local")
        }
        plugin = {
            "Authorization": "Bearer "
            + store.credential(identities[0], "access", 300, "report-probe", scope=PLUGIN_SCOPE)
        }
        other = {
            "Authorization": "Bearer "
            + store.credential(identities[1], "access", 300, "report-probe", scope=PLUGIN_SCOPE)
        }
        payload = dict(
            project="report-probe",
            conversation_id="synthetic-seo",
            agent="agent:claude-code:probe",
            task="Prepare synthetic SEO",
            status="blocked",
            done=["Added sitemap"],
            checks=["12 metadata checks passed"],
            pending=["Search verification pending"],
            artifacts=[],
            seq=1,
            recorded_at="2026-10-07T12:00:00Z",
            occurred_at="2026-10-07T12:00:00Z",
            reference="varyz-progress:synthetic-report",
            provenance="agent_report",
            independently_verified=False,
        )
        batch = {"records": [{"id": "synthetic-report", "base_version": 0, "payload": payload}]}
        with httpx.Client(base_url=settings.public_url, timeout=30) as http:

            def tool(headers, method="tools/call"):
                response = http.post(
                    "/mcp/plugin",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {
                            "name": "session_history",
                            "arguments": {
                                "project": "report-probe",
                                "agent": "claude",
                                "topic": "SEO",
                            },
                        },
                    },
                )
                assert response.status_code == 200, response.status_code
                return response.json()["result"]

            assert http.post("/api/reports/sync", headers=local, json=batch).status_code == 422
            assert (
                http.post(
                    "/api/reports/permit", headers=local, json={"project": "report-probe"}
                ).status_code
                == 200
            )
            first = http.post("/api/reports/sync", headers=local, json=batch)
            assert first.status_code == 200, first.status_code
            assert http.post("/api/reports/sync", headers=local, json=batch).json() == first.json()
            listed = tool(plugin, "tools/list")["tools"]
            found = next(t for t in listed if t["name"] == "session_history")
            assert found["annotations"]["readOnlyHint"]
            current = tool(plugin)["structuredContent"]["reports"][0]
            assert all(current[k] == value for k, value in payload.items())
            assert tool(other)["structuredContent"]["found"] == 0
            assert http.get("/reports/synthetic-report", headers=other).status_code == 404
            assert (
                http.post(
                    "/api/reports/permit", headers=plugin, json={"project": "report-probe"}
                ).status_code
                == 403
            )
            with store.connection() as db:
                row = db.execute(
                    "SELECT * FROM cloud_session_reports WHERE account=?", (identities[0],)
                ).fetchone()
                assert "sitemap" not in row["payload"] and row["project"] != "report-probe"
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM archive_permissions WHERE account=?", (identities[0],)
                    ).fetchone()[0]
                    == 0
                )
            assert (
                http.post(
                    "/api/reports/revoke", headers=local, json={"project": "report-probe"}
                ).status_code
                == 200
            )
            assert tool(plugin)["structuredContent"]["found"] == 0
            assert http.get("/reports/synthetic-report", headers=plugin).status_code == 404
        print(
            json.dumps(
                {
                    "live_https": "passed",
                    "plugin_tool_discovery": "passed",
                    "approved_report_retrieval": "passed",
                    "consent_isolation": "passed",
                    "tenant_isolation": "passed",
                    "retry": "passed",
                    "encrypted_storage": "passed",
                    "revocation": "passed",
                    "data": "disposable synthetic reports only",
                }
            )
        )
    finally:
        with store.connection() as db:
            for identity in identities:
                for table in (
                    "session_report_terms",
                    "cloud_session_reports",
                    "session_report_permissions",
                    "credentials",
                ):
                    db.execute("DELETE FROM " + table + " WHERE account=?", (identity,))
                db.execute("DELETE FROM accounts WHERE id=?", (identity,))
        if hasattr(store, "close"):
            store.close()


if __name__ == "__main__":
    main()
