"""Offline plugin packaging, bootstrap and explicit migration boundaries."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import keyring
import pytest

from varyz import plugin

ROOT = Path(__file__).resolve().parents[1]


def test_generated_bundles_have_no_drift():
    script = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    for path, content in script["generated"]().items():
        assert path.read_bytes() == content, f"Regenerate {path}"


def test_hosted_listing_assets_cases_and_no_activation_sales_skill():
    base = ROOT / "plugins/chatgpt"
    manifest = json.loads((base / ".codex-plugin/plugin.json").read_text())
    interface = manifest["interface"]
    assert len(interface["shortDescription"]) <= 30
    assert interface["longDescription"]
    for field in ("composerIcon", "logo"):
        assert (base / interface[field]).is_file()
    review = manifest["extensions"]["com.openai"]["review"]
    assert review["commerce"] is False
    assert len(review["test_cases"]["positive"]) == 5
    assert len(review["test_cases"]["negative"]) == 3
    assert all(case["tools_triggered"] for case in review["test_cases"]["positive"])
    assert not (base / "skills/varyz-cloud-activate").exists()


def test_private_hosted_binding_does_not_leak_into_publisher_package(tmp_path):
    script = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    public = script["generated"]()
    base = ROOT / "plugins/chatgpt"
    before = dict(public)
    private = script["private_hosted_files"](public, "plugin_asdk_app_synthetic")
    assert public == before
    assert base / ".app.json" not in public
    assert json.loads(private[base / ".app.json"]) == {
        "apps": {"varyz": {"id": "asdk_app_synthetic", "required": True}}
    }
    for name in ("plugin.json", ".codex-plugin/plugin.json"):
        manifest = json.loads(private[base / name])
        assert manifest["extensions"]["com.openai"]["apps"] == "./.app.json"
        original = json.loads(public[base / name])
        expected_cases = original["extensions"]["com.openai"]["review"].pop("test_cases")
        assert json.loads(private[base / "references/private-review-cases.json"]) == expected_cases
        original.pop("mcpServers", None)
        manifest["extensions"]["com.openai"].pop("apps")
        manifest.pop("apps", None)
        assert manifest == original
    for name in ("mcp.json", ".mcp.json"):
        assert base / name not in private
        assert base / name in public
    for name in ("skills/varyz-get-started/SKILL.md",):
        assert private[base / name] == public[base / name]
    for candidate, expected in ((public, False), (private, True)):
        target = tmp_path / ("private.zip" if expected else "public.zip")
        script["hosted_zip"](candidate, target)
        with zipfile.ZipFile(target) as archive:
            assert (".app.json" in archive.namelist()) is expected
            assert ("mcp.json" in archive.namelist()) is not expected
            assert (".mcp.json" in archive.namelist()) is not expected


@pytest.mark.parametrize(
    "identifier",
    ["Plugin_package", "", "https://cloud.varyzai.com", "asdk_app_", "asdk_app_x/y"],
)
def test_private_hosted_binding_rejects_non_connection_ids(identifier):
    script = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    with pytest.raises(ValueError, match="registered MCP connection ID"):
        script["private_hosted_files"](script["generated"](), identifier)


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_plugin_references_contained_files_and_canonical_skills(client):
    base = ROOT / "plugins" / client
    manifest_folder = ".claude-plugin" if client == "claude-code" else ".codex-plugin"
    manifest = json.loads((base / manifest_folder / "plugin.json").read_text())
    assert manifest["name"] == "varyz"
    mcp = json.loads((base / ".mcp.json").read_text())
    assert mcp["mcpServers"]["varyz"]["command"] == "node"
    assert "env" not in mcp["mcpServers"]["varyz"]
    if client == "codex":
        assert mcp["mcpServers"]["varyz"]["cwd"] == "."
        assert mcp["mcpServers"]["varyz"]["args"] == ["./runtime/launcher.mjs", "codex"]
    for name in ("away", "varyz-context"):
        assert (base / "skills" / name / "SKILL.md").read_bytes() == (
            ROOT / "src/varyz/skills" / name / "SKILL.md"
        ).read_bytes()


def node_run(tmp_path, args, stdin=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for plugin bootstrap verification")
    env = dict(
        os.environ,
        PATH="",
        USERPROFILE=str(tmp_path),
        HOME=str(tmp_path),
        VARYZ_CONFIG=str(tmp_path / "missing.toml"),
        VARYZ_AGENT_TOKEN="secret-canary",
    )
    return subprocess.run(
        [node, str(ROOT / "plugins/claude-code/runtime/launcher.mjs"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_bootstrap_handshakes_without_install_and_leaks_no_secrets(tmp_path, client):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "plugin_status"}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "install", "arguments": {"approve": True}},
        },
    ]
    result = node_run(tmp_path, [client], "\n".join(map(json.dumps, requests)) + "\n")
    assert result.returncode == 0
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert replies[1]["result"]["tools"][0]["name"] == "plugin_status"
    status = json.loads(replies[2]["result"]["content"][0]["text"])
    assert status["capabilities"] == {
        "persistent_preferences": False,
        "archive_retrieval": False,
    }
    assert "cloud" not in status
    assert "error" in replies[-1]
    assert "secret-canary" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


def test_hosted_draft_is_remote_only_and_zip_excludes_unrelated_files(tmp_path):
    script = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    files = {
        tmp_path / path.relative_to(ROOT): content
        for path, content in script["generated"]().items()
    }
    hosted = tmp_path / "plugins/chatgpt"
    hosted.mkdir(parents=True)
    (hosted / "secret.env").write_text("canary")
    script["hosted_zip"].__globals__["ROOT"] = tmp_path
    target = tmp_path / "plugin.zip"
    script["hosted_zip"](files, target)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert names == {
            "README.md",
            "LICENSE",
            "LICENSE.additional-terms",
            "NOTICE",
            "plugin.json",
            "mcp.json",
            ".codex-plugin/plugin.json",
            ".mcp.json",
            "assets/icon.svg",
            "assets/icon.png",
            "skills/varyz-get-started/SKILL.md",
            "skills/varyz-get-started/references/local-setup.md",
            "skills/varyz-get-started/references/cloud-connection.md",
            "skills/varyz-hosted-context/SKILL.md",
            "skills/varyz-hosted-context/references/capabilities.md",
            "skills/varyz-hosted-context/references/listing.md",
            "skills/varyz-cloud-archive/SKILL.md",
        }
        manifest = json.loads(archive.read(".codex-plugin/plugin.json"))
        assert "hooks" not in manifest
        assert "apps" not in manifest
        mcp = json.loads(archive.read(".mcp.json"))["mcpServers"]["varyz"]
        assert mcp == {"url": "https://cloud.varyzai.com/mcp/plugin"}
        portable = json.loads(archive.read("plugin.json"))
        assert portable["extensions"]["com.openai"]["interface"] == manifest["interface"]
        assert len(json.loads(archive.read("mcp.json"))["mcpServers"]) == 1
        assert not any(b"canary" in archive.read(name) for name in names)


def test_bootstrap_bad_json_recovers_and_missing_hook_fails_open(tmp_path):
    result = node_run(tmp_path, ["codex"], 'bad\n[]\n{"id":2,"method":"ping"}\n')
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["error"]["code"] == -32600
    assert replies[2]["result"] == {}
    result = node_run(tmp_path, ["codex", "--hook", "session-start"], "{}")
    assert result.returncode == 0
    assert not result.stdout


def test_registration_does_not_accept_wrong_client_or_invalid_config(tmp_path):
    path = tmp_path / "client.json"
    path.write_text("[]")
    assert plugin.registration("claude-code", path) == {}
    with pytest.raises(ValueError):
        plugin.registration("other", path)


def test_plugin_hook_defers_only_for_matching_existing_event(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": "'C:/archive/hooks/varyz-hook-session-start.cmd'"}]}
                    ]
                }
            }
        )
    )
    assert plugin.global_hook_present("claude-code", "session-start", path)
    assert not plugin.global_hook_present("claude-code", "stop", path)


def migration_environment(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("# fixture")
    client = tmp_path / ".claude.json"
    entry = {
        "command": "varyz",
        "args": ["mcp", "--config", str(config)],
        "env": {"VARYZ_AGENT_TOKEN": "approved-token"},
    }
    data = {
        "unrelated": {"colour": "blue"},
        "mcpServers": {"varyz": entry, "other": {"command": "keep-me"}},
    }
    client.write_text(json.dumps(data))
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"claude-code": client})
    monkeypatch.setattr("varyz.config.load_config", lambda p: SimpleNamespace(db_path="db"))
    monkeypatch.setattr(
        "varyz.db.connection.open_db", lambda p: SimpleNamespace(close=lambda: None)
    )
    verified = []
    monkeypatch.setattr("varyz.trust.caller_from_token", lambda c, t: verified.append(t))
    store = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, n, t: store.update({(s, n): t}))
    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    return config, client, data, store, verified


def test_migration_preserves_credential_and_other_settings_and_is_idempotent(tmp_path, monkeypatch):
    config, client, original, store, verified = migration_environment(tmp_path, monkeypatch)
    before = client.read_bytes()
    assert "Would preserve" in plugin.migrate("claude-code", config, dry_run=True)
    assert client.read_bytes() == before
    assert not store and not verified
    result = plugin.migrate("claude-code", config)
    assert "approved-token" not in result
    assert verified == ["approved-token"]
    original["mcpServers"].pop("varyz")
    assert json.loads(client.read_text()) == original
    assert list(store.values()) == ["approved-token"]
    assert "No standalone" in plugin.migrate("claude-code", config)


def test_failed_keyring_or_wrong_archive_leaves_connection_untouched(tmp_path, monkeypatch):
    config, client, _, _, _ = migration_environment(tmp_path, monkeypatch)
    before = client.read_bytes()
    with pytest.raises(ValueError, match="different archive"):
        plugin.migrate("claude-code", tmp_path / "other.toml")
    monkeypatch.setattr(keyring, "get_password", lambda s, n: None)
    with pytest.raises(ValueError, match="nothing removed"):
        plugin.migrate("claude-code", config)
    assert client.read_bytes() == before


def test_serve_reuses_registration_without_self_grant_or_token_output(
    tmp_path, monkeypatch, capsys
):
    config, _, _, _, _ = migration_environment(tmp_path, monkeypatch)
    monkeypatch.delenv("VARYZ_AGENT_TOKEN", raising=False)
    received = []
    monkeypatch.setattr(
        "varyz.mcp_server.serve",
        lambda **kw: received.append((kw["config_path"], os.environ.get("VARYZ_AGENT_TOKEN"))),
    )
    plugin.serve("claude-code", config)
    assert received == [(config, "approved-token")]
    assert "VARYZ_AGENT_TOKEN" not in os.environ
    assert "approved-token" not in capsys.readouterr().out


def test_serve_after_migration_uses_keyring_and_restores_environment(tmp_path, monkeypatch):
    config, _, _, _, _ = migration_environment(tmp_path, monkeypatch)
    plugin.migrate("claude-code", config)
    monkeypatch.delenv("VARYZ_AGENT_TOKEN", raising=False)
    received = []
    monkeypatch.setattr(
        "varyz.mcp_server.serve", lambda **kw: received.append(os.environ.get("VARYZ_AGENT_TOKEN"))
    )
    plugin.serve("claude-code", config)
    assert received == ["approved-token"]
    assert "VARYZ_AGENT_TOKEN" not in os.environ


def test_plugin_marks_origin_to_suppress_commercial_messages(tmp_path, monkeypatch):
    config, _, _, _, _ = migration_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("VARYZ_PLUGIN_CLIENT", "previous")
    received = []
    monkeypatch.setattr(
        "varyz.mcp_server.serve",
        lambda **kw: received.append(os.environ.get("VARYZ_PLUGIN_CLIENT")),
    )
    plugin.serve("claude-code", config)
    assert received == ["claude-code"]
    assert os.environ["VARYZ_PLUGIN_CLIENT"] == "previous"


def test_invalid_credential_cannot_remove_existing_connection(tmp_path, monkeypatch):
    config, client, _, store, _ = migration_environment(tmp_path, monkeypatch)
    before = client.read_bytes()

    def deny(*args):
        raise ValueError("Revoked agent credential")

    monkeypatch.setattr("varyz.trust.caller_from_token", deny)
    with pytest.raises(ValueError, match="Revoked"):
        plugin.migrate("claude-code", config)
    assert client.read_bytes() == before
    assert not store


def test_codex_failed_removal_restores_config_and_keeps_credential(tmp_path, monkeypatch):
    config, _, _, store, _ = migration_environment(tmp_path, monkeypatch)
    client = tmp_path / "codex.toml"
    client.write_text(f"""colour = "blue"
[mcp_servers.varyz]
command = "varyz"
args = ["mcp", "--config", {json.dumps(str(config))}]
[mcp_servers.varyz.env]
VARYZ_AGENT_TOKEN = "approved-token"
[mcp_servers.other]
command = "keep"
""")
    before = client.read_bytes()
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"codex": client})

    def fail_remove(args):
        assert args == ["mcp", "remove", "varyz"]
        client.write_text("partially changed")
        raise RuntimeError("Client could not remove registration")

    monkeypatch.setattr("varyz.install._run_codex", fail_remove)
    with pytest.raises(RuntimeError, match="could not remove"):
        plugin.migrate("codex", config)
    assert client.read_bytes() == before
    assert list(store.values()) == ["approved-token"]
