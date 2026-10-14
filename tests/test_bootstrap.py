from pathlib import Path

import pytest

from tests.conftest import FakeEmbedder
from varyz.bootstrap import (
    DetectedSources,
    approve_detected_sources,
    approved_local_connectors,
    discover_local_sources,
    missing_detected_sources,
    sync_local_sources,
)
from varyz.cli import DEFAULT_CONFIG
from varyz.config import BUILTIN_SOURCE_DEFAULTS, load_config
from varyz.db.connection import open_db
from varyz.domain import Sensitivity
from varyz.graph.repository import GraphRepository
from varyz.retrieval.search import hybrid_search


def _config(path: Path) -> Path:
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    return path


def test_discovery_offers_documents_and_conventional_git_roots(tmp_path):
    home = tmp_path / "home"
    documents = home / "Documents"
    repo = home / "projects" / "one"
    documents.mkdir(parents=True)
    (repo / ".git").mkdir(parents=True)

    found = discover_local_sources(home=home, cwd=home, documents_dir=documents)

    assert found.document_paths == (documents.resolve(),)
    assert found.git_paths == ((home / "projects").resolve(),)
    assert home.resolve() not in found.document_paths


def test_a_checkout_inside_the_projects_folder_is_offered_once(tmp_path):
    home = tmp_path / "home"
    documents = home / "Documents"
    checkout = documents / "projects" / "varyz"
    documents.mkdir(parents=True)
    (checkout / ".git").mkdir(parents=True)

    found = discover_local_sources(home=home, cwd=checkout, documents_dir=documents)

    assert found.git_paths == ((documents / "projects").resolve(),)


def test_a_checkout_deeper_than_the_scan_is_still_offered(tmp_path):
    home = tmp_path / "home"
    documents = home / "Documents"
    checkout = documents / "projects" / "a" / "b" / "c" / "deep"
    documents.mkdir(parents=True)
    (documents / "projects" / "one" / ".git").mkdir(parents=True)
    (checkout / ".git").mkdir(parents=True)

    found = discover_local_sources(home=home, cwd=checkout, documents_dir=documents)

    assert checkout.resolve() in found.git_paths
    assert (documents / "projects").resolve() in found.git_paths


def test_discovery_expands_home_in_the_xdg_documents_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    documents = home / "My Documents"
    documents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", "$HOME/My Documents")

    found = discover_local_sources(home=home, cwd=home)

    assert found.document_paths == (documents.resolve(),)


def test_checkout_outside_documents_is_a_narrow_source_for_both_connectors(tmp_path):
    home = tmp_path / "home"
    checkout = tmp_path / "work" / "project"
    (home / "Documents").mkdir(parents=True)
    (checkout / ".git").mkdir(parents=True)

    found = discover_local_sources(home=home, cwd=checkout)

    assert checkout.resolve() in found.document_paths
    assert checkout.resolve() in found.git_paths


def test_discovery_never_offers_home_as_a_document_root(tmp_path):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)

    found = discover_local_sources(home=home, cwd=home, documents_dir=home / "missing")

    assert found.document_paths == ()


def test_approval_writes_exact_local_paths_and_is_idempotent(tmp_path):
    config = _config(tmp_path / "config.toml")
    docs = tmp_path / "Docs with spaces"
    repos = tmp_path / "repos"
    docs.mkdir()
    repos.mkdir()
    detected = DetectedSources((docs.resolve(),), (repos.resolve(),))

    assert approve_detected_sources(config, detected) == ("documents", "git")
    first = config.read_text(encoding="utf-8")
    assert approve_detected_sources(config, detected) == ()
    assert config.read_text(encoding="utf-8") == first

    cfg = load_config(config)
    assert cfg.connector_options("documents") == {
        "approved": True,
        "paths": [str(docs.resolve())],
    }
    assert cfg.connector_options("git") == {
        "approved": True,
        "paths": [str(repos.resolve())],
    }
    assert approved_local_connectors(config) == ("documents", "git")


def test_approval_ignores_commented_connector_examples_in_packaged_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    docs = tmp_path / "Documents"
    docs.mkdir()

    assert approve_detected_sources(
        config, DetectedSources(document_paths=(docs.resolve(),))
    ) == ("documents",)

    cfg = load_config(config)
    assert cfg.connector_options("documents") == {
        "approved": True,
        "paths": [str(docs.resolve())],
    }
    # A packaged config that named one source used to replace the whole
    # mapping, downgrading WhatsApp, Telegram, Instagram, contacts and browser
    # history to cloud-eligible on every installed copy.
    assert cfg.sensitivity.source_defaults == BUILTIN_SOURCE_DEFAULTS
    assert cfg.sensitivity.source_defaults["whatsapp"] == Sensitivity.SENSITIVE
    assert cfg.sensitivity.rules.patterns, "no content escalation in a packaged config"


def test_existing_connector_is_never_overwritten_by_generic_consent(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\ndata_dir = "data"\n\n'
        '[connectors.documents]\napproved = false\npaths = ["chosen"]\n',
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")
    offered = DetectedSources((tmp_path.resolve(),), ())

    assert missing_detected_sources(config, offered).document_paths == ()
    assert approve_detected_sources(config, offered) == ()
    assert config.read_text(encoding="utf-8") == before
    assert approved_local_connectors(config) == ()


def test_local_sync_refuses_any_cloud_or_unknown_connector(tmp_path):
    config = _config(tmp_path / "config.toml")
    with pytest.raises(ValueError, match="non-local.*gmail"):
        sync_local_sources(config, ("gmail",))
    assert not (tmp_path / "data").exists()


def test_approved_first_run_indexes_evidence_and_is_idempotent(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "decision.md").write_text(
        "The zephyr protocol was chosen for the launch checklist.", encoding="utf-8"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\ndata_dir = "data"\n\n'
        '[embedding]\nmodel = "fake-embedder-384"\n\n'
        '[graph]\nextractor = "deterministic"\n\n'
        f'[connectors.documents]\napproved = true\npaths = ["{docs.as_posix()}"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    open_db(cfg.db_path).close()  # the installer's preceding `varyz init`
    monkeypatch.setattr("varyz.bootstrap.Embedder", lambda _cfg: FakeEmbedder())

    first = sync_local_sources(config, ("documents",))
    second = sync_local_sources(config, ("documents",))

    assert first.stats[0].new == 1
    assert first.embedded > 0
    assert first.graph is not None
    assert first.graph.run_id
    assert first.graph_error is None
    assert second.stats[0].new == 0
    assert second.stats[0].unchanged == 1
    assert second.embedded == 0
    assert second.graph is not None
    assert second.graph.no_op is True
    conn = open_db(cfg.db_path)
    try:
        hits = hybrid_search(conn, "zephyr protocol", FakeEmbedder())
        active_graph = GraphRepository(conn).active_run()
    finally:
        conn.close()
    assert hits
    assert active_graph is not None
    assert "zephyr protocol" in hits[0].entry.body.lower()
