"""How far through a source the reading is, so setup's bar can time it.

Each connector that knows what it has to read keeps `read_fraction` while
`discover` runs; the sync hands it to the bar with the item count.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FakeEmbedder
from tests.test_autosync import _project
from varyz import cli
from varyz.config import load_config
from varyz.connectors.ai_conversations import AIConversationsConnector
from varyz.connectors.documents import DocumentsConnector
from varyz.connectors.git_repos import GitConnector


def _fractions(connector, options, cursor=None) -> list[float | None]:
    return [connector.read_fraction for _ in connector.discover(options, cursor)]


def test_documents_count_through_the_files_walked(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md"):
        (notes / name).write_text(f"# {name}\n\nSome words.", encoding="utf-8")
    # Never a document by its name, so not counted as work.
    (notes / "photo.jpg").write_bytes(b"\xff\xd8")
    (notes / "._a.md").write_bytes(b"\x00\x05\x16\x07")

    fractions = _fractions(DocumentsConnector(), {"approved": True, "paths": [str(notes)]})

    assert fractions == [0.0, 0.25, 0.5, 0.75]


def _claude_session(path: Path, turns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "user",
            "sessionId": path.stem,
            "uuid": f"{path.stem}-{number}",
            "timestamp": "2026-09-22T09:00:00.000Z",
            "cwd": "/Users/o/projects/site",
            "message": {"role": "user", "content": f"Keep the archive local, point {number}."},
        }
        for number in range(turns)
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_ai_conversations_weigh_each_file_by_what_is_left_to_read(tmp_path):
    root = tmp_path / ".claude" / "projects"
    first, second = root / "site" / "aaaa.jsonl", root / "site" / "bbbb.jsonl"
    _claude_session(first, 1)
    _claude_session(second, 3)
    options = {"approved": True, "adapters": ["claude_code"], "paths": [str(root)]}
    connector = AIConversationsConnector()

    fractions = _fractions(connector, options)

    share = first.stat().st_size / (first.stat().st_size + second.stat().st_size)
    assert fractions == [0.0, share, share, share]

    # The next run: the first file is read already, so the second is all of it.
    _claude_session(second, 4)
    again = AIConversationsConnector()
    assert _fractions(again, options, connector.checkpoint) == [0.0]


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode,
                    reason="git not on PATH")
def test_git_gives_each_repository_a_share_split_by_its_commits(tmp_path):
    from tests.test_git_connector import make_repo

    make_repo(tmp_path / "one", [("a.txt", "a", "first"), ("b.txt", "b", "second")])
    make_repo(tmp_path / "two", [("c.txt", "c", "only")])

    fractions = _fractions(GitConnector(), {"approved": True, "paths": [str(tmp_path)]})

    assert fractions == [0.0, 0.25, 0.5]


def test_indexing_shows_how_far_through_each_source_it_is(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    heard: list[tuple[str, float | None]] = []
    connector = SimpleNamespace(read_fraction=None)

    def fake_sync(conn, vault, given, cfg, *, on_progress):
        assert given is connector
        given.read_fraction = 0.4
        on_progress(SimpleNamespace(total=100))
        return SimpleNamespace()

    monkeypatch.setattr(cli, "get_connector", lambda cid: connector)
    monkeypatch.setattr(cli, "sync_connector", fake_sync)
    monkeypatch.setattr(cli, "_print_stats", lambda stats: None)
    monkeypatch.setattr(cli, "embed_pending", lambda *a, **kw: 0)
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())

    cli._sync_sources(load_config(config_path), ("documents",),
                      on_progress=lambda line, fraction: heard.append((line, fraction)))

    assert heard[:2] == [("Reading documents", None), ("Reading documents: 100 items", 0.4)]


def test_indexing_reads_sources_before_fetching_search_models(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    heard: list[tuple[str, float | None]] = []

    def fetch(cfg, on_progress):
        assert heard[0] == ("Reading documents", None)
        # Sources must be available for rule extraction while models download.
        assert ("Sources ready", None) in heard
        on_progress("Downloading the search models", 0, 200_000_000)
        on_progress("Downloading the search models", 200_000_000, 200_000_000)
        return False  # the Hub was not reached: the models load the old way

    monkeypatch.setattr(cli, "fetch_search_models", fetch)
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())

    cli._sync_sources(load_config(config_path), ("documents",),
                      on_progress=lambda line, fraction: heard.append((line, fraction)),
                      on_read=lambda: heard.append(("Sources ready", None)))

    downloads = [row for row in heard if row[0].startswith("Downloading the search models:")]
    assert downloads == [
        ("Downloading the search models: 0 bytes of 200.0 MB", 0.0),
        ("Downloading the search models: 200.0 MB of 200.0 MB", 1.0),
    ]
    assert (cli.FETCHING_SEARCH_MODELS, None) in heard
