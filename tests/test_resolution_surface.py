from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from varyz.db.connection import open_db
from varyz.install import AGENT_TOKEN_ENV, SERVER_KEY, connect_agent, server_entry
from varyz.mcp_server import LEGACY_TOOLS, TOOLS, VaryzServer
from varyz.personal_model.query import QueryDisposition
from varyz.resolution.client import ResolutionClientError
from varyz.resolution.models import Resolution
from varyz.trust import (
    AgentAuthenticationError,
    caller_from_token,
    capability_limits,
    default_low_trust_caller,
    grant_agent_capability,
    hash_agent_token,
    mint_agent_token,
    register_agent,
    register_agent_principal,
    revoke_agent,
)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return path


def test_agent_token_is_hashed_and_revocation_is_fail_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = open_db(tmp_path / "data" / "varyz.db")
    credential = register_agent(
        conn,
        agent_id="agent:codex:local",
        display_name="Codex",
        device_id="local",
    )

    stored = conn.execute(
        "SELECT token_hash FROM agent_tokens WHERE id=?", (credential.token_id,)
    ).fetchone()[0]
    assert stored == hash_agent_token(credential.token)
    assert credential.token not in stored
    assert caller_from_token(conn, credential.token).agent_id == "agent:codex:local"

    assert revoke_agent(conn, credential.caller.agent_id, token_id=credential.token_id) == 1
    with pytest.raises(AgentAuthenticationError, match="revoked"):
        caller_from_token(conn, credential.token)
    conn.close()
    assert config.exists()


def test_unregistered_caller_is_explicitly_low_trust_and_bounded() -> None:
    caller = default_low_trust_caller()

    assert caller.agent_id == "agent:unregistered"
    assert caller.capability_id is None


def test_capability_limits_are_the_disclosure_ceiling(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "varyz.db")
    register_agent_principal(conn, agent_id="agent:test", display_name="Test agent")
    capability_id = grant_agent_capability(
        conn,
        "agent:test",
        max_items=2,
        max_chars=321,
    )
    token, _ = mint_agent_token(conn, "agent:test")
    caller = caller_from_token(conn, token)

    assert caller.capability_id == capability_id
    assert capability_limits(conn, caller) == (2, 321)
    conn.close()


def test_resolve_is_the_writable_product_tool_and_mirror_is_retired() -> None:
    tools = {tool["name"]: tool for tool in TOOLS + LEGACY_TOOLS}

    assert "mirror" not in tools
    resolve = tools["resolve"]
    assert resolve["annotations"]["readOnlyHint"] is False
    description = resolve["description"]
    assert "instead of guessing" in description
    assert "NOT_KNOWN" in description
    assert "ESCALATED" in description


class _FakeResolutionClient:
    def __init__(self) -> None:
        self.requests = []

    def resolve(self, request):
        self.requests.append(request)
        return Resolution(
            disposition=QueryDisposition.NOT_KNOWN,
            basis=None,
            answer=None,
            tier=5,
            confidence=1,
            resolved_at=datetime.now(UTC),
            monotonic_seq=1,
        )


def test_mcp_resolve_binds_the_authenticated_caller(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = open_db(tmp_path / "data" / "varyz.db")
    credential = register_agent(
        conn,
        agent_id="agent:codex:local",
        display_name="Codex",
        device_id="device-1",
    )
    conn.close()
    client = _FakeResolutionClient()
    server = VaryzServer(
        config,
        agent_token=credential.token,
        resolution_client=client,
    )
    try:
        payload = server.call(
            "resolve",
            {"question": "Which API shape did the owner choose?"},
        )
    finally:
        server.close()

    assert payload["disposition"] == "not_known"
    assert client.requests[0].caller.agent_id == "agent:codex:local"
    assert client.requests[0].caller.capability_id is not None


def test_mcp_resolve_reports_daemon_outage_without_claiming_unknown(tmp_path: Path) -> None:
    class OfflineClient:
        def resolve(self, request):
            raise ResolutionClientError("connection refused")

    server = VaryzServer(_config(tmp_path), resolution_client=OfflineClient())
    try:
        payload = server.call("resolve", {"question": "What did the owner decide?"})
    finally:
        server.close()
    assert payload["error"] == "resolution_daemon_unavailable"
    assert payload["question"] == "What did the owner decide?"
    assert "disposition" not in payload


def test_connect_is_idempotent_across_identity_config_and_skill(tmp_path: Path) -> None:
    config = _config(tmp_path)
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text("", encoding="utf-8")

    def runner(arguments: list[str]) -> None:
        if arguments[:3] == ["mcp", "remove", SERVER_KEY]:
            codex_config.write_text("", encoding="utf-8")
            return
        assert arguments[:3] == ["mcp", "add", SERVER_KEY]
        env_index = arguments.index("--env")
        token = arguments[env_index + 1].split("=", 1)[1]
        separator = arguments.index("--")
        command, *command_args = arguments[separator + 1 :]
        codex_config.write_text(
            f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {json.dumps(command)}\n"
            f"args = {json.dumps(command_args)}\n"
            f"\n[mcp_servers.{SERVER_KEY}.env]\n"
            f"{AGENT_TOKEN_ENV} = {json.dumps(token)}\n",
            encoding="utf-8",
        )

    first_credential, first = connect_agent(
        "codex",
        config_path=config,
        paths={"codex": codex_config},
        codex_runner=runner,
    )
    second_credential, second = connect_agent(
        "codex",
        config_path=config,
        paths={"codex": codex_config},
        codex_runner=runner,
    )

    assert first_credential is not None and second_credential is not None
    assert first_credential.token == second_credential.token
    assert first[0].changed is True
    assert second[0].changed is False
    assert server_entry(config, agent_token=first_credential.token)["env"] == {
        AGENT_TOKEN_ENV: first_credential.token
    }
    skill = home / ".agents" / "skills" / "varyz-context" / "SKILL.md"
    assert "When you are about to assume something about the owner" in skill.read_text(
        encoding="utf-8"
    )

    conn = open_db(tmp_path / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    ("autostart", "detail", "started"),
    [
        (True, "resolution daemon unavailable: refused", True),
        (False, "resolution daemon unavailable: refused", False),
        (True, "daemon returned HTTP 403: forbidden", False),
    ],
)
def test_mcp_resolve_starts_a_down_loopback_daemon_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, autostart: bool, detail: str, started: bool
) -> None:
    import varyz.resolution.client as client_module

    calls: list[str] = []

    class FlakyClient:
        def __init__(self, *args, **kwargs) -> None:
            self.up = False

        def resolve(self, request):
            calls.append("resolve")
            if not self.up:
                raise ResolutionClientError(detail)
            return _FakeResolutionClient().resolve(request)

    def fake_ensure(client, config_path, **kwargs):
        calls.append("ensure")
        client.up = True
        return object()

    monkeypatch.setattr(client_module, "ResolutionClient", FlakyClient)
    monkeypatch.setattr(client_module, "ensure_daemon", fake_ensure)
    config = _config(tmp_path)
    if not autostart:
        config.write_text(
            config.read_text(encoding="utf-8") + "[resolution]\nautostart = false\n",
            encoding="utf-8",
        )
    server = VaryzServer(config)
    try:
        payload = server.call("resolve", {"question": "What did the owner decide?"})
    finally:
        server.close()

    if started:
        assert calls == ["resolve", "ensure", "resolve"]
        assert payload["disposition"] == "not_known"
    else:
        assert calls == ["resolve"]
        assert payload["error"] == "resolution_daemon_unavailable"
