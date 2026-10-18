"""Ollama for the local classifier: found, installed without a prompt, started.

Nothing here downloads, installs or starts anything: every program, server
and download is a fake, and the one real thing unpacked is a small archive
built in tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from varyz.config import load_config
from varyz.resolution import ollama_setup
from varyz.resolution.local_classifier import ClassifierUnavailable, LocalModel
from varyz.resolution.ollama_setup import (
    MIN_VERSION,
    OLLAMA_VERSION,
    OllamaUnavailable,
    binary_version,
    endpoint,
    ensure_classifier,
    has_model,
    install,
    managed_executable,
    ollama_home,
    platform_asset,
    pull,
    record_server,
    server_version,
    wait_until_up,
)

# The real one, taken before the suite's guard replaces it.
from varyz.resolution.ollama_setup import start_recorded as real_start_recorded

OLD = (0, 11, 11)
NEW = (0, 34, 4)


def _cfg(url: str = "http://localhost:11434", model: str = "qwen3.5:4b"):
    return SimpleNamespace(workflows=SimpleNamespace(classifier_url=url, classifier_model=model))


class _Machine:
    """Fakes for everything ensure_classifier touches, and what each was asked."""

    def __init__(self, *, running=None, found=(), installed_starts=None, pulled=False):
        self.running = running
        self.found = list(found)
        self.installed_starts = installed_starts
        self.pulled = pulled
        self.calls: list[tuple] = []

    def probe(self, base_url):
        self.calls.append(("probe", base_url))
        return self.running

    def version_of(self, path):
        return {Path("/new/ollama"): NEW, Path("/old/ollama"): OLD}.get(path)

    def find(self):
        return self.found

    def installer(self, on_progress):
        self.calls.append(("install",))
        if self.installed_starts is not None:
            self.running = self.installed_starts
        return Path("/managed/ollama")

    def start(self, executable, host, port):
        self.calls.append(("start", executable, host, port))

    def wait(self, base_url):
        self.calls.append(("wait", base_url))
        return NEW

    def is_pulled(self, base_url, model):
        self.calls.append(("pulled?", base_url, model))
        return self.pulled

    def pull(self, base_url, model, on_progress):
        self.calls.append(("pull", base_url, model))

    def run(self, cfg, cfg_path, **overrides):
        fakes = dict(
            probe=self.probe, version_of=self.version_of, find=self.find,
            installer=self.installer, start=self.start, wait=self.wait,
            pulled=self.is_pulled, puller=self.pull,
        )
        return ensure_classifier(cfg, cfg_path, None, **(fakes | overrides))

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


def test_a_new_enough_ollama_with_the_model_is_left_as_it_is(tmp_path):
    machine = _Machine(running=NEW, pulled=True)

    result = machine.run(_cfg(), tmp_path / "config.toml")

    assert result.detail == "qwen3.5:4b ready" and result.moved_to is None
    assert machine.named("install") == machine.named("start") == machine.named("pull") == []


def test_a_running_ollama_without_the_model_pulls_it(tmp_path):
    machine = _Machine(running=NEW, pulled=False)

    result = machine.run(_cfg(), tmp_path / "config.toml")

    assert machine.named("pull") == [("pull", "http://127.0.0.1:11434", "qwen3.5:4b")]
    assert result.detail == "qwen3.5:4b pulled"


def test_a_stopped_ollama_already_installed_is_started_not_installed(tmp_path):
    machine = _Machine(running=None, found=[Path("/old/ollama"), Path("/new/ollama")])

    result = machine.run(_cfg(), tmp_path / "config.toml")

    assert machine.named("install") == []
    assert machine.named("start") == [("start", Path("/new/ollama"), "127.0.0.1", 11434)]
    assert json.loads((ollama_home() / "server.json").read_text()) == {
        "executable": str(Path("/new/ollama")), "host": "127.0.0.1", "port": 11434,
    }
    assert result.moved_to is None


def test_no_ollama_installs_the_pinned_one_and_starts_it(tmp_path):
    machine = _Machine(running=None, found=[Path("/old/ollama")])

    result = machine.run(_cfg(), tmp_path / "config.toml")

    assert machine.named("install") == [("install",)]
    assert machine.named("start") == [("start", Path("/managed/ollama"), "127.0.0.1", 11434)]
    assert result.detail == f"installed Ollama {OLLAMA_VERSION}; qwen3.5:4b pulled"


def test_an_installer_that_starts_ollama_itself_is_not_started_again(tmp_path):
    # Windows: the installer upgrades in place and starts the app.
    machine = _Machine(running=None, installed_starts=NEW, pulled=True)

    machine.run(_cfg(), tmp_path / "config.toml")

    assert machine.named("start") == []


def test_an_older_ollama_on_the_port_keeps_it_and_the_new_one_serves_beside(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.toml"
    config.write_text('[workflows]\nclassifier_model = "qwen3.5:4b"  # the sorter\n',
                      encoding="utf-8")
    monkeypatch.setattr(ollama_setup, "_free_port", lambda host, first: 11437)
    machine = _Machine(running=OLD, found=[Path("/new/ollama")], pulled=True)

    result = machine.run(_cfg(), config)

    assert result.moved_to == "http://127.0.0.1:11437"
    assert machine.named("start") == [("start", Path("/new/ollama"), "127.0.0.1", 11437)]
    assert machine.named("pulled?") == [("pulled?", "http://127.0.0.1:11437", "qwen3.5:4b")]
    assert "Ollama 0.11.11 is too old for qwen3.5:4b" in result.detail
    workflows = load_config(config).workflows
    assert workflows.classifier_url == "http://127.0.0.1:11437"
    assert "# the sorter" in config.read_text(encoding="utf-8")


def test_an_ollama_that_does_not_start_names_its_log(tmp_path):
    machine = _Machine(running=None, found=[Path("/new/ollama")])

    with pytest.raises(OllamaUnavailable, match=r"did not start on .*serve\.log"):
        machine.run(_cfg(), tmp_path / "config.toml", wait=lambda base_url: None)
    assert not (ollama_home() / "server.json").exists()


def test_a_server_that_cannot_list_its_models_is_unavailable(tmp_path):
    machine = _Machine(running=NEW)

    def broken(base_url, model):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaUnavailable, match="did not list its models"):
        machine.run(_cfg(), tmp_path / "config.toml", pulled=broken)


def test_endpoint_reads_the_loopback_forms():
    assert endpoint("http://localhost:11434") == ("127.0.0.1", 11434, "http://127.0.0.1:11434")
    assert endpoint("http://127.0.0.1") == ("127.0.0.1", 11434, "http://127.0.0.1:11434")
    assert endpoint("http://[::1]:11500") == ("::1", 11500, "http://[::1]:11500")


def test_versions_come_from_the_program_and_the_server():
    def run(output, stderr=""):
        return lambda *a, **k: SimpleNamespace(stdout=output, stderr=stderr)

    assert binary_version(Path("x"), run=run("ollama version is 0.34.4\n")) == NEW
    # With an older server up, the program names its own version in a warning.
    both = "ollama version is 0.11.11\nWarning: client version is 0.34.4\n"
    assert binary_version(Path("x"), run=run(both)) == NEW
    assert binary_version(Path("x"), run=run("")) is None

    def missing(*a, **k):
        raise FileNotFoundError("x")

    assert binary_version(Path("x"), run=missing) is None

    def get(url, timeout):
        assert url == "http://127.0.0.1:11434/api/version" and timeout == 2.0
        return httpx.Response(200, json={"version": "0.17.5"}, request=httpx.Request("GET", url))

    assert server_version("http://127.0.0.1:11434", get=get) == MIN_VERSION

    def down(url, timeout):
        raise httpx.ConnectError("refused")

    assert server_version("http://127.0.0.1:11434", get=down) is None


def test_wait_until_up_gives_up_at_its_deadline():
    now = [0.0]
    probes = []

    def probe(base_url):
        probes.append(now[0])
        return None

    def sleep(seconds):
        now[0] += seconds

    assert wait_until_up("u", timeout=2.0, probe=probe, sleep=sleep, clock=lambda: now[0]) is None
    assert probes == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_has_model_matches_the_classifiers_names():
    def get(url, timeout):
        return httpx.Response(
            200, json={"models": [{"name": "qwen3.5:4b"}]}, request=httpx.Request("GET", url)
        )

    assert has_model("u", "qwen3.5:4b", get=get)
    assert has_model("u", "qwen3.5", get=get)
    assert not has_model("u", "qwen3.5:9b", get=get)


def _stream(lines, status=200):
    @contextmanager
    def stream(method, url, **kwargs):
        stream.asked = (method, url, kwargs["json"], kwargs["timeout"])
        response = httpx.Response(
            status, content="\n".join(lines).encode(), request=httpx.Request(method, url)
        )
        yield response

    return stream


def test_pull_reports_bytes_and_needs_success():
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "pulling abc", "total": 100, "completed": 40}),
        "",
        json.dumps({"status": "pulling abc", "total": 100, "completed": 100}),
        json.dumps({"status": "success"}),
    ]
    stream = _stream(lines)
    heard = []

    pull("http://127.0.0.1:11434", "qwen3.5:4b", lambda *event: heard.append(event),
         stream=stream)

    method, url, body, timeout = stream.asked
    assert (method, url, body) == (
        "POST", "http://127.0.0.1:11434/api/pull", {"model": "qwen3.5:4b", "stream": True},
    )
    assert timeout.read == 300.0 and timeout.connect == 10.0
    assert heard == [("Downloading qwen3.5:4b", 40, 100), ("Downloading qwen3.5:4b", 100, 100)]


def test_a_pull_that_errors_or_stops_short_is_unavailable():
    with pytest.raises(OllamaUnavailable, match="no space left"):
        pull("u", "m", stream=_stream([json.dumps({"error": "no space left on device"})]))
    with pytest.raises(OllamaUnavailable, match="ended at 'pulling abc'"):
        pull("u", "m", stream=_stream([json.dumps({"status": "pulling abc"})]))
    with pytest.raises(OllamaUnavailable, match="could not pull m"):
        pull("u", "m", stream=_stream([], status=500))


# -- install -----------------------------------------------------------------------


def _fake_download(members: dict[str, bytes], asked: list):
    def download(url, destination, sha256, size, phase, on_progress):
        asked.append((url, sha256, size, phase))
        with tarfile.open(destination, "w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))

    return download


@pytest.fixture
def roomy(monkeypatch):
    monkeypatch.setattr("varyz.diskspace.free_bytes", lambda path: 10**12)


def test_macos_unpacks_the_standalone_build_under_the_varyz_home(roomy):
    asked: list = []

    program = install(system="Darwin", machine="arm64",
                      download=_fake_download({"ollama": b"#!", "libx.dylib": b""}, asked))

    asset = platform_asset("darwin")
    assert asked == [(asset.url, asset.sha256, asset.size, "Downloading Ollama")]
    assert asset.url.endswith(f"v{OLLAMA_VERSION}/ollama-darwin.tgz")
    assert program == managed_executable("darwin") == ollama_home() / OLLAMA_VERSION / "ollama"
    assert program.read_bytes() == b"#!" and os.access(program, os.X_OK)
    assert not list((ollama_home() / "downloads").iterdir())
    assert not (ollama_home() / f".{OLLAMA_VERSION}.partial").exists()


def test_an_archive_without_the_program_is_not_an_install(roomy):
    with pytest.raises(OllamaUnavailable, match="left no program"):
        install(system="Darwin", download=_fake_download({"README": b""}, []))


def test_too_little_disk_is_said_before_downloading(monkeypatch):
    monkeypatch.setattr("varyz.diskspace.free_bytes", lambda path: 100 * 1024**2)
    asked: list = []

    with pytest.raises(OllamaUnavailable, match="Ollama needs .* free"):
        install(system="Darwin", download=_fake_download({}, asked))
    assert asked == []


def test_linux_without_zstd_says_what_to_install(roomy, monkeypatch):
    monkeypatch.setattr(ollama_setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(ollama_setup.sys, "version_info", (3, 12, 0))

    with pytest.raises(OllamaUnavailable, match="install zstd"):
        install(system="Linux", machine="x86_64", download=_fake_download({}, []))


def test_linux_unpacks_with_the_zstd_tool(roomy, monkeypatch):
    monkeypatch.setattr(ollama_setup.shutil, "which", lambda name: "/usr/bin/zstd")
    commands = []

    def download(url, destination, sha256, size, phase, on_progress):
        assert destination.name == "ollama-linux-arm64.tar.zst"
        destination.write_bytes(b"zstd bytes")

    def run(command, **kwargs):
        commands.append(command)
        tar = Path(command[command.index("-o") + 1])
        # As the release: bin/ollama beside lib/ollama.
        with tarfile.open(tar, "w") as archive:
            for name, data in (("bin/ollama", b"#!"), ("lib/ollama/libggml.so", b"")):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o755
                archive.addfile(info, io.BytesIO(data))
        return SimpleNamespace(returncode=0, stderr="")

    program = install(system="Linux", machine="aarch64", run=run, download=download)

    assert program == ollama_home() / OLLAMA_VERSION / "bin" / "ollama" and program.is_file()
    assert commands[0][:5] == ["/usr/bin/zstd", "-d", "-q", "-f", "-o"]
    assert not list((ollama_home() / "downloads").iterdir())


def test_windows_runs_the_official_installer_silently(roomy, monkeypatch, tmp_path):
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    launched = []

    def download(url, destination, sha256, size, phase, on_progress):
        assert url.endswith("/OllamaSetup.exe")
        destination.write_bytes(b"MZ")

    class Installer:
        def __init__(self, command, **kwargs):
            launched.append(command)
            # The app would start without its welcome window.
            assert (local / "Ollama" / "upgraded").exists()
            program = local / "Programs" / "Ollama" / "ollama.exe"
            program.parent.mkdir(parents=True)
            program.write_bytes(b"MZ")

        def wait(self, timeout):
            assert timeout == 15 * 60.0
            return 0

    program = install(system="Windows", machine="AMD64", popen=Installer, download=download)

    assert program == local / "Programs" / "Ollama" / "ollama.exe"
    assert launched[0][1:] == ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]


def test_a_failed_windows_installer_says_its_exit_code(roomy, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def download(url, destination, *rest):
        destination.write_bytes(b"MZ")

    installer = SimpleNamespace(wait=lambda timeout: 1603)

    with pytest.raises(OllamaUnavailable, match="exit code 1603"):
        install(system="Windows", popen=lambda *a, **k: installer, download=download)


# -- starting the recorded server ------------------------------------------------------


def _program(tmp_path) -> Path:
    program = tmp_path / "ollama"
    program.write_bytes(b"#!")
    return program


def test_serve_starts_detached_on_the_classifiers_address(tmp_path):
    started = []

    ollama_setup.serve(_program(tmp_path), "::1", 11500,
                       popen=lambda command, **kw: started.append((command, kw)))

    command, kwargs = started[0]
    assert command == [str(tmp_path / "ollama"), "serve"]
    assert kwargs["env"]["OLLAMA_HOST"] == "[::1]:11500"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert Path(kwargs["stdout"].name) == ollama_home() / "serve.log"


def test_the_recorded_server_starts_again_once_a_minute_at_most(tmp_path):
    started = []

    def popen(command, **kwargs):
        started.append(command)

    assert real_start_recorded("127.0.0.1", 11434, popen=popen) is False  # nothing recorded
    record_server(_program(tmp_path), "127.0.0.1", 11434)

    assert real_start_recorded("127.0.0.1", 11435, popen=popen) is False  # another address
    assert real_start_recorded("127.0.0.1", 11434, popen=popen) is True
    assert real_start_recorded("127.0.0.1", 11434, popen=popen) is False  # just tried
    attempt = ollama_home() / "server.started"
    earlier = time.time() - 61
    os.utime(attempt, (earlier, earlier))
    assert real_start_recorded("127.0.0.1", 11434, popen=popen) is True
    assert len(started) == 2


def test_a_recorded_program_that_is_gone_is_not_started(tmp_path):
    program = _program(tmp_path)
    record_server(program, "127.0.0.1", 11434)
    program.unlink()

    assert real_start_recorded("127.0.0.1", 11434, popen=lambda *a, **k: None) is False


def test_the_classifier_starts_a_stopped_ollama_for_the_next_question(monkeypatch):
    asked = []

    def start(host, port):
        asked.append((host, port))
        return True

    monkeypatch.setattr(ollama_setup, "start_recorded", start)
    monkeypatch.setattr(
        "varyz.resolution.local_classifier.socket.create_connection",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    model = LocalModel("qwen3.5:4b", url="http://localhost:11500")

    with pytest.raises(ClassifierUnavailable, match="starting for the next question"):
        model.ready()
    assert asked == [("127.0.0.1", 11500)]

    monkeypatch.setattr(ollama_setup, "start_recorded", lambda host, port: False)
    with pytest.raises(ClassifierUnavailable, match="Ollama is not running"):
        model.ready()
