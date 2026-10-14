"""MCP unregistration: remove only Varyz's key, and only for this archive."""

from __future__ import annotations

from varyz.setup_flow import unregister_mcp


def test_unregister_is_dry_run_safe_and_preserves_other_servers(tmp_path):
    client = tmp_path / "mcp.json"
    original = '{"mcpServers":{"varyz":{"command":"varyz"},"other":{"command":"x"}}}'
    client.write_text(original, encoding="utf-8")
    paths = {"cursor": client}

    preview = unregister_mcp(dry_run=True, paths=paths)

    assert "would remove" in preview[0].detail
    assert client.read_text(encoding="utf-8") == original

    removed = unregister_mcp(paths=paths)
    payload = __import__("json").loads(client.read_text(encoding="utf-8"))
    assert removed[0].changed is True
    assert payload["mcpServers"] == {"other": {"command": "x"}}
    assert client.with_suffix(".json.varyz-uninstall-backup").exists()


def test_unregistering_one_archive_leaves_another_archives_registration_alone(tmp_path):
    """`varyz uninstall` is typed in a directory, and removed a machine.

    A client registration names the archive it starts varyz against. The
    removal did not look at that name, so the command typed in a second
    archive -- or in a directory holding no archive at all -- unregistered the
    archive the reader actually uses, from every client on the machine, with
    no mention of which one it had just taken away.
    """
    import json

    mine = tmp_path / "mine" / "config.toml"
    theirs = tmp_path / "theirs" / "config.toml"
    client = tmp_path / "mcp.json"
    client.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "varyz": {"command": "varyz", "args": ["mcp", "--config", str(theirs)]},
                    "other": {"command": "x"},
                }
            }
        ),
        encoding="utf-8",
    )
    paths = {"cursor": client}

    left = unregister_mcp(paths=paths, archive=mine)

    assert left[0].changed is False
    assert "another archive" in left[0].detail
    assert str(theirs) in left[0].detail
    assert "varyz" in json.loads(client.read_text(encoding="utf-8"))["mcpServers"]

    # Standing in the archive that owns it, the same command removes it -- and
    # a spelling that differs only in case is the same archive on Windows.
    gone = unregister_mcp(paths=paths, archive=theirs)

    assert gone[0].changed is True
    assert json.loads(client.read_text(encoding="utf-8"))["mcpServers"] == {
        "other": {"command": "x"}
    }


def test_a_registration_naming_no_archive_is_still_this_archives_to_remove(tmp_path):
    """An entry written by hand, or by a varyz too old to pin a config, names
    no archive. There is nothing to compare it against, and refusing to touch
    it would leave the one thing `uninstall` exists to remove in place.
    """
    import json

    client = tmp_path / "mcp.json"
    client.write_text(json.dumps({"mcpServers": {"varyz": {"command": "varyz"}}}), encoding="utf-8")

    removed = unregister_mcp(paths={"cursor": client}, archive=tmp_path / "config.toml")

    assert removed[0].changed is True
    assert json.loads(client.read_text(encoding="utf-8"))["mcpServers"] == {}


def test_codex_uninstall_removes_server_and_routing_skill(tmp_path):
    config = tmp_path / "archive" / "config.toml"
    config.parent.mkdir()
    codex = tmp_path / "home" / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        '[mcp_servers.varyz]\ncommand = "varyz"\n'
        f'args = ["mcp", "--config", {__import__("json").dumps(str(config))}]\n',
        encoding="utf-8",
    )
    skill = tmp_path / "home" / ".agents" / "skills" / "varyz-context" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill", encoding="utf-8")
    calls = []

    result = unregister_mcp(
        paths={"codex": codex},
        archive=config,
        codex_runner=calls.append,
    )

    assert result[0].changed is True
    assert calls == [["mcp", "remove", "varyz"]]
    assert not skill.exists()
    assert skill.with_suffix(".md.varyz-uninstall-backup").read_text(encoding="utf-8") == "skill"
