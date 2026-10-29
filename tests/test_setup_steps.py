"""Setup's last steps: the graph, autosync, the classifier and macOS folder access.

The suite's guard replaces these steps everywhere else; here each real one is
run, with fakes for what it would install or start. The graph step is the one
run for real end to end, on a small archive, since it is offline by design.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeEmbedder
from tests.test_autosync import _project
from tests.test_setup import _config
from tests.test_setup import machine as machine  # noqa: F401 - the fixture, reused
from varyz import autosync, cli, mac_app
from varyz.autosync import DoctorCheck
from varyz.config import load_config
from varyz.configfile import set_classifier_url, set_graph_extractor, set_resolution_port
from varyz.llm.local_runtime import extract_archive
from varyz.resolution.ollama_setup import ClassifierSetup, OllamaUnavailable

# The real steps, taken before the suite's guard replaces them.
from varyz.cli import (  # isort: skip
    _build_graph as real_build_graph,
    _mac_folder_access as real_mac_folder_access,
    _open_folder_access as real_open_folder_access,
    _install_autosync as real_install_autosync,
    _ready_classifier as real_ready_classifier,
    _restart_daemon as real_restart_daemon,
)

runner = CliRunner()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# -- the graph -------------------------------------------------------------------------


def _archive(tmp_path, monkeypatch, extractor: str) -> Path:
    # The alpha: the managed model is held back, so the graph is deterministic.
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())
    config = _config(tmp_path / "config.toml")
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[graph]\nextractor = "{extractor}"  # which reader\n')
    assert runner.invoke(cli.app, ["init", "--config", str(config)]).exit_code == 0
    for note in ("I moved to Berlin in 2021.", "I prefer tea to coffee."):
        added = runner.invoke(cli.app, ["add", note, "--config", str(config)])
        assert added.exit_code == 0, added.output
    return config


def test_empty_archive_setup_skips_model_until_data_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())
    config = _config(tmp_path / "config.toml")
    assert runner.invoke(cli.app, ["init", "--config", str(config)]).exit_code == 0

    state, detail = real_build_graph(config)
    assert state == "skipped"
    assert "archive is empty" in detail

    added = runner.invoke(cli.app, ["add", "I prefer tea.", "--config", str(config)])
    assert added.exit_code == 0, added.output
    state, detail = real_build_graph(config)
    assert state == "done", detail
    assert "personal model compiled" in detail


def test_the_graph_is_built_deterministically_and_the_model_compiled(tmp_path, monkeypatch):
    config = _archive(tmp_path, monkeypatch, "contextual_facets")
    heard: list[tuple[str, float | None]] = []

    state, detail = real_build_graph(
        config, on_progress=lambda line, fraction: heard.append((line, fraction))
    )

    assert state == "done", detail
    assert "from 2 entries; personal model compiled" in detail
    assert '[graph] extractor set to "deterministic"' in detail
    assert load_config(config).graph.extractor == "deterministic"
    assert "# which reader" in config.read_text(encoding="utf-8")
    assert ("Building the graph: 1 of 2 entries", 0.5) in heard
    assert ("Building the graph: 2 of 2 entries", 1.0) in heard
    # Compiling names each stage, so a long one is not taken for a hung one,
    # and says how far the saving has got.
    compiling = [item for item in heard if item[0].startswith("Compiling the personal model")]
    assert compiling[0] == ("Compiling the personal model: starting", None)
    assert ("Compiling the personal model: finding conversations", None) in compiling
    assert any(line.startswith("Compiling the personal model: saving 0 of ")
               and fraction == 0.0 for line, fraction in compiling)
    assert ("Compiling the personal model: checking every record, then switching to the "
            "new model", None) in compiling
    assert compiling[-1][0].startswith("Compiling the personal model: checking 0 of ")
    assert compiling[-1][1] == 0.0

    again, detail = real_build_graph(config)
    assert again == "done" and "extractor set" not in detail


def test_a_build_without_the_compiler_builds_only_the_graph(tmp_path, monkeypatch):
    config = _archive(tmp_path, monkeypatch, "deterministic")
    monkeypatch.setattr("varyz.personal_model.compiler_is_shipped", lambda: False)
    heard: list[str] = []

    state, detail = real_build_graph(config, on_progress=lambda line, _f: heard.append(line))

    assert (state, detail) == ("done", "1 claim from 2 entries")
    assert not any(line.startswith("Compiling") for line in heard)


def test_a_build_with_the_model_leaves_the_semantic_graph_to_the_owner(tmp_path, monkeypatch):
    config = _archive(tmp_path, monkeypatch, "contextual_facets")
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: True)

    state, detail = real_build_graph(config)

    assert state == "skipped"
    assert "varyz graph rebuild" in detail and "varyz model compile" in detail
    assert load_config(config).graph.extractor == "contextual_facets"


def test_a_graph_that_cannot_be_built_says_why(tmp_path, monkeypatch):
    config = _archive(tmp_path, monkeypatch, "deterministic")

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("varyz.graph.service.rebuild_graph", broken)

    assert real_build_graph(config) == (
        "failed", "the graph could not be built: database is locked",
    )


@pytest.mark.parametrize("where", ["before", "inside"])
def test_a_compile_out_of_memory_says_what_to_do(tmp_path, monkeypatch, where):
    """It said only "MemoryError: ", whether the compile caught it or not."""
    from varyz.personal_model import episodes

    config = _archive(tmp_path, monkeypatch, "deterministic")

    def full(*args, **kwargs):
        raise MemoryError

    if where == "inside":
        monkeypatch.setattr(episodes.EpisodeReconstructor, "build", full)
    else:
        monkeypatch.setattr(
            "varyz.personal_model.compiler.PersonalModelCompiler.compile", full
        )

    state, detail = real_build_graph(config)

    assert state == "failed"
    assert detail.startswith("the computer ran out of memory")
    assert "close other programs and run setup again" in detail


def test_the_graph_reports_every_entry_read(tmp_path, monkeypatch):
    from varyz.db.connection import open_db
    from varyz.graph.service import rebuild_graph

    config = _archive(tmp_path, monkeypatch, "deterministic")
    cfg = load_config(config)
    heard: list[tuple[int, int]] = []
    conn = open_db(cfg.db_path)
    try:
        rebuild_graph(conn, cfg, on_progress=lambda done, total: heard.append((done, total)))
    finally:
        conn.close()

    assert heard == [(1, 2), (2, 2)]


# -- autosync --------------------------------------------------------------------------


def _checks(*failing: str):
    names = ("config", "database", "connectors", "registry", "macOS privacy", "update")
    return [DoctorCheck(name, name not in failing, f"{name} detail") for name in names]


def test_autosync_is_installed_at_login_on_the_configured_schedule(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    installed = []
    monkeypatch.setattr(autosync, "doctor", lambda path: _checks())
    monkeypatch.setattr(
        autosync, "install_daemon",
        lambda path, *, every_minutes: installed.append((path, every_minutes)) or "plist",
    )

    state, detail = real_install_autosync(config)

    minutes = load_config(config).autosync.schedule_minutes
    assert installed == [(config, minutes)]
    assert state == "done"
    assert detail.startswith(f"syncs every {minutes} minutes and at login")


def test_autosync_waits_for_a_source_and_refuses_a_failed_preflight(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    installed = []
    monkeypatch.setattr(autosync, "install_daemon",
                        lambda path, *, every_minutes: installed.append(path))

    monkeypatch.setattr(autosync, "doctor", lambda path: _checks("connectors"))
    state, detail = real_install_autosync(config)
    assert state == "skipped" and "no data source" in detail

    monkeypatch.setattr(autosync, "doctor", lambda path: _checks("connectors", "registry"))
    assert real_install_autosync(config) == (
        "failed", "connectors: connectors detail; registry: registry detail",
    )
    assert installed == []


def test_a_folder_macos_keeps_from_autosync_does_not_stop_the_install(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    installed = []
    monkeypatch.setattr(autosync, "doctor", lambda path: _checks("macOS privacy"))
    monkeypatch.setattr(autosync, "install_daemon",
                        lambda path, *, every_minutes: installed.append(path))

    assert real_install_autosync(config)[0] == "done"
    assert installed == [config]


def test_a_login_registration_that_fails_is_a_failed_step(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    monkeypatch.setattr(autosync, "doctor", lambda path: _checks())

    def refuse(path, *, every_minutes):
        raise ValueError("launchctl bootstrap failed")

    monkeypatch.setattr(autosync, "install_daemon", refuse)

    assert real_install_autosync(config) == ("failed", "launchctl bootstrap failed")


def test_autosync_compiles_the_personal_model_after_each_new_graph(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg, **_: FakeEmbedder())
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    from varyz.personal_model import compiler

    compiles = []
    real = compiler.build_personal_model_compiler

    def spy(conn, cfg):
        built = real(conn, cfg)
        compiles.append(built)
        return built

    monkeypatch.setattr(compiler, "build_personal_model_compiler", spy)

    first = autosync.run_once(config_path, trigger="test")
    second = autosync.run_once(config_path, trigger="test")

    assert first.graph_status == "updated" and second.graph_status == "throttled"
    assert len(compiles) == 1
    assert "Personal model" not in first.detail


def test_a_build_with_the_model_leaves_compiling_to_the_owner(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg, **_: FakeEmbedder())
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: True)
    monkeypatch.setattr(
        "varyz.personal_model.compiler.build_personal_model_compiler",
        lambda conn, cfg: pytest.fail("compiled in a build with the model"),
    )

    assert autosync.run_once(config_path, trigger="test").graph_status == "updated"


# -- the classifier --------------------------------------------------------------------


def test_the_classifier_step_reports_what_ollama_needed(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    heard: list[tuple[str, float | None]] = []

    def ensure(cfg, cfg_path, on_progress):
        on_progress("Downloading Ollama", 0, 2_000_000_000)
        on_progress("Downloading Ollama", 1_000, 2_000_000_000)  # same percent: quiet
        on_progress("Downloading Ollama", 1_000_000_000, 2_000_000_000)
        on_progress("Unpacking Ollama", 0, 0)
        return ClassifierSetup("installed Ollama 0.34.4; qwen3.5:4b pulled")

    monkeypatch.setattr("varyz.resolution.ollama_setup.ensure_classifier", ensure)

    state, detail, moved = real_ready_classifier(
        config, on_progress=lambda line, fraction: heard.append((line, fraction))
    )

    assert (state, detail, moved) == ("done", "installed Ollama 0.34.4; qwen3.5:4b pulled", False)
    assert [fraction for _, fraction in heard] == [0.0, 0.5, None]
    assert heard[0][0].startswith("Downloading Ollama: ")
    assert heard[2] == ("Unpacking Ollama", None)


def test_a_classifier_that_cannot_be_readied_says_what_answers_instead(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")

    for error in (OllamaUnavailable("install zstd"), httpx.ConnectError("refused")):
        def ensure(cfg, cfg_path, on_progress, error=error):
            raise error

        monkeypatch.setattr("varyz.resolution.ollama_setup.ensure_classifier", ensure)
        state, detail, moved = real_ready_classifier(config)
        assert state == "failed" and not moved
        assert "embedding classifier" in detail


def test_a_moved_classifier_is_reported_as_moved(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    monkeypatch.setattr(
        "varyz.resolution.ollama_setup.ensure_classifier",
        lambda cfg, cfg_path, on_progress: ClassifierSetup("moved", "http://127.0.0.1:11435"),
    )

    assert real_ready_classifier(config) == ("done", "moved", True)


def test_restarting_the_daemon_waits_for_it_to_stop(tmp_path, monkeypatch):
    from varyz.resolution.client import ResolutionClientError

    config = _config(tmp_path / "config.toml")
    events = []

    class Client:
        polls = 0

        def stop(self):
            events.append("stop")

        def status(self):
            Client.polls += 1
            if Client.polls > 2:
                raise ResolutionClientError("down")
            return {}

    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: Client())
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "varyz.resolution.client.ensure_daemon",
        lambda client, path: events.append("start") or SimpleNamespace(port=8765),
    )

    assert real_restart_daemon(config) is True
    assert events == ["stop", "start"] and Client.polls == 3


# -- macOS folder access ---------------------------------------------------------------


@pytest.fixture
def mac(tmp_path, monkeypatch):
    record = tmp_path / "macos_privacy.json"
    documents = tmp_path / "Documents"
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(autosync, "mac_protected_paths", lambda cfg: [documents])
    monkeypatch.setattr(autosync, "mac_protected_sources",
                        lambda cfg: {str(documents): "Documents"})
    monkeypatch.setattr(autosync, "mac_privacy_path", lambda cfg: record)
    return SimpleNamespace(record=record, documents=documents,
                           config=_config(tmp_path / "config.toml"))


def _answer(mac, blocked, program="/Users/o/.varyz/data/autosync/Varyz.app"):
    mac.record.write_text(json.dumps({"blocked": blocked, "program": program}),
                          encoding="utf-8")


def test_nothing_is_asked_where_macos_let_autosync_read(mac):
    since = time.time() - 1
    _answer(mac, [])

    assert real_mac_folder_access(mac.config, since=since, wait=0) is None


def test_a_folder_macos_kept_from_the_app_is_named_by_its_source(mac):
    since = time.time() - 1
    _answer(mac, [str(mac.documents)])

    assert real_mac_folder_access(mac.config, since=since, wait=0) == {
        "folders": [{"source": "Documents", "path": str(mac.documents)}],
        "via": "app", "program": "/Users/o/.varyz/data/autosync/Varyz.app",
    }


def test_without_the_app_the_only_switch_is_full_disk_access(mac):
    since = time.time() - 1
    _answer(mac, [str(mac.documents)], program="/opt/python")

    privacy = real_mac_folder_access(mac.config, since=since, wait=0)
    assert privacy["via"] == "full_disk_access" and privacy["program"] == "/opt/python"


def test_without_a_fresh_answer_nothing_is_claimed(mac):
    """Before the agent has tried, macOS asks for itself; a guess only alarmed."""
    _answer(mac, [str(mac.documents)])
    old = time.time() - 3600
    os.utime(mac.record, (old, old))

    assert real_mac_folder_access(mac.config, since=time.time(), wait=0) is None


def test_folder_access_is_a_macos_question_only(mac, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert real_mac_folder_access(mac.config, since=0, wait=0) is None

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(autosync, "mac_protected_paths", lambda cfg: [])
    assert real_mac_folder_access(mac.config, since=0, wait=0) is None


def test_settings_opens_where_the_permission_lives(monkeypatch):
    opened: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda command, **kw: opened.append(command))

    real_open_folder_access({"via": "app", "program": "/x/Varyz.app"})
    assert opened == [["open", cli.FILES_AND_FOLDERS_URL]]

    opened.clear()
    real_open_folder_access({"via": "full_disk_access", "program": "/opt/python"})
    assert opened == [["open", cli.FULL_DISK_ACCESS_URL], ["open", "-R", "/opt/python"]]


def test_autosync_says_ahead_what_macos_will_ask(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    config = _config(tmp_path / "config.toml")
    cfg = load_config(config)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.setattr(autosync, "mac_protected_paths", lambda cfg: [home / "Documents" / "a"])

    assert cli._mac_ask_ahead(config) == ""  # no app: macOS will not ask
    app = autosync.autosync_dir(cfg) / "Varyz.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": mac_app.APP_VERSION}))
    (autosync.autosync_dir(cfg) / mac_app.LAUNCHER_NAME).write_text("", encoding="utf-8")

    assert "macOS may ask whether Varyz can access your Documents: choose Allow" in (
        cli._mac_ask_ahead(config)
    )


# -- progress --------------------------------------------------------------------------


def test_indexing_reports_each_source_then_how_far_embedding_has_got(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    (tmp_path / "notes" / "trip.md").write_text("# Porto\n\nPack light.", encoding="utf-8")
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())
    heard: list[tuple[str, float | None]] = []

    failed = cli._sync_sources(
        load_config(config_path), ("documents",),
        on_progress=lambda line, fraction: heard.append((line, fraction)),
    )

    assert failed == []
    assert heard[0] == ("Reading documents", None)
    loading = heard.index((cli.LOADING_SEARCH_MODELS, None))
    embedding = heard[loading + 1:]
    assert embedding[0] == ("Embedding locally: 0 of 2 chunks", 0.0)
    assert embedding[-1] == ("Embedding locally: 2 of 2 chunks", 1.0)


def test_embedding_reports_after_each_batch(tmp_path, monkeypatch):
    from varyz.db.connection import open_db
    from varyz.pipeline.embed import embed_pending

    config = _project(tmp_path)
    (tmp_path / "notes" / "trip.md").write_text("# Porto\n\nPack light.", encoding="utf-8")
    synced = runner.invoke(cli.app, ["sync", "--no-embed", "--config", str(config)])
    assert synced.exit_code == 0, synced.output
    cfg = load_config(config)
    heard: list[tuple[int, int]] = []

    conn = open_db(cfg.db_path)
    try:
        embedded = embed_pending(conn, cfg, FakeEmbedder(), batch_size=1,
                                 on_progress=lambda done, total: heard.append((done, total)))
    finally:
        conn.close()

    assert embedded == 2
    assert heard == [(0, 2), (1, 2), (2, 2)]


def test_indexing_numbers_the_sources_when_there_are_several(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    heard: list[str] = []

    def fake_sync(conn, vault, connector, cfg, *, on_progress):
        on_progress(SimpleNamespace(total=7))
        return SimpleNamespace()

    monkeypatch.setattr(cli, "get_connector", lambda cid: cid)
    monkeypatch.setattr(cli, "sync_connector", fake_sync)
    monkeypatch.setattr(cli, "_print_stats", lambda stats: None)
    monkeypatch.setattr(cli, "embed_pending", lambda *a, **kw: 0)
    monkeypatch.setattr(cli, "Embedder", lambda _cfg, **_: FakeEmbedder())

    cli._sync_sources(load_config(config_path), ("git", "documents"),
                      on_progress=lambda line, fraction: heard.append(line))

    assert heard[:4] == [
        "Reading git (1 of 2 sources)", "Reading git (1 of 2 sources): 7 items",
        "Reading documents (2 of 2 sources)", "Reading documents (2 of 2 sources): 7 items",
    ]


def test_time_left_waits_for_a_pace_then_follows_it():
    now = [0.0]
    left = cli.TimeLeft(clock=lambda: now[0])

    assert left("Embedding locally: 0 of 100 chunks", 0.0) == ""
    now[0] = 2.0
    assert left("Embedding locally: 10 of 100 chunks", 0.1) == ""  # too soon to tell
    now[0] = 90.0
    assert left("Embedding locally: 50 of 100 chunks", 0.5) == "about 2 min left"
    now[0] = 178.0
    assert left("Embedding locally: 99 of 100 chunks", 0.99) == "under a minute left"
    assert left("Embedding locally: 100 of 100 chunks", 1.0) == ""


def test_time_left_follows_the_recent_pace_when_the_run_slows():
    now = [0.0]
    left = cli.TimeLeft(clock=lambda: now[0])
    left("Embedding locally: 0 of 1000 chunks", 0.0)
    # 10 chunks a second for 60 s, then 2 a second, as a laptop that warms up.
    for second in range(1, 61):
        now[0] = float(second)
        left(f"Embedding locally: {second * 10} of 1000 chunks", second / 100)
    for second in range(61, 121):
        now[0] = float(second)
        said = left(f"Embedding locally: {600 + (second - 60) * 2} of 1000 chunks",
                    (600 + (second - 60) * 2) / 1000)
    # 280 chunks left at 2 a second: 140 s. The whole run's pace would say 47 s.
    assert said == "about 3 min left"


def test_recent_seconds_left_drops_samples_before_its_window():
    samples = [(float(t), float(t * 10)) for t in range(0, 101)]

    assert cli.recent_seconds_left(samples, 100, recent=20, share=0.2) == pytest.approx(10)
    assert samples[0] == (0.0, 0.0) and len(samples) == 22  # the first, then the last 21 s
    assert cli.recent_seconds_left(samples[:1], 100, recent=20, share=0.2) is None


@pytest.mark.parametrize("line, fraction", [
    ("Pulling qwen3.5:4b: 1 MB of 3 GB", 0.0),  # another phase
    ("Embedding locally: 5 of 100 chunks", 0.05),  # went back: a new pass
    ("Loading the local search models", None),  # no end known
])
def test_time_left_starts_again_where_the_old_pace_says_nothing(line, fraction):
    now = [0.0]
    left = cli.TimeLeft(clock=lambda: now[0])
    left("Embedding locally: 0 of 100 chunks", 0.0)
    now[0] = 30.0
    assert left("Embedding locally: 50 of 100 chunks", 0.5)

    assert left(line, fraction) == ""
    now[0] = 31.0
    assert left("Embedding locally: 60 of 100 chunks", 0.6) == ""


@pytest.mark.parametrize("seconds, label", [
    (0, "under a minute left"), (59, "under a minute left"), (60, "about 1 min left"),
    (61, "about 2 min left"), (3540, "about 59 min left"), (3600, "about 1 h left"),
    (5400, "about 1 h 30 min left"),
])
def test_time_left_reads_in_minutes_and_hours(seconds, label):
    assert cli.time_left_label(seconds) == label


def test_a_setup_bar_is_numbered_and_says_how_long_it_has_left(monkeypatch):
    monkeypatch.setattr("rich.progress.Progress", _Bar)
    now = [0.0]
    real = cli.TimeLeft
    monkeypatch.setattr(cli, "TimeLeft", lambda: real(clock=lambda: now[0]))

    with cli._progress_bar("Building the graph", step=5) as bar:
        bar("Building the graph: 0 of 10 entries", 0.0)
        now[0] = 60.0
        bar("Building the graph: 5 of 10 entries", 0.5)

    assert _Bar.made.events[0] == ("add", 1, "5/7 Building the graph", None)
    assert _Bar.made.events[-2] == (
        "update", 1, {"description": "5/7 Building the graph: 5 of 10 entries · about 1 min left",
                      "total": 1.0, "completed": 0.5})


class _Bar:
    """rich's Progress, as far as the setup bar uses it."""

    def __init__(self, *columns, **options):
        self.events: list[tuple] = []
        self.next_id = 0
        _Bar.made = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append(("closed",))

    def add_task(self, description, total):
        self.next_id += 1
        self.events.append(("add", self.next_id, description, total))
        return self.next_id

    def remove_task(self, task):
        self.events.append(("remove", task))

    def update(self, task, **fields):
        self.events.append(("update", task, fields))


def test_the_terminal_bar_fills_when_a_step_can_say_and_sweeps_when_it_cannot(monkeypatch):
    monkeypatch.setattr("rich.progress.Progress", _Bar)

    with cli._progress_bar("Indexing") as bar:
        bar("Reading git")
        bar("Embedding locally: 1 of 4 chunks", 0.25)
        bar("Embedding locally: 4 of 4 chunks", 1.0)
        bar("Compiling [the] model")

    assert _Bar.made.events == [
        ("add", 1, "Indexing", None),
        ("update", 1, {"description": "Reading git"}),
        ("update", 1, {"description": "Embedding locally: 1 of 4 chunks", "total": 1.0,
                       "completed": 0.25}),
        ("update", 1, {"description": "Embedding locally: 4 of 4 chunks", "total": 1.0,
                       "completed": 1.0}),
        # A total cannot be taken back in rich, so the sweep is a new task.
        ("remove", 1),
        ("add", 2, r"Compiling \[the] model", None),
        ("update", 2, {"description": r"Compiling \[the] model"}),
        ("closed",),
    ]


def test_a_download_is_reported_once_a_percent_in_bytes():
    heard: list[tuple[str, float | None]] = []
    report = cli._bytes_progress(lambda line, fraction: heard.append((line, fraction)))

    for done in (0, 1_000, 2_000_000, 2_000_001, 200_000_000):
        report("Downloading Ollama", done, 200_000_000)
    report("Downloading Ollama", 5, 0)  # a size nobody gave

    assert heard == [
        ("Downloading Ollama: 0 bytes of 200.0 MB", 0.0),
        ("Downloading Ollama: 2.0 MB of 200.0 MB", 0.01),
        ("Downloading Ollama: 200.0 MB of 200.0 MB", 1.0),
        ("Downloading Ollama", None),
    ]
    cli._bytes_progress(None)("Downloading Ollama", 1, 2)  # nobody listening


def test_ctrl_c_pauses_a_download_and_still_stops_anything_else(monkeypatch):
    monkeypatch.setattr("rich.progress.Progress", _Bar)

    paused = pytest.raises(cli.DownloadPaused, match="Downloading Ollama: 1.0 MB")
    with paused, cli._progress_bar("Setting up") as bar:
        bar("Downloading Ollama: 1.0 MB of 200.0 MB", 0.005)
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt), cli._progress_bar("Setting up") as bar:
        bar("Downloading Ollama: 200.0 MB of 200.0 MB", 1.0)
        bar("Unpacking Ollama")
        raise KeyboardInterrupt


def test_a_paused_download_says_how_to_carry_on(monkeypatch, capsys):
    def paused(*args, **kwargs):
        raise cli.DownloadPaused("Downloading Ollama: 1.0 MB of 200.0 MB")

    monkeypatch.setattr(cli, "app", paused)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 130
    assert "run the same command again to carry on" in capsys.readouterr().out


def test_the_page_hears_the_running_steps_progress_and_only_its(tmp_path):
    from varyz.bootstrap import DetectedSources
    from varyz.setup_web import SetupSession

    session = SetupSession(_config(tmp_path / "config.toml"), detected=DetectedSources())

    session._set(4, "running")
    session._progress("Embedding locally: 3 of 4 chunks", 0.75)
    assert (session.snapshot()["activity"], session.snapshot()["fraction"]) == (
        "Embedding locally: 3 of 4 chunks", 0.75,
    )
    session._progress("Downloading", 1.2)
    assert session.snapshot()["fraction"] == 1.0

    session._set(4, "done", "indexed")
    session._set(5, "running")
    assert (session.snapshot()["activity"], session.snapshot()["fraction"]) == ("", None)
    session._progress("Compiling the personal model")
    assert session.snapshot()["fraction"] is None


def test_the_page_has_a_bar_for_every_long_step_and_the_wait_after_them():
    from varyz.setup_web import render_page

    page = render_page("tok").decode("utf-8")

    for name in ("Data sources", "Graph", "Autosync", "Classifier", "Setup"):
        assert f'role="progressbar" aria-label="{name} progress"' in page
    assert "prefers-reduced-motion" in page and "@keyframes sweep" in page


# -- the terminal ----------------------------------------------------------------------


def _finishing(monkeypatch, *, graph=("done", "12 claims from 2 entries"),
               autosync_step=("done", "syncs every 30 minutes and at login"),
               classifier=("done", "qwen3.5:4b ready", False), privacy=None):
    calls = []
    monkeypatch.setattr(cli, "_build_graph",
                        lambda path, on_progress=None: calls.append("graph") or graph)
    monkeypatch.setattr(cli, "_install_autosync",
                        lambda path: calls.append("autosync") or autosync_step)
    monkeypatch.setattr(cli, "_ready_classifier",
                        lambda path, on_progress=None: calls.append("classifier") or classifier)
    monkeypatch.setattr(cli, "_mac_folder_access",
                        lambda path, **kw: calls.append("privacy") or privacy)
    monkeypatch.setattr(cli, "_restart_daemon", lambda path: calls.append("restart") or True)
    return calls


def test_terminal_setup_ends_with_the_graph_autosync_and_the_classifier(
    tmp_path, machine, monkeypatch  # noqa: F811 - the fixture
):
    config = _config(tmp_path / "config.toml")
    calls = _finishing(monkeypatch)

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources",
                                    "--ai-mode", "local"])

    assert result.exit_code == 0, result.output
    assert calls == ["restart", "graph", "autosync", "classifier", "privacy"]
    flat = _flat(result.output)
    assert "1/7 Archive" in flat
    assert "5/7 Graph 12 claims from 2 entries" in flat
    assert "6/7 Autosync syncs every 30 minutes and at login" in flat
    assert "7/7 Classifier qwen3.5:4b ready" in flat
    assert "NO-GO" not in flat


def test_terminal_setup_says_a_warm_computer_is_normal_only_when_it_reads(
    tmp_path, machine, monkeypatch  # noqa: F811 - the fixture
):
    from varyz.setup_run import WARM_NOTE

    _finishing(monkeypatch)

    def setup(name: str, *extra: str) -> str:
        (tmp_path / name).mkdir()
        config = _config(tmp_path / name / "config.toml")
        result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes",
                                        "--ai-mode", "local", *extra])
        assert result.exit_code == 0, result.output
        return _flat(result.output)

    assert WARM_NOTE not in setup("nothing", "--no-sources")
    monkeypatch.setattr("varyz.bootstrap.approved_local_connectors", lambda _path: ("git",))
    monkeypatch.setattr(cli, "_sync_sources", lambda *_a, **_k: [])
    flat = setup("git")
    assert WARM_NOTE in flat
    assert flat.index(WARM_NOTE) < flat.index("4/7 Data sources")


def test_a_build_without_the_graph_builder_has_no_graph_step(
    tmp_path, machine, monkeypatch  # noqa: F811 - the fixture
):
    """The alpha holds the builder back: setup has six steps and builds no graph."""
    monkeypatch.setattr("varyz.graph.builder_is_shipped", lambda: False)
    config = _config(tmp_path / "config.toml")
    calls = _finishing(monkeypatch, classifier=("failed", "Ollama did not start", False))

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources",
                                    "--ai-mode", "local"])

    assert result.exit_code == 1, result.output
    assert "graph" not in calls
    flat = _flat(result.output)
    assert "1/6 Archive" in flat
    assert "5/6 Autosync syncs every 30 minutes and at login" in flat
    assert "6/6 Classifier failed: Ollama did not start" in flat
    # A step label, not a bare "/7": a temp folder named 71731ea7... holds that too.
    assert "Graph" not in flat and not re.search(r"\b\d/7 [A-Z]", flat)


def test_a_failed_last_step_is_a_no_go(tmp_path, machine, monkeypatch):  # noqa: F811
    config = _config(tmp_path / "config.toml")
    calls = _finishing(monkeypatch, classifier=("failed", "Ollama did not start", False),
                       autosync_step=("skipped", "no data source to keep current"))

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources",
                                    "--ai-mode", "local"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "7/7 Classifier failed: Ollama did not start" in flat
    assert "6/7 Autosync skipped: no data source to keep current" in flat
    assert "NO-GO: Not ready: classifier." in flat
    # No autosync, so nothing to ask macOS about.
    assert "privacy" not in calls


def test_a_moved_classifier_restarts_the_daemon_and_asks_for_an_agent_restart(
    tmp_path, machine, monkeypatch  # noqa: F811
):
    config = _config(tmp_path / "config.toml")
    from varyz.install import InstallResult

    agent = tmp_path / ".claude.json"
    agent.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"claude-code": agent})
    monkeypatch.setattr("varyz.install.connect_agent", lambda *args, **kwargs:
                        (None, [InstallResult("claude-code", agent, False, "already connected")]))
    calls = _finishing(monkeypatch, classifier=("done", "serves on port 11435", True))

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources", "--yes",
                                    "--ai-mode", "local"])

    assert result.exit_code == 0, result.output
    assert "restart" in calls
    assert "Restart Claude Code so it loads Varyz." in _flat(result.output)


def test_what_macos_keeps_from_autosync_is_printed_with_the_way_to_allow_it(
    tmp_path, machine, monkeypatch  # noqa: F811
):
    config = _config(tmp_path / "config.toml")
    folders = [{"source": "Documents", "path": "/Users/o/Documents"}]
    _finishing(monkeypatch, privacy={"folders": folders, "via": "app",
                                     "program": "/o/Varyz.app"})

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources",
                                    "--ai-mode", "local"])

    flat = _flat(result.output)
    assert "background syncing skips them: Documents /Users/o/Documents" in flat
    assert "If macOS asked, choose Allow" in flat and "That opens these folders only" in flat
    assert cli.FILES_AND_FOLDERS_URL in flat and "Full Disk Access" not in flat
    assert "run `varyz sync` yourself" in flat


def test_without_the_app_full_disk_access_is_explained_not_just_asked(
    tmp_path, machine, monkeypatch  # noqa: F811
):
    config = _config(tmp_path / "config.toml")
    folders = [{"source": "Documents", "path": "/Users/o/Documents"}]
    _finishing(monkeypatch, privacy={"folders": folders, "via": "full_disk_access",
                                     "program": "/opt/python"})

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources",
                                    "--ai-mode", "local"])

    flat = _flat(result.output)
    assert "no folder-only switch" in flat and "opens more than these folders" in flat
    assert "add /opt/python to System Settings" in flat
    assert cli.FULL_DISK_ACCESS_URL in flat


def test_a_dry_run_says_what_the_last_steps_would_do(tmp_path, machine, monkeypatch):  # noqa: F811
    config = _config(tmp_path / "config.toml")
    calls = _finishing(monkeypatch)

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls == []
    flat = _flat(result.output)
    assert "5/7 Graph would build the graph" in flat
    assert "6/7 Autosync would sync in the background" in flat
    assert "7/7 Classifier would ready" in flat


# -- the config edits and the unpacker ---------------------------------------------------


def test_config_setters_keep_comments_and_add_what_is_missing(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[resolution]\nport = 8765  # this archive's\n\n"
        '[graph]\nextractor = "contextual_facets"  # reader\n',
        encoding="utf-8",
    )

    set_resolution_port(config, 8799)
    set_graph_extractor(config, "deterministic")
    set_classifier_url(config, "http://127.0.0.1:11435")

    text = config.read_text(encoding="utf-8")
    assert "port = 8799  # this archive's" in text
    assert 'extractor = "deterministic"  # reader' in text
    assert text.endswith('[workflows]\nclassifier_url = "http://127.0.0.1:11435"\n')
    cfg = load_config(config)
    assert cfg.resolution.port == 8799
    assert cfg.workflows.classifier_url == "http://127.0.0.1:11435"


def test_extract_archive_reads_gzip_and_plain_tar(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "ollama").write_bytes(b"#!")
    (source / "lib.dylib").write_bytes(b"")
    for name, mode in (("a.tgz", "w:gz"), ("a.tar", "w")):
        archive = tmp_path / name
        with tarfile.open(archive, mode) as packed:
            for path in source.iterdir():
                packed.add(path, arcname=path.name)
        destination = tmp_path / f"out-{name}"
        destination.mkdir()

        extract_archive(archive, destination)

        assert (destination / "ollama").read_bytes() == b"#!"
        assert (destination / "lib.dylib").exists()
