from pathlib import Path

import pytest

from varyz.config import ConfigError, find_config_file, load_config


def test_home_env_alias_and_explicit_config_share_one_archive(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    canonical = project / "config.toml"
    canonical.write_text('[storage]\ndata_dir="data"\n[resolution]\nport=1207\n',
                         encoding="utf-8")
    alias = tmp_path / "home.toml"
    alias.write_text('redirect_config="project/config.toml"\n', encoding="utf-8")
    monkeypatch.setenv("VARYZ_CONFIG", str(alias))
    cfg = load_config()
    assert find_config_file() == canonical
    assert cfg.source_file == canonical
    assert cfg.data_dir == project / "data"
    assert cfg.resolution.port == 1207
    assert load_config(alias).db_path == load_config(canonical).db_path


@pytest.mark.parametrize("target", ['"home.toml"', '"missing.toml"', '42'])
def test_broken_alias_does_not_silently_select_another_archive(tmp_path, target):
    alias = tmp_path / "home.toml"
    alias.write_text(f"redirect_config={target}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(alias)


def test_explicit_unredirected_config_still_overrides_env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    monkeypatch.setenv("VARYZ_CONFIG", str(tmp_path / "other.toml"))
    assert find_config_file(path) == path
    assert find_config_file(Path("not-created.toml")) == Path("not-created.toml")
