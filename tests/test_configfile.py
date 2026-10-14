r"""The small TOML edits every connect, reconnect and trust toggle goes through.

These edits are made with regular expressions against the user's own file, so
the file has to survive being edited more than once. It did not: the header
pattern began `^\s*`, which in multiline mode matches at the start of the blank
line above a section and swallows the newline, and everything measured from
that match landed one line early -- the new keys written above the header, into
whatever section came before, and a config no varyz command would load again.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from varyz import filesystem
from varyz.agent_style import read_style
from varyz.configfile import (
    ensure_section,
    remove_connector,
    set_agent_citations,
    set_provider,
    upsert_connector,
)

BASE = '[storage]\ndata_dir = "data"\n'


def test_config_write_retries_transient_windows_lock(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")
    replace = filesystem.os.replace
    attempts = []
    waits = []

    def briefly_locked(source, destination):
        attempts.append((source, destination))
        assert path.read_text(encoding="utf-8") == BASE
        if len(attempts) < 3:
            error = PermissionError("Temporary scanner lock")
            error.winerror = 5
            raise error
        replace(source, destination)

    monkeypatch.setattr(filesystem.os, "replace", briefly_locked)
    monkeypatch.setattr(filesystem.time, "sleep", waits.append)
    set_provider(path, "openai")
    assert len(attempts) == 3
    assert waits == [0.05, 0.1]
    assert _load(path)["llm"]["provider"] == "openai"
    assert not list(tmp_path.glob("*.tmp"))


def test_config_write_persistent_windows_lock_preserves_previous_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")
    attempts = []

    def always_locked(source, destination):
        attempts.append((source, destination))
        error = PermissionError("Persistent lock")
        error.winerror = 5
        raise error

    monkeypatch.setattr(filesystem.os, "replace", always_locked)
    monkeypatch.setattr(filesystem.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="Persistent lock"):
        set_provider(path, "openai")
    assert len(attempts) == 6
    assert path.read_text(encoding="utf-8") == BASE
    assert not list(tmp_path.glob("*.tmp"))


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_a_second_upsert_writes_inside_the_section_not_above_it(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE, encoding="utf-8")

    upsert_connector(cfg, "ai_conversations", {"approved": True, "include_assistant": False})
    upsert_connector(cfg, "ai_conversations", {"approved": True, "include_assistant": True})

    parsed = _load(cfg)
    assert parsed["connectors"]["ai_conversations"]["include_assistant"] is True
    # The section above it is untouched: no stray keys leaked into [storage].
    assert parsed["storage"] == {"data_dir": "data"}
    assert cfg.read_text(encoding="utf-8").count("[connectors.ai_conversations]") == 1


def test_a_key_is_updated_in_place_rather_than_appended_twice(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE, encoding="utf-8")

    upsert_connector(cfg, "git", {"enabled": True})
    upsert_connector(cfg, "git", {"enabled": False})
    upsert_connector(cfg, "git", {"enabled": True})

    assert cfg.read_text(encoding="utf-8").count("enabled =") == 1
    assert _load(cfg)["connectors"]["git"]["enabled"] is True


def test_an_upsert_leaves_a_later_section_and_its_comments_alone(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        BASE + '\n[connectors.git]\n# hand written\nenabled = true\n\n[llm]\nprovider = "claude"\n',
        encoding="utf-8",
    )

    upsert_connector(cfg, "git", {"enabled": False})

    text = cfg.read_text(encoding="utf-8")
    assert "# hand written" in text
    parsed = _load(cfg)
    assert parsed["connectors"]["git"]["enabled"] is False
    assert parsed["llm"]["provider"] == "claude"


def test_ensure_section_sees_a_header_that_follows_a_blank_line(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE + "\n[connectors.telegram]\napproved = true\n", encoding="utf-8")

    assert ensure_section(cfg, "telegram", "[connectors.telegram]\napproved = true\n") is False
    assert cfg.read_text(encoding="utf-8").count("[connectors.telegram]") == 1


def test_set_provider_rewrites_the_one_key_and_keeps_the_file_loadable(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE + '\n[llm]\n\nprovider = "claude"\n', encoding="utf-8")

    set_provider(cfg, "openai")

    parsed = _load(cfg)
    assert parsed["llm"]["provider"] == "openai"
    assert parsed["storage"] == {"data_dir": "data"}
    assert cfg.read_text(encoding="utf-8").count("provider =") == 1


def test_remove_connector_takes_the_section_and_nothing_after_it(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        BASE + '\n[connectors.git]\nenabled = true\n\n[llm]\nprovider = "claude"\n',
        encoding="utf-8",
    )

    remove_connector(cfg, "git")

    parsed = _load(cfg)
    assert "git" not in parsed.get("connectors", {})
    assert parsed["llm"]["provider"] == "claude"


def test_an_update_does_not_reformat_the_lines_around_it(tmp_path):
    """The module promises to rewrite only the lines it owns."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        BASE
        + "\n[connectors.git]\n# why this is on\napproved = true\n\nenabled = true\n"
        + '\n[llm]\nprovider = "claude"\n',
        encoding="utf-8",
    )

    upsert_connector(cfg, "git", {"enabled": False})

    text = cfg.read_text(encoding="utf-8")
    assert "# why this is on" in text
    # The blank line the user put between the two keys is still there.
    assert "approved = true\n\nenabled = false\n" in text
    assert _load(cfg)["llm"]["provider"] == "claude"


def test_a_write_that_dies_halfway_leaves_config_toml_loadable(tmp_path, monkeypatch):
    """A full disk, a crash or a Ctrl-C while one key is being set.

    Pausing a source in the trust centre rewrites the whole file. It used
    to rewrite it in place, so an interrupted pause left config.toml cut
    off mid-section and every later varyz command refusing to start.
    """
    cfg = tmp_path / "config.toml"
    original = (
        '[storage]\ndata_dir = "data"\n\n'
        '[connectors.documents]\nroots = ["D:/notes"]\n'
    )
    cfg.write_text(original, encoding="utf-8")

    real_write = Path.write_text

    def dies_halfway(self, text, *args, **kwargs):
        real_write(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", dies_halfway)
    with pytest.raises(OSError):
        upsert_connector(cfg, "documents", {"paused": True})
    monkeypatch.undo()

    assert cfg.read_text(encoding="utf-8") == original
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["connectors"]["documents"]
    assert not list(tmp_path.glob("*.tmp"))


def test_the_citation_setting_is_written_into_a_new_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")

    set_agent_citations(path, "full")

    assert path.read_text(encoding="utf-8").endswith('[agent]\ncitations = "full"\n')
    assert read_style(path) == "full"


def test_changing_the_setting_keeps_the_rest_of_the_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[agent]\n# mine\ncitations = "full"\nother = 1\n\n[llm]\nprovider = "claude"\n',
        encoding="utf-8",
    )

    set_agent_citations(path, "off")
    text = path.read_text(encoding="utf-8")

    assert '[agent]\n# mine\ncitations = "off"\nother = 1\n' in text
    assert 'provider = "claude"' in text
    assert read_style(path) == "off"


def test_a_citations_line_in_another_section_is_left_alone(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[other]\ncitations = "full"\n', encoding="utf-8")

    set_agent_citations(path, "minimal")
    text = path.read_text(encoding="utf-8")

    assert '[other]\ncitations = "full"\n' in text
    assert read_style(path) == "minimal"
