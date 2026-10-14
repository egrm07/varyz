"""Check hosted transport with the official MCP SDK and synthetic preferences.

Pipe `seed` output from the container into `client` on the test machine. Never
print the intermediate credentials; the client deletes its synthetic account.
"""

import argparse
import asyncio
import json
import secrets
import site
import sys
from pathlib import Path


def verify_tools(listed):
    names = {tool.name for tool in listed.tools}
    required = {
        "owner_questions", "answer_owner_question", "away", "start_task", "ask",
        "finish_check", "remember_owner_rule", "remember_owner_workflow", "route_task",
    }
    assert required <= names, f"Missing Cloud tools: {required - names}"
    assert names <= required | {"collaboration", "follow"}, f"Unexpected Cloud tools: {names}"


def seed():
    from varyz.cloud.app import LOCAL_CLIENT, Settings, password_hash
    from varyz.cloud.store import Store

    settings = Settings.from_env()
    assert settings.paddle_sandbox
    store = Store(settings.database)
    owner = "sdk_probe_" + secrets.token_hex(8)
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES(?,?,?,'active',1)",
            (owner, owner + "@example.test", password_hash(secrets.token_urlsafe(32))),
        )
    print(
        json.dumps(
            dict(
                url=settings.public_url, token=store.credential(owner, "access", 600, LOCAL_CLIENT)
            )
        )
    )


async def client():
    sdk_path = str(Path(".tmp/mcp-sdk").resolve())
    sys.path.insert(0, sdk_path)
    site.addsitedir(sdk_path)
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    state = json.load(sys.stdin)
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + state["token"]}, timeout=30
    ) as http:
        try:
            async with (
                streamable_http_client(state["url"] + "/mcp", http_client=http) as streams,
                ClientSession(*streams) as session,
            ):
                initialized = await session.initialize()
                print("PASS official MCP SDK initialize " + initialized.protocol_version)
                listed = await session.list_tools()
                verify_tools(listed)
                rule = await session.call_tool(
                    "remember_owner_rule",
                    arguments={"statement": "Use short synthetic sentences", "scope": "probe"},
                )
                assert not rule.is_error
                briefing = await session.call_tool("start_task", arguments={"project": "probe"})
                assert not briefing.is_error
                assert "Use short synthetic sentences" in briefing.content[0].text
                print("PASS official SDK tool discovery, write and fresh-task retrieval")
                bad = await session.call_tool(
                    "answer_owner_question",
                    arguments={"question_id": "missing", "answer": "Synthetic"},
                )
                assert bad.is_error
                print("PASS official SDK tool error interoperability")
        finally:
            deleted = await http.delete(state["url"] + "/api/account")
            assert deleted.status_code == 200
            print("PASS synthetic SDK account removed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "client"])
    action = parser.parse_args().action
    if action == "seed":
        seed()
    else:
        asyncio.run(client())
