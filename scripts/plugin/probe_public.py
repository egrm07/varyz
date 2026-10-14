"""Read public OAuth metadata and verify unauthenticated Cloud tool denial.

No account credentials, archive access, registrations, uploads or server writes.
"""

import httpx

ORIGIN = "https://cloud.varyzai.com"


def probe(client, origin: str = ORIGIN) -> list[str]:
    checked = []
    for resource, scope in (
        ("/mcp", "varyz"),
        ("/mcp/context", "varyz.context.read"),
        ("/mcp/archive", "varyz.archive.read"),
        ("/mcp/plugin", "varyz.plugin"),
        ("/mcp/shared", "varyz.shared"),
    ):
        suffix = "" if resource == "/mcp" else resource
        metadata_url = origin + "/.well-known/oauth-protected-resource" + suffix
        response = client.get(metadata_url)
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("resource") != origin + resource:
            raise RuntimeError(f"{resource}: unexpected OAuth resource")
        if scope not in metadata.get("scopes_supported", []):
            raise RuntimeError(f"{resource}: expected OAuth scope unavailable")
        response = client.post(
            origin + resource,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        if resource == "/mcp/plugin":
            # Hosts discover the plugin's OAuth policy before linking an account.
            # Tool execution must still return the MCP authentication challenge.
            response.raise_for_status()
            if response.json().get("result", {}).get("serverInfo", {}).get("name") != "varyz-cloud":
                raise RuntimeError("Plugin initialization failed")
            listed = client.post(
                origin + resource,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            listed.raise_for_status()
            tools = listed.json().get("result", {}).get("tools", [])
            if not tools:
                raise RuntimeError("Plugin tool discovery returned no tools")
            for tool in tools:
                security = [{"type": "oauth2", "scopes": [scope]}]
                if tool.get("securitySchemes") != security:
                    raise RuntimeError("Plugin tool lacks the required OAuth scope")
                blocked = client.post(
                    origin + resource,
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": tool["name"], "arguments": {}}},
                )
                blocked.raise_for_status()
                result = blocked.json().get("result", {})
                challenges = result.get("_meta", {}).get("mcp/www_authenticate", [])
                if (
                    result.get("isError") is not True
                    or metadata_url not in blocked.headers.get("WWW-Authenticate", "")
                    or blocked.headers.get("WWW-Authenticate") not in challenges
                ):
                    raise RuntimeError(f"Plugin tool {tool['name']}: authentication not enforced")
        else:
            if response.status_code != 401:
                raise RuntimeError(f"{resource}: unauthenticated MCP access was not denied")
            if metadata_url not in response.headers.get("WWW-Authenticate", ""):
                raise RuntimeError(f"{resource}: OAuth challenge identifies the wrong resource")
        checked.append(resource)
        print(f"{resource}: public discovery passed; unauthenticated tool access denied")
    return checked


def main() -> None:
    with httpx.Client(timeout=20) as client:
        probe(client)


if __name__ == "__main__":
    main()
