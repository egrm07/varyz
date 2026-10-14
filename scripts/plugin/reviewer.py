"""Synthetic reviewer fixture; secrets travel over SSH stdin/stdout into OS keyring.

Run --seed only after server deployment is authorized. --check tests live OAuth and
tools using this dedicated synthetic account. Never puts credentials in a ZIP/report.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from varyz.cloud.plugin_sharing import OPERATIONS

SERVICE = "varyz-plugin-review"
KEY_NAME = "reviewer-2026-10-05"
ORIGIN = "https://cloud.varyzai.com"
REVIEW_CORE_TOOLS = {
    "start_task", "ask", "finish_check", "away", "answer_owner_question",
    "remember_owner_rule", "remember_owner_workflow", "owner_questions", "route_task",
    "search", "fetch", "context", "timeline", "compare", "session_history",
}


def review_tool_names(tools):
    names = {tool["name"] for tool in tools}
    assert len(names) == len(tools), "Duplicate reviewer tools"
    assert names >= REVIEW_CORE_TOOLS, "Required reviewer tools are missing"
    operations = set(OPERATIONS)
    assert names <= REVIEW_CORE_TOOLS | operations | {"collaboration", "follow"}, (
        "Unexpected reviewer tool"
    )
    if names & operations:
        assert names >= operations, "Public sharing operations are missing"
    return names


def seed(settings):
    """Create only new synthetic data; never reads, copies or changes owner records."""
    from varyz.cloud.app import create_app, password_hash
    from varyz.cloud.archive import MODEL, Archive, CachedEmbedder, Change
    from varyz.cloud.payloads import canonical_payload

    store = create_app(settings).state.store
    owner = "plugin_review_" + secrets.token_hex(12)
    password = secrets.token_urlsafe(32)
    email = owner + "@example.test"
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES(?,?,?,'active',1)",
            (owner, email, password_hash(password)),
        )
    preferences = [
        ("rule", "demo-rule", {"statement": "Use short, plain replies.", "scope": "demo"}),
        (
            "answer",
            "demo-answer",
            {
                "question": "Which reply style did I choose for demo?",
                "answer": "Short, plain replies.",
                "scope": "demo",
            },
        ),
        (
            "workflow",
            "demo-workflow",
            {
                "name": "Review a source-backed reply",
                "scope": "demo",
                "steps": [
                    {"step": "Read relevant sources"},
                    {"step": "Draft a short reply"},
                    {"step": "Check sources and instructions"},
                ],
            },
        ),
    ]
    store.push(
        owner,
        [
            {
                "kind": kind,
                "id": identity,
                "base_version": 0,
                "payload": canonical_payload(kind, payload),
            }
            for kind, identity, payload in preferences
        ],
    )
    embedder = (
        CachedEmbedder(settings.archive_embedding_cache)
        if settings.archive_embedding_cache
        else None
    )
    archive = Archive(
        store,
        settings.public_url,
        settings.archive_limit,
        embedder,
        encryption_key=settings.archive_encryption_key,
    )
    texts = [
        "We chose SQLite so demo memory works offline. The database decision is complete. "
        "The next task is timeline implementation, awaiting review.",
        "We kept SQLite so demo memory works offline and added retained-version comparisons. "
        "The database decision is complete. Timeline implementation is awaiting review.",
    ]
    for index, text in enumerate(texts):
        document = dict(
            id="demo-decision",
            project="demo",
            source="documents",
            external_id="synthetic/demo-decision.md",
            title="Demo database decision",
            text=text,
            occurred_at=f"2026-10-0{index + 1}T12:00:00Z",
            sensitivity="personal",
            authored=True,
            chunks=[{"text": text}],
        )
        if embedder:
            document["model"] = MODEL
            document["chunks"][0]["vector"] = embedder.embed_documents([text])[0]
        archive.push(owner, [Change(id="demo-decision", base_version=index, payload=document)])
    return {"email": email, "password": password, "account": owner, "origin": settings.public_url}


def remote_seed():
    import keyring

    if keyring.get_password(SERVICE, KEY_NAME):
        raise RuntimeError("Reviewer already exists in OS keyring; use --check, do not duplicate")
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(Path.home() / ".ssh/varyz_cloud_ed25519"),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "root@2.31.3.96",
            "docker exec -i varyz-cloud-cloud-1 python - --service-seed",
        ],
        input=Path(__file__).read_bytes(),
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Reviewer provisioning failed; output withheld to protect credentials")
    value = json.loads(result.stdout)
    if value["origin"] != ORIGIN or not value["account"].startswith("plugin_review_"):
        raise RuntimeError("Unexpected reviewer destination")
    keyring.set_password(SERVICE, KEY_NAME, json.dumps(value))
    if keyring.get_password(SERVICE, KEY_NAME) != json.dumps(value):
        raise RuntimeError("OS keyring verification failed")
    print(
        "Reviewer provisioned: synthetic preferences/history only; "
        "credentials verified in OS keyring"
    )


def oauth(client, email, password, requested_scope="varyz.plugin"):
    """Use real sign-in, CSRF consent and PKCE; no synthetic bearer shortcut."""
    client.get(ORIGIN + "/login").raise_for_status()
    response = client.post(
        ORIGIN + "/login",
        data={
            "csrf": client.cookies.get("__Host-csrf") or client.cookies.get("csrf"),
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 303, "Reviewer sign-in failed"
    redirect = "http://127.0.0.1:8766/callback"
    registered = client.post(ORIGIN + "/oauth/register", json={"redirect_uris": [redirect]})
    registered.raise_for_status()
    client_id = registered.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    params = dict(
        client_id=client_id,
        redirect_uri=redirect,
        response_type="code",
        code_challenge_method="S256",
        state=secrets.token_urlsafe(24),
        code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("="),
        resource=ORIGIN + "/mcp/plugin",
        scope=requested_scope,
    )
    consent = client.get(ORIGIN + "/oauth/authorize", params=params)
    assert consent.status_code == 200
    assert "approved synced project history and session reports" in consent.text
    consent = client.post(
        ORIGIN + "/oauth/authorize",
        data={**params, "csrf": client.cookies.get("__Host-csrf") or client.cookies.get("csrf")},
    )
    returned = parse_qs(urlsplit(consent.headers["location"]).query)
    assert returned["state"][0] == params["state"]
    exchanged = client.post(
        ORIGIN + "/oauth/token",
        data=dict(
            grant_type="authorization_code",
            client_id=client_id,
            code=returned["code"][0],
            code_verifier=verifier,
            redirect_uri=redirect,
            resource=params["resource"],
        ),
    )
    exchanged.raise_for_status()
    token = exchanged.json()
    assert token["scope"] == "varyz.plugin"
    return client_id, token


def check():
    import httpx
    import keyring

    raw = keyring.get_password(SERVICE, KEY_NAME)
    if not raw:
        raise RuntimeError("Provision the reviewer after authorized server deployment first")
    reviewer = json.loads(raw)
    assert reviewer["origin"] == ORIGIN
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        endpoint = ORIGIN + "/mcp/plugin"
        initialized = client.post(endpoint, json=dict(jsonrpc="2.0", id=1, method="initialize"))
        assert initialized.status_code == 200 and "serverInfo" in initialized.json()["result"]
        discovered = client.post(endpoint, json=dict(jsonrpc="2.0", id=2, method="tools/list"))
        assert discovered.status_code == 200
        tools = discovered.json()["result"]["tools"]
        names = review_tool_names(tools)
        for tool in tools:
            expected = [{"type": "oauth2", "scopes": ["varyz.plugin"]}]
            assert tool["securitySchemes"] == expected
            assert tool["_meta"]["securitySchemes"] == expected
            blocked = client.post(endpoint, json=dict(
                jsonrpc="2.0", id=3, method="tools/call",
                params=dict(name=tool["name"], arguments={}),
            ))
            assert blocked.status_code == 200
            result = blocked.json()["result"]
            assert result["isError"]
            challenge = result["_meta"]["mcp/www_authenticate"][0]
            assert 'scope="varyz.plugin"' in challenge
            assert '/oauth-protected-resource/mcp/plugin"' in challenge
        print(f"PASS anonymous initialize, {len(tools)} OAuth tool descriptions, "
              f"all {len(tools)} calls denied with link challenge")
        client_id, token = oauth(
            client, reviewer["email"], reviewer["password"],
            requested_scope="varyz varyz.archive.read varyz.context.read varyz.plugin",
        )
        headers = {"Authorization": "Bearer " + token["access_token"]}

        def rpc(method, name=None, arguments=None):
            message = dict(jsonrpc="2.0", id=1, method=method)
            if name:
                message["params"] = dict(name=name, arguments=arguments or {})
            response = client.post(ORIGIN + "/mcp/plugin", headers=headers, json=message)
            response.raise_for_status()
            result = response.json()["result"]
            assert not result.get("isError"), "Reviewer tool failed"
            return result

        try:
            rpc("initialize")
            listed = rpc("tools/list")["tools"]
            assert review_tool_names(listed) == names
            for name, arguments, evidence in [
                ("start_task", {"project": "demo"}, "Use short, plain replies"),
                (
                    "ask",
                    {"project": "demo", "question": "Which reply style did I choose for demo?"},
                    "Short, plain replies",
                ),
                ("context", {"project": "demo"}, "demo-decision"),
                ("search", {"project": "demo", "query": "SQLite offline"}, "demo-decision"),
                ("fetch", {"id": "demo-decision"}, "retained-version comparisons"),
                ("timeline", {"project": "demo"}, "awaiting review"),
                ("compare", {"id": "demo-decision"}, "original"),
                ("finish_check", {"project": "demo"}, "Use short, plain replies"),
            ]:
                assert evidence in json.dumps(rpc("tools/call", name, arguments)), name
            for path in ("/api/export", "/api/archive/status"):
                assert client.get(ORIGIN + path, headers=headers).status_code == 403
            assert (
                client.post(
                    ORIGIN + "/api/archive/sync", headers=headers, json={"records": []}
                ).status_code
                == 403
            )
            assert client.delete(ORIGIN + "/api/account", headers=headers).status_code == 403
            refreshed = client.post(
                ORIGIN + "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": token["refresh_token"],
                },
            )
            refreshed.raise_for_status()
            token = refreshed.json()
            assert token["scope"] == "varyz.plugin"
            headers["Authorization"] = "Bearer " + token["access_token"]
            rpc("tools/list")
            print(
                f"PASS live reviewer sign-in, OAuth consent, PKCE, {len(listed)} tools, "
                "8 source/preference calls, denied management and refresh"
            )
        finally:
            response = client.post(
                ORIGIN + "/oauth/revoke",
                data={"client_id": client_id, "token": token["refresh_token"]},
            )
            assert response.status_code == 200
            denied = client.post(
                ORIGIN + "/mcp/plugin",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert denied.status_code == 401
            print("PASS test connection revoked; reviewer fixture retained for platform review")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--service-seed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.service_seed:
        from varyz.cloud.app import Settings

        print(json.dumps(seed(Settings.from_env())))
    elif args.seed:
        remote_seed()
    else:
        check()
