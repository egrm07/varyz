from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from varyz.cli import app
from varyz.config import Config, LocalModelConfig, StorageConfig
from varyz.llm.llamacpp_local import LlamaCppProvider
from varyz.llm.local import build_local_provider
from varyz.llm.local_runtime import (
    HostMemory,
    ManagedLlamaRuntime,
    RuntimeAsset,
    companion_asset,
    host_memory,
    resolve_variant,
    runtime_asset,
)
from varyz.llm.refusal import ReplyTruncated


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _config(tmp_path: Path, **settings) -> Config:
    return Config(
        storage=StorageConfig(data_dir=tmp_path / "data"),
        local_model=LocalModelConfig(**settings),
        base_dir=tmp_path,
    )


def _serving_config(tmp_path: Path, **settings) -> Config:
    """A config for the tests that build a serving command, with the floor off.

    `_start_server` refuses before it builds anything when the machine has less
    physical memory free than `host_memory_floor_mib`, which is the right
    behaviour and the wrong dependency for a test about command arguments: run
    these while a graph rebuild holds ten gigabytes and four tests that have
    nothing to do with memory fail on the real machine's free page count. The
    floor has its own two tests, which stub the reading rather than read it.
    """

    settings.setdefault("host_memory_floor_mib", 0)
    return _config(tmp_path, **settings)


def test_runtime_assets_are_pinned_for_shipping_platforms():
    windows = runtime_asset(
        system="Windows", machine="AMD64", variant="auto", nvidia=False
    )
    mac = runtime_asset(system="Darwin", machine="arm64", variant="auto")
    linux = runtime_asset(system="Linux", machine="x86_64", variant="cpu")

    assert windows.name.endswith("win-vulkan-x64.zip")
    assert mac.name.endswith("macos-arm64.tar.gz")
    assert linux.name.endswith("ubuntu-x64.tar.gz")
    assert all(len(value.sha256) == 64 for value in (windows, mac, linux))


def test_an_nvidia_machine_gets_the_cuda_build_and_its_runtime_libraries():
    asset = runtime_asset(system="Windows", machine="AMD64", variant="auto", nvidia=True)
    companion = companion_asset(
        system="Windows", machine="AMD64", variant="auto", nvidia=True
    )

    assert asset.name.endswith("win-cuda-13.3-x64.zip")
    # llama-server.exe cannot start without the CUDA runtime DLLs, so the
    # companion is not optional on this backend.
    assert companion is not None
    assert companion.name.startswith("cudart-")
    assert len(companion.sha256) == 64


def test_the_half_gigabyte_cuda_download_is_skipped_without_an_nvidia_gpu():
    assert (
        companion_asset(system="Windows", machine="AMD64", variant="auto", nvidia=False)
        is None
    )
    assert resolve_variant("auto", "Windows", "x86_64", nvidia=False) == "vulkan"
    assert resolve_variant("auto", "Windows", "x86_64", nvidia=True) == "cuda"
    # An explicit choice is never overridden by what the machine happens to have.
    assert resolve_variant("vulkan", "Windows", "x86_64", nvidia=True) == "vulkan"


def test_backends_install_into_separate_directories(tmp_path, monkeypatch):
    """A Vulkan install and a CUDA install must not overwrite each other.

    Both are Windows builds: Linux ships no CUDA build and macOS one backend.
    """
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(local_runtime.platform, "machine", lambda: "AMD64")
    vulkan = ManagedLlamaRuntime(_config(tmp_path, runtime_variant="vulkan"))
    cuda = ManagedLlamaRuntime(_config(tmp_path, runtime_variant="cuda"))

    assert vulkan.runtime_dir != cuda.runtime_dir


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


class FakeLog:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _running_server(last_used: float):
    from varyz.llm.local_runtime import _RunningServer

    return _RunningServer(
        process=FakeProcess(),
        base_url="http://127.0.0.1:1",
        api_key="k",
        log_handle=FakeLog(),
        log_path=Path("server.log"),
        last_used=last_used,
    )


def test_an_idle_server_gives_its_vram_back(monkeypatch):
    """7.4 GB stayed pinned between fifteen-minute cycles for no reason."""
    import time

    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    now = time.monotonic()
    idle = _running_server(now - 600)
    local_runtime._SERVERS[Path("idle")] = idle

    local_runtime._unload_if_idle(180.0)

    assert idle.process.terminated
    assert idle.log_handle.closed
    assert not local_runtime._SERVERS


def test_a_server_answering_requests_is_not_unloaded_under_it(monkeypatch):
    import time

    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_SERVERS", {})
    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)
    armed: list[float] = []
    monkeypatch.setattr(local_runtime, "_schedule_idle_unload", armed.append)
    busy = _running_server(time.monotonic())
    local_runtime._SERVERS[Path("busy")] = busy

    local_runtime._unload_if_idle(180.0)

    assert not busy.process.terminated
    assert local_runtime._SERVERS
    # The deadline measures from the last request, so a live server re-arms.
    assert armed == [180.0]


def test_a_zero_idle_setting_keeps_the_model_resident(monkeypatch):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_IDLE_TIMER", None)

    local_runtime._schedule_idle_unload(0)

    assert local_runtime._IDLE_TIMER is None


def test_custom_model_path_needs_no_managed_download(tmp_path):
    model = tmp_path / "existing.gguf"
    model.write_bytes(b"gguf")
    runtime = ManagedLlamaRuntime(_config(tmp_path, model_path=model))
    runtime.executable.parent.mkdir(parents=True)
    runtime.executable.write_bytes(b"server")

    assert runtime.runtime_available() is True
    assert runtime.model_available() is True
    assert runtime.status().detail == "ready; starts headlessly on demand"


def test_runtime_archive_rejects_parent_traversal(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("../outside.exe", b"bad")

    with pytest.raises(RuntimeError, match="unsafe path"):
        ManagedLlamaRuntime._extract(archive, tmp_path / "stage")
    assert not (tmp_path / "outside.exe").exists()


def test_runtime_install_verifies_layout_and_writes_receipt(tmp_path, monkeypatch):
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr(runtime.executable.name, b"server")
        value.writestr("dependency.dll", b"dll")
    payload = archive.read_bytes()
    runtime.asset = RuntimeAsset(
        archive.name,
        "https://example.invalid/runtime.zip",
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )

    def fake_download(_url, destination, *_args):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(runtime, "_download", fake_download)

    executable = runtime.install_runtime()

    assert executable.read_bytes() == b"server"
    assert (runtime.runtime_dir / "INSTALL.json").is_file()


def _tarball(path: Path, members: list[tuple[str, bytes | str]]) -> None:
    """A tar.gz laid out like llama.cpp's macOS and Linux releases.

    A str payload is a symlink to that name; bytes are a regular file.
    """
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as archive:
        folder = tarfile.TarInfo("llama-b10221")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o755
        archive.addfile(folder)
        for name, payload in members:
            info = tarfile.TarInfo(f"llama-b10221/{name}")
            if isinstance(payload, str):
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                archive.addfile(info)
            else:
                info.size = len(payload)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(payload))


def test_a_mac_release_tarball_installs_with_its_library_links(tmp_path):
    """Its top-level folder is lifted and its versioned-library links kept.

    Measured against the b10221 release workflow on 2026-09-25: the macOS and
    Linux tarballs wrap build/bin in `llama-b10221/`, and the libraries carry
    SOVERSION 0, so the server loads `libllama.0.dylib` through a link. Before
    this, both made every macOS and Linux install fail.
    """
    archive = tmp_path / "llama-b10221-bin-macos-arm64.tar.gz"
    _tarball(
        archive,
        [
            ("llama-server", b"server"),
            ("libllama.0.0.10221.dylib", b"library"),
            ("libllama.0.dylib", "libllama.0.0.10221.dylib"),
            ("libllama.dylib", "libllama.0.dylib"),
        ],
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    ManagedLlamaRuntime._extract(archive, stage)
    assert (stage / "llama-server").read_bytes() == b"server"
    # Where a link cannot be made (Windows without the privilege), tarfile copies.
    assert (stage / "libllama.0.dylib").read_bytes() == b"library"
    assert (stage / "libllama.dylib").read_bytes() == b"library"
    assert not (stage / "llama-b10221").exists()


@pytest.mark.parametrize("target", ["../outside", "/etc/passwd", "lib/other.dylib"])
def test_a_link_that_reaches_out_of_the_folder_is_refused(tmp_path, target):
    archive = tmp_path / "runtime.tar.gz"
    _tarball(archive, [("llama-server", b"server"), ("libllama.dylib", target)])
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(RuntimeError, match="unsupported link"):
        ManagedLlamaRuntime._extract(archive, stage)


def test_managed_install_adopts_exact_existing_blob_without_network(
    tmp_path, monkeypatch
):
    payload = b"exact model"
    digest = hashlib.sha256(payload).hexdigest()
    home = tmp_path / "home"
    legacy = home / ".ollama" / "models" / "blobs" / f"sha256-{digest}"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(payload)
    config = _config(
        tmp_path,
        model="fixture",
        model_url="https://example.invalid/model",
        model_sha256=digest,
        model_size_bytes=len(payload),
    )
    runtime = ManagedLlamaRuntime(config)
    monkeypatch.setattr("varyz.llm.local_runtime.Path.home", lambda: home)
    monkeypatch.setattr(
        runtime,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("exact legacy weights must be reused"),
    )

    assert runtime.install_model().read_bytes() == payload
    assert runtime.model_receipt.read_text(encoding="ascii").strip() == digest


def test_llamacpp_provider_sends_schema_constrained_request(monkeypatch):
    captured = {}
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            model="fixture.gguf",
            request_timeout_seconds=321,
        ),
        endpoint=lambda: ("http://127.0.0.1:9876", "secret"),
        available=lambda: True,
        status=lambda: SimpleNamespace(detail="ready"),
    )

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return FakeResponse(
            {
                "choices": [{"message": {"content": '{"facets":[]}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            }
        )

    monkeypatch.setattr("varyz.llm.llamacpp_local.httpx.post", fake_post)
    provider = LlamaCppProvider(
        runtime,
        max_tokens=50,
        format_schema={"type": "object"},
    )

    assert provider.complete("system", "user") == '{"facets":[]}'
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    # The schema must be nested under "json_schema". A schema placed beside the
    # type is accepted by the server and then ignored, which is indistinguishable
    # from sending no schema at all: the reply is unconstrained and fails its
    # parse later, somewhere else.
    assert captured["payload"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "varyz_response",
            "strict": True,
            "schema": {"type": "object"},
        },
    }
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert captured["timeout"] == 321


def test_local_provider_factory_keeps_ollama_as_explicit_compatibility(tmp_path):
    managed = build_local_provider(_config(tmp_path))
    ollama = build_local_provider(_config(tmp_path, backend="ollama"))

    assert isinstance(managed, LlamaCppProvider)
    assert type(ollama).__name__ == "OllamaProvider"


def test_interactive_local_provider_clamps_compile_tuning_without_mutating_it(tmp_path):
    cfg = _config(tmp_path, context_window=16_384, parallel_slots=4)

    provider = build_local_provider(cfg, conservative_resources=True)

    assert provider.runtime.settings.context_window == 8192
    assert provider.runtime.settings.parallel_slots == 1
    assert cfg.local_model.context_window == 16_384
    assert cfg.local_model.parallel_slots == 4


def test_an_install_that_cannot_fit_is_refused_before_the_first_byte(tmp_path, monkeypatch):
    """Seven gigabytes were downloaded onto whatever space happened to be free.

    On a disk that could not hold the model the download ran until the write
    failed -- partway through a progress bar, having filled the disk the
    archive itself lives on. Nothing in the package had ever asked.
    """
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: SimpleNamespace(total=1, used=1, free=4096)
    )
    monkeypatch.setattr(
        runtime, "_download", lambda *_a: pytest.fail("downloaded onto a full disk")
    )

    with pytest.raises(RuntimeError) as caught:
        runtime.install()

    assert "not enough free space" in str(caught.value)
    assert "GB" in str(caught.value)


def test_a_resumed_install_only_counts_the_bytes_it_still_has_to_fetch(tmp_path):
    """A half-finished .part is space already paid for.

    Counting the whole download again would refuse an install with room for
    everything it still has left to do.
    """
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    whole = runtime.bytes_still_needed()
    runtime.model_path.parent.mkdir(parents=True, exist_ok=True)
    part = runtime.model_path.with_suffix(runtime.model_path.suffix + ".part")
    part.write_bytes(b"0" * 8192)

    assert runtime.bytes_still_needed() == whole - 8192


def test_local_model_install_dry_run_lists_exact_downloads_without_writes(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["local-model", "install", "--dry-run", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    assert "llama-b10221" in result.output
    assert "gemma4:12b-it-q4_K_M" in result.output
    assert "no network requests or file writes" in result.output
    assert not (tmp_path / "data").exists()


def _fixture_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(model="fixture.gguf", request_timeout_seconds=5),
        endpoint=lambda: ("http://127.0.0.1:9876", "secret"),
        available=lambda: True,
        status=lambda: SimpleNamespace(detail="ready"),
    )


def test_a_reply_cut_at_the_token_cap_says_so(monkeypatch):
    """Truncation and a model that cannot follow a schema are different faults.

    Both arrive as invalid JSON, and every layer above reported them the same
    way -- so a window that simply needed more room read as a model that had
    stopped obeying its grammar.  The server distinguishes them; this used to
    throw the distinction away.
    """

    def fake_post(url, *, headers, json, timeout):
        return FakeResponse(
            {
                "choices": [
                    {"message": {"content": '{"projects":[{"na'}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 2000, "completion_tokens": 4096},
            }
        )

    monkeypatch.setattr("varyz.llm.llamacpp_local.httpx.post", fake_post)
    provider = LlamaCppProvider(_fixture_runtime(), max_tokens=4096)

    with pytest.raises(ReplyTruncated) as caught:
        provider.complete("system", "user")

    assert caught.value.produced == 4096
    assert caught.value.cap == 4096
    assert caught.value.prompt == 2000
    # Counts, not the fragment: the reply is the archive coming back out of a
    # model, and this message is logged for the whole of a run.
    assert str(caught.value) == (
        "the reply was cut at its token cap after 4096 of 4096 tokens, "
        "behind a prompt of 2000"
    )


def test_a_reply_that_ran_out_of_slot_is_not_blamed_on_its_cap(monkeypatch):
    """Which number to change depends on which room ran out.

    A reply that stops at its cap wants a larger cap.  A reply that stops well
    under it has hit the slot it shares with its prompt, and no cap buys a
    token back -- on 2026-08-31 a candidate window stopped at 5,561 of 8,192
    with a 10,968-token prompt in a 16,384-token slot, and the fix was to send
    fewer observations, not to ask for more room.
    """

    def fake_post(url, *, headers, json, timeout):
        return FakeResponse(
            {
                "choices": [{"message": {"content": "{"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10968, "completion_tokens": 5561},
            }
        )

    monkeypatch.setattr("varyz.llm.llamacpp_local.httpx.post", fake_post)

    with pytest.raises(ReplyTruncated) as caught:
        LlamaCppProvider(_fixture_runtime(), max_tokens=8192).complete("s", "u")

    assert "the room left in its slot" in str(caught.value)
    assert "behind a prompt of 10968" in str(caught.value)


def test_a_reply_that_finished_on_its_own_still_comes_back(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return FakeResponse(
            {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            }
        )

    monkeypatch.setattr("varyz.llm.llamacpp_local.httpx.post", fake_post)

    assert LlamaCppProvider(_fixture_runtime()).complete("system", "user") == "{}"


def test_a_truncated_window_is_halved_rather_than_dropped():
    """The recovery that halves an unreadable window has to see this one too.

    A reply that ran out of room is the case halving was written for: half the
    window is half the reply, and it fits.
    """

    from varyz.personal_model.semantic_runtime import _UNREADABLE_REPLY

    assert issubclass(ReplyTruncated, _UNREADABLE_REPLY)


def test_adoption_asks_a_busy_server_more_than_once_before_declaring_it_stale(
    tmp_path, monkeypatch
):
    """One missed second used to end every remaining call in the process.

    A server decoding on all its slots can be late to a health probe. Adoption
    treated late as gone, refused to start a second server, and raised -- and
    because it left the lease exactly as it found it, the next caller read the
    same lease and failed the same way. Twelve synthesis windows in a row died
    on it with the model sitting there answering.
    """
    from varyz.llm import local_runtime

    runtime = ManagedLlamaRuntime(_config(tmp_path))
    endpoint = local_runtime.ManagedEndpoint("http://127.0.0.1:1", "k", 4321, "t")
    attempts = []

    def flaky_get(url, headers=None, timeout=None):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise TimeoutError("busy")
        return FakeResponse({})

    monkeypatch.setattr(local_runtime.httpx, "get", flaky_get)
    monkeypatch.setattr(local_runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(local_runtime.time, "sleep", lambda seconds: None)

    assert runtime._answers_health(endpoint)
    assert len(attempts) == 3


def test_a_server_that_has_exited_is_not_waited_on(tmp_path, monkeypatch):
    """Patience is for a busy server, not a dead one."""
    from varyz.llm import local_runtime

    runtime = ManagedLlamaRuntime(_config(tmp_path))
    endpoint = local_runtime.ManagedEndpoint("http://127.0.0.1:1", "k", 4321, "t")

    def refused(url, headers=None, timeout=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(local_runtime.httpx, "get", refused)
    monkeypatch.setattr(local_runtime, "_process_alive", lambda pid: False)

    assert not runtime._answers_health(endpoint)


def test_the_card_can_be_told_how_many_layers_to_hold(tmp_path, monkeypatch):
    """`auto` decides from the memory free when it loads, which on a machine
    whose desktop is also on the card is the wrong number to decide from."""
    from varyz.llm import local_runtime

    captured: list[list[str]] = []

    real = local_runtime.subprocess.Popen

    def refuse(command, **kwargs):
        # Variant detection shells out to nvidia-smi through the same name.
        if "--gpu-layers" not in command:
            return real(command, **kwargs)
        captured.append(list(command))
        raise RuntimeError("not started")

    monkeypatch.setattr(local_runtime.subprocess, "Popen", refuse)
    runtime = ManagedLlamaRuntime(_serving_config(tmp_path, gpu_layers=40))
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError):
        runtime._start_server()

    command = captured[0]
    assert command[command.index("--gpu-layers") + 1] == "40"


def test_the_server_is_started_with_unified_memory_and_says_so(monkeypatch, tmp_path):
    """Two slots on this card die on the second slot without it.

    The variable is the difference between a synthesis stage that finishes and
    one that loses every concept to a dead server, so it belongs to the
    settings rather than to whoever typed the command -- and a server already
    running without it must read as a different server, because it will die.
    """
    from varyz.llm import local_runtime

    captured = []
    real = local_runtime.subprocess.Popen

    def refuse(command, **kwargs):
        if "--gpu-layers" not in command:
            return real(command, **kwargs)
        captured.append(dict(kwargs.get("env") or {}))
        raise RuntimeError("not started")

    monkeypatch.setattr(local_runtime.subprocess, "Popen", refuse)
    monkeypatch.setenv("GGML_CUDA_ENABLE_UNIFIED_MEMORY", "1")
    off = ManagedLlamaRuntime(_serving_config(tmp_path, cuda_unified_memory=False))
    off.runtime_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError):
        off._start_server()

    # Off means off, even where the surrounding environment already set it:
    # a shape that says the server has no unified memory has to be true.
    assert "GGML_CUDA_ENABLE_UNIFIED_MEMORY" not in captured[0]
    assert "uvm=0" in off._serving_shape()

    on = ManagedLlamaRuntime(_serving_config(tmp_path))
    with pytest.raises(RuntimeError):
        on._start_server()
    assert captured[1]["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] == "1"
    assert "uvm=1" in on._serving_shape()
    assert on._serving_shape() != off._serving_shape()

def test_a_prompt_cache_of_zero_is_turned_off(monkeypatch, tmp_path):
    """0 is off, as it is for llama.cpp, not the server's 8 GB default."""
    from varyz.llm import local_runtime

    captured = []
    real = local_runtime.subprocess.Popen

    def refuse(command, **kwargs):
        if "--gpu-layers" not in command:
            return real(command, **kwargs)
        captured.append(command)
        raise RuntimeError("not started")

    monkeypatch.setattr(local_runtime.subprocess, "Popen", refuse)
    for cram in (0, 4096):
        runtime = ManagedLlamaRuntime(_serving_config(tmp_path, prompt_cache_ram_mib=cram))
        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError):
            runtime._start_server()
    assert [c[c.index("--cache-ram") + 1] for c in captured] == ["0", "4096"]


def test_an_idle_server_unloads_the_model_by_itself(monkeypatch, tmp_path):
    """The server sleeps after the configured idle time; 0 never sleeps."""
    from varyz.llm import local_runtime

    captured = []
    real = local_runtime.subprocess.Popen

    def refuse(command, **kwargs):
        if "--gpu-layers" not in command:
            return real(command, **kwargs)
        captured.append(command)
        raise RuntimeError("not started")

    monkeypatch.setattr(local_runtime.subprocess, "Popen", refuse)
    shapes = []
    for sleep in (60, 0):
        runtime = ManagedLlamaRuntime(_serving_config(tmp_path, sleep_idle_seconds=sleep))
        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        shapes.append(runtime._serving_shape())
        with pytest.raises(RuntimeError):
            runtime._start_server()
    assert [c[c.index("--sleep-idle-seconds") + 1] for c in captured] == ["60", "-1"]
    assert shapes[0] != shapes[1]


def test_a_sleeping_or_absent_server_is_not_loaded(tmp_path, monkeypatch):
    """Asked before a judged question, without starting or waking anything."""
    from varyz.llm import local_runtime

    runtime = ManagedLlamaRuntime(_config(tmp_path))
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    assert not runtime.loaded()
    local_runtime._write_lease_state(
        runtime.state_path,
        {
            "version": 1,
            "status": "ready",
            "owner_pid": os.getpid(),
            "server_pid": 4242,
            "base_url": "http://127.0.0.1:1",
            "api_key": "key",
            "runtime_dir": str(runtime.runtime_dir.resolve()),
            "model_path": str(runtime.model_path.resolve()),
            "serving_shape": runtime._serving_shape(),
            "token": "t",
            "last_used": time.time(),
        },
    )
    alive = {4242: True}
    monkeypatch.setattr(local_runtime, "_process_alive", lambda pid: alive.get(pid, False))
    asked = []
    props = {"is_sleeping": False}

    def get(url, **kwargs):
        asked.append(url)
        return local_runtime.httpx.Response(
            200, json=props, request=local_runtime.httpx.Request("GET", url)
        )

    monkeypatch.setattr(local_runtime.httpx, "get", get)
    assert runtime.loaded()
    assert asked == ["http://127.0.0.1:1/props"]
    props["is_sleeping"] = True
    assert not runtime.loaded()
    del props["is_sleeping"]
    assert not runtime.loaded()
    props["is_sleeping"] = False
    alive[4242] = False
    assert not runtime.loaded()


def test_a_server_serving_another_shape_is_stopped_not_started_beside(tmp_path, monkeypatch):
    """One card, one server.

    A running server whose shape does not match is invisible to adoption, and
    what used to happen next was a second llama-server launched beside it on a
    card with room for exactly one. Adding a field to the shape string on
    2026-09-07 cost three runs to precisely that.
    """
    from varyz.llm import local_runtime

    stopped: list[int] = []
    started: list[str] = []

    runtime = ManagedLlamaRuntime(_config(tmp_path))
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    local_runtime._write_lease_state(
        runtime.state_path,
        {
            "version": 1,
            "status": "ready",
            "owner_pid": os.getpid(),
            "server_pid": 4242,
            "base_url": "http://127.0.0.1:1",
            "api_key": "key",
            "runtime_dir": str(runtime.runtime_dir.resolve()),
            "model_path": str(runtime.model_path.resolve()),
            # The shape this run needs carries uvm=1; this one does not.
            "serving_shape": "ctx=16384:slots=2:kv=q8_0:layers=auto:cram=4096",
            "token": "t",
            "last_used": time.time(),
        },
    )

    alive = {4242: True}
    monkeypatch.setattr(local_runtime, "_process_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        local_runtime._PidProcess,
        "terminate",
        lambda self: (stopped.append(self.pid), alive.__setitem__(self.pid, False))[0],
    )

    class Reached(RuntimeError):
        pass

    def start(self):
        # Getting this far is the assertion: the only way here is past the
        # stale server, and the card has to be free before it is asked for.
        started.append("started")
        raise Reached("far enough")

    monkeypatch.setattr(local_runtime.ManagedLlamaRuntime, "_start_server", start)

    lease = local_runtime._FileLease(runtime.lease_path)
    assert lease.acquire()
    with pytest.raises(Reached):
        runtime._adopt_or_start(lease)

    assert stopped == [4242]
    assert started == ["started"]

def test_a_layer_count_is_part_of_how_the_server_was_told_to_serve(tmp_path):
    """A server already running holds its layers until it is restarted, so a
    changed count has to read as a different server rather than a live one."""
    auto = ManagedLlamaRuntime(_config(tmp_path))
    pinned = ManagedLlamaRuntime(_config(tmp_path, gpu_layers=40))

    assert "layers=auto" in auto._serving_shape()
    assert "layers=40" in pinned._serving_shape()
    assert auto._serving_shape() != pinned._serving_shape()


def test_the_machine_can_say_how_much_of_its_memory_is_left():
    """Whatever this is running on, the reading has to be a real one."""
    reading = host_memory()
    assert reading is not None
    assert reading.total_mib > 0
    assert 0 <= reading.free_mib <= reading.total_mib


def test_a_machine_with_nothing_left_is_told_so_before_a_model_is_started(
    monkeypatch, tmp_path
):
    """The refusal names the driver, because nothing else will.

    Pages pinned for unified memory belong to no process once the server that
    asked for them is killed, so the machine reads as full while every process
    on it together holds five gigabytes -- and the first four explanations
    anyone reaches for are all wrong. The sentence is most of the fix.
    """
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime.sys, "platform", "win32")
    # `sys.platform` is the interpreter's own: on Linux, `shutil.which` would
    # take the Windows branch looking for nvidia-smi and fail.
    monkeypatch.setattr(local_runtime, "has_nvidia_gpu", lambda: False)
    monkeypatch.setattr(
        local_runtime, "host_memory", lambda: HostMemory(total_mib=31849, free_mib=300)
    )
    runtime = ManagedLlamaRuntime(_config(tmp_path))
    with pytest.raises(RuntimeError) as refused:
        runtime._start_server()
    said = str(refused.value)
    assert "300 MiB" in said and "31849" in said
    assert "driver" in said


def test_on_a_mac_the_refusal_says_the_model_shares_that_memory(monkeypatch, tmp_path):
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_runtime, "host_memory", lambda: HostMemory(total_mib=16384, free_mib=300)
    )
    with pytest.raises(RuntimeError) as refused:
        ManagedLlamaRuntime(_config(tmp_path))._start_server()
    said = str(refused.value)
    assert "300 MiB" in said and "Activity Monitor" in said
    assert "driver" not in said


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            300000.
Pages inactive:                           50000.
Pages speculative:                         4000.
Pages throttled:                              0.
Pages wired down:                        150000.
Pages purgeable:                           2000.
"""


def test_a_mac_reads_free_memory_from_vm_stat(monkeypatch):
    """macOS has no SC_AVPHYS_PAGES, so without this the guard never ran there."""
    from varyz.llm import local_runtime

    sizes = {"SC_PHYS_PAGES": 1_048_576, "SC_PAGE_SIZE": 16384}
    monkeypatch.setattr(local_runtime.os, "sysconf", sizes.__getitem__, raising=False)
    reading = local_runtime._host_memory_macos(VM_STAT)
    free_pages = 10000 + 50000 + 4000  # free + inactive + speculative
    assert reading == HostMemory(total_mib=16384, free_mib=free_pages * 16384 // 1_048_576)
    assert local_runtime._host_memory_macos("not vm_stat at all") is None


def test_the_memory_floor_can_be_turned_off_and_an_unreadable_machine_stops_nobody(
    monkeypatch, tmp_path
):
    """A guard that cannot see must never be the thing that stops a run."""
    from varyz.llm import local_runtime

    started = []
    real = local_runtime.subprocess.Popen

    def refuse(command, **kwargs):
        if "--gpu-layers" not in command:
            return real(command, **kwargs)
        started.append(command)
        raise RuntimeError("not started")

    monkeypatch.setattr(local_runtime.subprocess, "Popen", refuse)

    monkeypatch.setattr(
        local_runtime, "host_memory", lambda: HostMemory(total_mib=31849, free_mib=300)
    )
    off = ManagedLlamaRuntime(_config(tmp_path, host_memory_floor_mib=0))
    off.runtime_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="not started"):
        off._start_server()

    monkeypatch.setattr(local_runtime, "host_memory", lambda: None)
    blind = ManagedLlamaRuntime(_config(tmp_path))
    with pytest.raises(RuntimeError, match="not started"):
        blind._start_server()
    assert len(started) == 2


class _Child:
    """A server process that notices which way it was asked to go."""

    def __init__(self, *, pid: int = 4321, obeys: bool = True) -> None:
        self.pid = pid
        self.obeys = obeys
        self.asked = False
        self.killed = False
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("llama-server", timeout or 0)
        return self.returncode

    def terminate(self):
        self.killed = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = 1


def test_a_server_is_asked_to_stop_before_it_is_killed(monkeypatch, tmp_path):
    """Killing it is what leaves sixteen gigabytes behind.

    `TerminateProcess` -- which is all `terminate()` is on Windows -- runs no
    more of the child, so llama.cpp never frees what it mapped into host
    memory and the driver keeps the pages until the machine is rebooted. A
    Ctrl-Break the child can handle is the whole difference.
    """
    from varyz.llm import local_runtime

    sent = []

    def record(pid, sig):
        sent.append((pid, sig))
        child.returncode = 0

    monkeypatch.setattr(local_runtime.os, "kill", record)
    monkeypatch.setattr(local_runtime.os, "name", "nt")
    monkeypatch.setattr(local_runtime.signal, "CTRL_BREAK_EVENT", 1, raising=False)

    child = _Child()
    server = local_runtime._RunningServer(
        process=child,
        base_url="http://127.0.0.1:1/",
        api_key="k",
        log_handle=(tmp_path / "log").open("wb"),
        log_path=tmp_path / "log",
        askable=True,
    )
    assert local_runtime._ask_to_stop(server) is True
    assert sent == [(4321, 1)]
    assert child.killed is False


def test_a_server_we_did_not_start_is_still_stopped(monkeypatch, tmp_path):
    """One adopted from another process is not ours to send signals to.

    We do not know its process group, and a Ctrl-Break sent to the wrong one
    stops whatever else is in it -- including us. So an unaskable server falls
    through to the hard stop it always had, rather than being left running.
    """
    from varyz.llm import local_runtime

    monkeypatch.setattr(
        local_runtime.os, "kill", lambda *a: pytest.fail("signalled a stranger")
    )
    child = _Child()
    server = local_runtime._RunningServer(
        process=child,
        base_url="http://127.0.0.1:1/",
        api_key="k",
        log_handle=(tmp_path / "log").open("wb"),
        log_path=tmp_path / "log",
    )
    assert local_runtime._ask_to_stop(server) is False
    local_runtime._terminate(server)
    assert child.killed is True


def test_a_reused_server_pid_is_never_stopped(monkeypatch):
    """After a reboot the recorded pid can belong to another program."""
    from varyz import autosync
    from varyz.llm import local_runtime

    monkeypatch.setattr(local_runtime, "_process_alive", lambda pid: True)
    monkeypatch.setattr(autosync, "process_started_at", lambda pid: 2000.0)
    assert local_runtime._same_server({"last_used": 3000.0}, 4242)
    assert not local_runtime._same_server({"last_used": 1000.0}, 4242)
