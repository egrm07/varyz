# ruff: noqa: F811
"""Public sharing tools expose fixed operations and retain account isolation."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_cloud_following import BASE, cloud, publish  # noqa: F401
from varyz.cloud.archive import PLUGIN_SCOPE
from varyz.cloud.plugin_sharing import OPERATIONS, dispatch, tools


def rpc(client, headers, method, name=None, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if name:
        body["params"] = {"name": name, "arguments": arguments or {}}
    return client.post("/mcp/plugin", headers=headers, json=body).json()["result"]


def test_public_tools_have_no_action_switches_and_describe_permissions():
    listed = tools()
    assert len(listed) == 18
    for tool in listed:
        assert "action" not in tool["inputSchema"]["properties"]
        assert tool["inputSchema"]["additionalProperties"] is False
        with pytest.raises(ValidationError):
            dispatch(tool["name"], {"action": "remove"})
    by_name = {tool["name"]: tool for tool in listed}
    assert by_name["collaboration_remove"]["annotations"]["destructiveHint"]
    assert by_name["collaboration_invite"]["annotations"]["openWorldHint"]
    assert by_name["following_read_standards"]["annotations"]["readOnlyHint"]
    with pytest.raises(ValidationError):
        dispatch("following_remove", {})


def test_plugin_discovery_and_real_follow_operations_remain_scoped(cloud):
    app, store, following = cloud
    publish(following, store, "ann")
    client = TestClient(app, base_url=BASE)
    headers = {
        "Authorization": "Bearer "
        + store.credential("egr", "access", 3600, "plugin-test", scope=PLUGIN_SCOPE)
    }
    listed = rpc(client, {}, "tools/list")["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    assert set(OPERATIONS) <= by_name.keys()
    assert not {"collaboration", "follow"} & by_name.keys()
    for name in OPERATIONS:
        assert by_name[name]["securitySchemes"] == [{"type": "oauth2", "scopes": [PLUGIN_SCOPE]}]
        denied = rpc(client, {}, "tools/call", name)
        assert denied["isError"]
    added = rpc(client, headers, "tools/call", "following_add", {"username": "ann"})
    assert not added.get("isError")
    assert json.loads(added["content"][0]["text"])["status"] == "following"
    assert following.following("ann")["following"] == []
    status = rpc(client, headers, "tools/call", "following_list")
    assert "@ann" in json.dumps(status)
    smuggled = rpc(client, headers, "tools/call", "following_list", {"action": "unfollow"})
    assert smuggled["isError"]
    assert "@ann" in json.dumps(rpc(client, headers, "tools/call", "following_list"))
    removed = rpc(client, headers, "tools/call", "following_remove", {"username": "ann"})
    assert not removed.get("isError")
    assert following.following("egr")["following"] == []
