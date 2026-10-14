"""Verify the deployed read-only connector through the official MCP SDK."""

import asyncio
import json
import site
import subprocess
import sys
from pathlib import Path


def verify_tools(listed):
    assert {tool.name for tool in listed.tools} == {
        "search", "fetch", "session_history", "context", "timeline", "compare",
    }
    assert all(tool.annotations.read_only_hint for tool in listed.tools)


async def main():
    sdk = Path(".tmp/mcp-sdk").resolve()
    sys.path.insert(0, str(sdk))
    site.addsitedir(str(sdk))
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    seed = """import json,secrets
from varyz.cloud.app import Settings,create_app
from varyz.cloud.archive import CONTEXT_SCOPE
s=Settings.from_env(); store=create_app(s).state.store
owner='context_sdk_'+secrets.token_hex(8)
with store.connection() as db:
    db.execute("INSERT INTO accounts(id,email,password,status,email_verified) "
               "VALUES(?,?,?,'active',1)",(owner,owner+'@example.test','unused'))
print(json.dumps({'url':s.public_url,
    'upload':store.credential(owner,'access',600,'varyz-local'),
    'read':store.credential(owner,'access',600,'sdk',scope=CONTEXT_SCOPE)}))
"""
    state = json.loads(
        subprocess.check_output(
            [
                "ssh",
                "-i",
                str(Path.home() / ".ssh/varyz_cloud_ed25519"),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "root@2.31.3.96",
                "docker exec -i varyz-cloud-cloud-1 python -",
            ],
            input=seed.encode(),
            timeout=30,
        )
    )
    async with httpx2.AsyncClient(timeout=30) as management:
        upload = {"Authorization": "Bearer " + state["upload"]}
        try:
            text = "The demo project uses SQLite for offline memory."
            doc = dict(
                id="sdk-source",
                project="demo",
                source="documents",
                external_id="demo.md",
                title="Database decision",
                text=text,
                occurred_at="2026-10-01T12:00:00Z",
                sensitivity="personal",
                chunks=[{"text": text}],
            )
            response = await management.post(
                state["url"] + "/api/archive/sync",
                headers=upload,
                json={"records": [{"id": doc["id"], "base_version": 0, "payload": doc}]},
            )
            assert response.status_code == 200
            async with (
                httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + state["read"]}, timeout=30
            ) as http, streamable_http_client(
                    state["url"] + "/mcp/context", http_client=http
                ) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                verify_tools(tools)
                result = await session.call_tool(
                    "search", arguments={"query": "SQLite offline memory"}
                )
                assert not result.is_error and "sdk-source" in result.content[0].text
                result = await session.call_tool("fetch", arguments={"id": "sdk-source"})
                assert not result.is_error and text in result.content[0].text
                result = await session.call_tool("context", arguments={"project": "demo"})
                assert not result.is_error and "sdk-source" in result.content[0].text
            print(
                "PASS official MCP SDK initialize, read-only tools, "
                "source search/fetch and project context"
            )
        finally:
            response = await management.delete(state["url"] + "/api/account", headers=upload)
            assert response.status_code == 200
            print("PASS disposable SDK account removed")


if __name__ == "__main__":
    asyncio.run(main())
