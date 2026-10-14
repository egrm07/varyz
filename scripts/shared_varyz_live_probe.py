"""Authorized two-account HTTPS pilot; synthetic records only in joemama.

Run inside the deployed Cloud container. Never prints credentials or email.
Temporary browser/access grants are erased even on failure. A clearly named demo
collaboration is retained for the native-host acceptance check.
"""

import json
import secrets
from urllib.parse import urlparse

import httpx

from varyz.cloud.app import Settings, create_app
from varyz.cloud.sharing_service import SCOPE, identifier
from varyz.cloud.store import digest


def main():
    app = create_app(Settings.from_env())
    store = app.state.store
    origin = app.state.settings.public_url
    assert origin == "https://cloud.varyzai.com"
    with store.connection() as db:
        accounts = {
            r["username"]: dict(r)
            for r in db.execute(
                "SELECT id,username,email,status,email_verified FROM accounts "
                "WHERE username IN ('egr','joemama')"
            ).fetchall()
        }
    assert set(accounts) == {"egr", "joemama"}
    assert all(a["email_verified"] for a in accounts.values())
    owner_id, guest_id = accounts["joemama"]["id"], accounts["egr"]["id"]
    grants = []
    clients = []
    shares = []
    touched = []  # Every share this run made, so its notices never reach a real inbox.
    success = False
    project = "shared-varyz-pilot"
    selected_id, private_id = identifier(), identifier()
    private_marker = "Synthetic unselected marker " + secrets.token_hex(12)
    store.push(
        owner_id,
        [
            {
                "kind": "rule",
                "id": selected_id,
                "base_version": 0,
                "payload": {
                    "statement": (
                        "For this Shared Varyz demo, use short paragraphs "
                        "and include a test checklist."
                    ),
                    "scope": project,
                },
            },
            {
                "kind": "rule",
                "id": private_id,
                "base_version": 0,
                "payload": {"statement": private_marker, "scope": project},
            },
        ],
    )

    def grant(account, kind, **kwargs):
        value = store.credential(account, kind, 900, **kwargs)
        grants.append(digest(value))
        return value

    def browser(account):
        client = httpx.Client(base_url=origin, timeout=20, follow_redirects=False)
        clients.append(client)
        client.cookies.set(
            "__Host-session", grant(account, "session"), domain=urlparse(origin).hostname, path="/"
        )
        client.cookies.set("__Host-csrf", identifier(), domain=urlparse(origin).hostname, path="/")
        return client

    def api(client, path, data, expected=200):
        result = client.post(
            path,
            json=data,
            headers={
                "Origin": origin,
                "X-CSRF-Token": client.cookies.get("__Host-csrf"),
                "Idempotency-Key": identifier(),
            },
        )
        assert result.status_code == expected, (path, result.status_code)
        return result.json()

    def create(owner, label):
        preview = api(
            owner,
            "/api/sharing/v1/shares/preview",
            {
                "project": project,
                "label": label,
                "selection": [{"kind": "rule", "id": selected_id, "version": 1}],
            },
        )
        result = api(
            owner,
            "/api/sharing/v1/shares",
            {
                "preview_id": preview["preview_id"],
                "source_fingerprint": preview["source_fingerprint"],
                "invited_email": accounts["egr"]["email"],
            },
        )
        shares.append(result["share_id"])
        touched.append(result["share_id"])
        return result

    def accept(client, invitation, expected=303):
        path = "/invite/" + invitation["invite_url"].rsplit("/", 1)[-1] + "/accept"
        result = client.post(
            path,
            data={"csrf": client.cookies.get("__Host-csrf"), "idempotency_key": identifier()},
            headers={"Origin": origin},
        )
        assert result.status_code == expected, ("invite", result.status_code)
        if expected == 303:
            return result.headers["location"].rsplit("/", 1)[-1]

    try:
        owner, guest = browser(owner_id), browser(guest_id)
        invitation = create(owner, "Shared Varyz HTTPS acceptance test")
        accept(owner, invitation, 403)
        member = accept(guest, invitation)
        access = grant(guest_id, "access", client="shared-pilot-http", scope=SCOPE)
        headers = {"Authorization": "Bearer " + access}

        def tool(name, arguments, *, error=False):
            response = guest.post(
                "/mcp/shared",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert bool(result.get("isError")) == error
            assert private_marker not in json.dumps(result)
            return result.get("structuredContent", result)

        projects = tool("shared_projects", {})
        assert any(m["membership_id"] == member for m in projects["memberships"])
        task = {"membership_id": member, "conversation_id": "https-pilot-" + identifier()}
        brief = tool("shared_brief", {**task, "expected_binding_revision": 0})
        assert len(brief["items"]) == 1 and not brief["truncated"]
        bound = {**task, "binding_revision": brief["binding_revision"]}
        asked = tool(
            "shared_ask",
            {
                **bound,
                "idempotency_key": identifier(),
                "requests": [
                    {
                        "question": "Which demo accent should we use?",
                        "reason": "Synthetic HTTPS test",
                    }
                ],
            },
        )
        thread = asked["results"][0]["question_id"]
        api(
            owner,
            "/api/sharing/v1/questions/" + thread + "/reply",
            {
                "expected_thread_version": 1,
                "answer": "Use marigold for this demo.",
                "remember_project": False,
                "publish_to_share": True,
                "expected_publication_revision": 1,
            },
        )
        status = tool("shared_question_status", {**bound, "thread_ids": [thread]})
        assert status["questions"][0]["reply"]["answer"] == "Use marigold for this demo."
        assert tool("shared_finish", bound)["brief_changed"]
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        share_id = invitation["share_id"]
        owner_headers = {
            "Authorization": "Bearer " + grant(owner_id, "access", client="shared-pilot-owner")
        }

        def collaborate(arguments):
            response = owner.post(
                "/api/sharing/v1/owner/collaboration", json=arguments, headers=owner_headers
            )
            assert response.status_code == 200, (arguments["action"], response.status_code)
            return response.json()

        def rebrief():
            value = tool("shared_brief", task)
            return {**task, "binding_revision": value["binding_revision"]}, value

        listed = owner.post("/mcp", json=rpc, headers=owner_headers).json()["result"]["tools"]
        assert "collaboration" in {t["name"] for t in listed}
        collaborate({"action": "set_level", "membership_id": member, "level": "decide"})
        bound, brief = rebrief()
        assert brief["collaboration"]["level"] == "decide"
        routed = collaborate(
            {
                "action": "ask_partner",
                "share_id": share_id,
                "question": "Which demo hosting should we use?",
                "reason": "Owner is away (synthetic test)",
            }
        )
        assert routed["partners"] == ["egr"]
        bound, brief = rebrief()
        assert [q["question_id"] for q in brief["collaboration"]["to_decide"]] == [
            routed["question_id"]
        ]
        decided = tool(
            "shared_answer",
            {**bound, "question_id": routed["question_id"], "answer": "Static demo hosting."},
        )
        assert decided["authority"] == "partner_decision"
        assert decided["permission_grant"] is False
        work = {
            "summary": "Synthetic demo section for the Shared Varyz test.",
            "branch": "egr/shared-demo",
            "commit": "abc1234",
            "run": "npm run dev",
            "provisional": [{"question": "Demo button radius?", "answer": "8px"}],
            "disagreements": [
                {"preference": "Use short paragraphs.", "suggestion": "Allow one long intro."}
            ],
        }
        report = tool("shared_finish", {**bound, "work": work})["report"]
        assert report["for_owner_review"] == 2
        assert tool("shared_finish", {**bound, "work": work})["report"]["duplicate"]
        waiting = collaborate({"action": "digest", "project": project})["shares"]
        assert [(s["share_id"], s["new_reports"], s["to_review"]) for s in waiting] == [
            (share_id, 1, 3)
        ]
        collaborate(
            {
                "action": "link_repo",
                "share_id": share_id,
                "repo": "https://github.com/varyz-demo/shared-varyz-demo.git",
            }
        )
        checks = {"git_installed": True, "gh_installed": True, "gh_signed_in": True}
        requested = tool("shared_github", {**bound, **checks, "github_username": "egr-demo"})
        assert requested["state"] == "requested"
        status = collaborate({"action": "status"})
        detail = [s for s in status["shares"] if s["share_id"] == share_id][0]
        assert detail["reports"][0]["branch"] == "egr/shared-demo"
        assert detail["access_requests"][0]["command"].endswith(
            "collaborators/egr-demo -f permission=push"
        )
        collaborate({"action": "access_granted", "membership_id": member})
        ready = tool("shared_github", {**bound, **checks, "has_access": True, "cloned": True})
        assert ready["state"] == "accepted" and "git worktree add" in ready["next_step"]
        for item in detail["review"]:
            decision = "dismiss" if item["kind"] == "disagreement" else "keep"
            collaborate({"action": "review", "review_id": item["review_id"], "decision": decision})
        assert collaborate({"action": "digest", "project": project}) == {"shares": []}
        collaborate(
            {"action": "report", "share_id": share_id, "work": {"summary": "Owner demo merged."}}
        )
        bound, brief = rebrief()
        assert brief["collaboration"]["reports"][0]["summary"] == "Owner demo merged."
        owner_call = owner.post(
            "/mcp",
            headers=owner_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "collaboration", "arguments": {"action": "status"}},
            },
        ).json()["result"]
        assert not owner_call.get("isError") and share_id in json.dumps(owner_call)
        for path in ("/mcp", "/mcp/plugin", "/mcp/archive", "/mcp/context"):
            assert guest.post(path, json=rpc, headers=headers).status_code == 403
        assert guest.get("/api/export", headers=headers).status_code == 403
        with store.connection() as db:
            revisions = db.execute(
                "SELECT m.revision AS member,p.revision AS share FROM shared_memberships m "
                "JOIN shared_projects p ON p.id=m.share WHERE m.id=?",
                (member,),
            ).fetchone()
        api(
            owner,
            "/api/sharing/v1/memberships/" + member + "/revoke",
            {"expected_revision": revisions["member"]},
        )
        tool("shared_question_status", {**bound, "thread_ids": [thread]}, error=True)
        api(
            owner,
            "/api/sharing/v1/shares/" + share_id + "/state",
            {"status": "deleted", "expected_publication_revision": revisions["share"]},
        )
        with store.connection() as db:
            for table in ("shared_reports", "shared_suggestions", "shared_owner_questions"):
                left = db.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE share=?", (share_id,)
                ).fetchone()
                assert left["n"] == 0, table
        shares.remove(invitation["share_id"])
        with store.connection() as db:
            db.execute(
                "DELETE FROM shared_projects WHERE owner=? AND label=?",
                (owner_id, "Shared Varyz demo from joemama"),
            )
        # The owner's agent adds egr by username; egr's agent joins without the link.
        added = collaborate(
            {
                "action": "add",
                "username": "egr",
                "project": "shared-varyz-username-test",
                "level": "decide",
            }
        )
        shares.append(added["share_id"])
        touched.append(added["share_id"])
        assert added["status"] == "invited" and added["shared_preferences"] == 0, added
        pending = tool("shared_projects", {})["invitations"]
        assert added["invite_id"] in [i["invite_id"] for i in pending]
        joined = tool("shared_join", {"invite_id": added["invite_id"]})["membership_id"]
        named = {"membership_id": joined, "conversation_id": "https-username-" + identifier()}
        brief = tool("shared_brief", {**named, "expected_binding_revision": 0})
        assert brief["collaboration"]["level"] == "decide"
        changed = collaborate(
            {"action": "add", "username": "egr", "share_id": added["share_id"], "level": "consult"}
        )
        assert changed["status"] == "level_changed" and changed["membership_id"] == joined
        collaborate({"action": "remove", "username": "egr", "share_id": added["share_id"]})
        tool("shared_brief", {**named, "expected_binding_revision": 1}, error=True)
        api(
            owner,
            "/api/sharing/v1/shares/" + added["share_id"] + "/state",
            {"status": "deleted", "expected_publication_revision": 1},
        )
        shares.remove(added["share_id"])
        demo = create(owner, "Shared Varyz demo from joemama")
        demo_member = accept(guest, demo)
        # egr's local Varyz sees the invitation as a desktop notice.
        own = {"Authorization": "Bearer " + grant(guest_id, "access", client="shared-pilot-http")}
        texts = [
            n["text"] for n in guest.get("/api/sharing/v1/notices", headers=own).json()["notices"]
        ]
        assert "joemama invited you to Shared Varyz demo from joemama at Consult" in texts, texts
        # Decide, so a native client can try every guest tool on the retained demo.
        collaborate({"action": "set_level", "membership_id": demo_member, "level": "decide"})
        shares.remove(demo["share_id"])
        success = True
        print(
            json.dumps(
                {
                    "accounts": ["joemama", "egr"],
                    "transport": "real HTTPS",
                    "checks": {
                        "selected_only": True,
                        "wrong_email_denied": True,
                        "all_guest_tools": True,
                        "owner_reply": True,
                        "scope_firewall": True,
                        "revocation": True,
                        "erasure": True,
                        "owner_collaboration_tool": True,
                        "level_change": True,
                        "away_question_to_decide_partner": True,
                        "partner_answer": True,
                        "work_report_and_dedupe": True,
                        "owner_review_and_digest": True,
                        "github_handshake": True,
                        "owner_report_reaches_guest": True,
                        "collaboration_erasure": True,
                        "add_by_username": True,
                        "join_from_agent": True,
                        "remove_by_username": True,
                        "invitation_notice": True,
                    },
                    "demo_share_id": demo["share_id"],
                    "demo_membership_id": demo_member,
                    "native_host_verified": False,
                }
            )
        )
    finally:
        for client in clients:
            client.close()
        with store.connection() as db:
            for value in grants:
                db.execute("DELETE FROM credential_grants WHERE hash=?", (value,))
                db.execute("DELETE FROM credentials WHERE hash=?", (value,))
            for share_id in shares:
                db.execute(
                    "DELETE FROM shared_projects WHERE id=? AND owner=?", (share_id, owner_id)
                )
            for share_id in touched:
                db.execute("DELETE FROM shared_notices WHERE share=?", (share_id,))
        changes = [{"kind": "rule", "id": private_id, "base_version": 1, "payload": None}]
        if not success:
            changes.append({"kind": "rule", "id": selected_id, "base_version": 1, "payload": None})
        store.push(owner_id, changes)


if __name__ == "__main__":
    main()
