"""Ollama for the local classifier: found, or installed without a prompt.

The classifier (``local_classifier.LocalModel``) speaks Ollama's HTTP API on
loopback, so setup makes one answer there with the configured model pulled.
An Ollama already on the machine is used when it is new enough. Otherwise a
pinned release is downloaded, checked against its SHA-256, and installed
where no administrator is needed:

* macOS: the standalone build, unpacked under ~/.varyz/ollama;
* Windows: Ollama's own installer, run with its silent switches, which puts it
  in %LOCALAPPDATA%\\Programs\\Ollama for this user and starts it at login;
* Linux: the standalone build, unpacked under ~/.varyz/ollama. It is packed
  with zstd, so this needs the `zstd` tool or Python 3.14; without either the
  install would need a prompt, and setup says so instead.

An Ollama that setup started itself is recorded, and ``start_recorded``
starts it again when the classifier finds it down (after a restart, say):
the standalone builds have nothing else that starts them.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from varyz.llm.local_runtime import Progress, download_verified, extract_archive

OLLAMA_VERSION = "0.34.4"
_RELEASE = f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}"
# qwen3.5's small models arrived in 0.17.5; the log-probabilities the
# classifier reads its answer from, in 0.12.11. An older Ollama answers, and
# then cannot load the model.
MIN_VERSION = (0, 17, 5)
# How long a started server has to answer, and how soon a failed start is retried.
START_TIMEOUT = 30.0
RETRY_AFTER = 60.0
_WINDOWS_INSTALL_TIMEOUT = 15 * 60.0


class OllamaUnavailable(RuntimeError):
    """Ollama cannot be made ready here without the owner; the reason says why."""


@dataclass(frozen=True)
class OllamaAsset:
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return f"{_RELEASE}/{self.name}"


# From the release's sha256sum.txt.
ASSETS: dict[tuple[str, str], OllamaAsset] = {
    ("darwin", "any"): OllamaAsset(
        "ollama-darwin.tgz",
        "e9c8fddaab5f48f47f2c4ae3d23d0732f5182417125353faeed2188e34a22799",
        160_042_307,
    ),
    # One installer for x64 and ARM.
    ("windows", "any"): OllamaAsset(
        "OllamaSetup.exe",
        "4a6514323eb8c131f6c8bb651b4fe2edf5f1525f4fd8b84315ba20f1af3e210f",
        1_571_115_536,
    ),
    ("linux", "x86_64"): OllamaAsset(
        "ollama-linux-amd64.tar.zst",
        "c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533",
        1_427_703_051,
    ),
    ("linux", "aarch64"): OllamaAsset(
        "ollama-linux-arm64.tar.zst",
        "96f50a1192133028cf4e010d8c333f8af14b1505db6be7b2034c11487e7fd7e6",
        1_549_684_612,
    ),
}


def _system(system: str | None = None) -> str:
    return (system or platform.system()).lower()


def _machine(machine: str | None = None) -> str:
    value = (machine or platform.machine()).lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(value, value)


def platform_asset(system: str | None = None, machine: str | None = None) -> OllamaAsset | None:
    """The pinned download for this machine, or None where there is none."""
    name = _system(system)
    return ASSETS.get((name, "any")) or ASSETS.get((name, _machine(machine)))


def ollama_home() -> Path:
    """~/.varyz/ollama, or ollama under $VARYZ_HOME: what setup installs and records."""
    from varyz.config import home_config_file

    return home_config_file().parent / "ollama"


def managed_executable(system: str | None = None) -> Path:
    """Where the install below leaves the `ollama` program."""
    name = _system(system)
    if name == "windows":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "Programs" / "Ollama" / "ollama.exe"
    folder = ollama_home() / OLLAMA_VERSION
    return folder / "ollama" if name == "darwin" else folder / "bin" / "ollama"


def candidates(system: str | None = None) -> list[Path]:
    """Every `ollama` program on this machine, the one setup installs first."""
    name = _system(system)
    found = [managed_executable(system)]
    on_path = shutil.which("ollama")
    if on_path:
        found.append(Path(on_path))
    if name == "darwin":
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            found.append(apps / "Ollama.app" / "Contents" / "Resources" / "ollama")
    unique: list[Path] = []
    for path in found:
        if path.is_file() and path not in unique:
            unique.append(path)
    return unique


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def version_label(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def binary_version(
    executable: Path, *, run: Callable[..., Any] = subprocess.run
) -> tuple[int, ...] | None:
    """The version of the program itself.

    With a server up, `ollama --version` names the server's version first and
    its own in a warning after it, so the warning wins when there is one.
    """
    try:
        done = run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = f"{done.stdout or ''}\n{done.stderr or ''}"
    client = re.search(r"client version is\s+(\S+)", output)
    if client:
        return parse_version(client.group(1))
    server = re.search(r"version is\s+(\S+)", output)
    return parse_version(server.group(1)) if server else None


def server_version(
    base_url: str, *, get: Callable[..., Any] = httpx.get
) -> tuple[int, ...] | None:
    """The version of the Ollama answering at ``base_url``; None when nothing does."""
    try:
        response = get(f"{base_url}/api/version", timeout=2.0)
        response.raise_for_status()
        return parse_version(str(response.json().get("version", "")))
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def endpoint(url: str) -> tuple[str, int, str]:
    """(host, port, base URL) for a loopback classifier URL."""
    parts = urlsplit(url)
    host = "127.0.0.1" if parts.hostname in (None, "localhost") else parts.hostname
    port = parts.port or 11434
    shown = f"[{host}]" if ":" in host else host
    return host, port, f"{parts.scheme or 'http'}://{shown}:{port}"


# -- install -------------------------------------------------------------------


def install(
    on_progress: Progress | None = None,
    *,
    system: str | None = None,
    machine: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    download: Callable[..., None] = download_verified,
) -> Path:
    """Install the pinned Ollama for this user, without a prompt; returns the program."""
    from varyz.diskspace import bytes_label, free_bytes

    name = _system(system)
    asset = platform_asset(system, machine)
    if asset is None:
        raise OllamaUnavailable(
            f"there is no Ollama build for {platform.system()} {platform.machine()}"
        )
    zstd_tool = shutil.which("zstd")
    if name == "linux" and zstd_tool is None and sys.version_info < (3, 14):
        raise OllamaUnavailable(
            "Ollama for Linux is packed with zstd, and this machine has no zstd to "
            "unpack it; install zstd (sudo apt install zstd), then run setup again"
        )
    home = ollama_home()
    downloads = home / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    free = free_bytes(downloads)
    # The download, and as much again unpacked.
    if free is not None and free < 2 * asset.size:
        raise OllamaUnavailable(
            f"Ollama needs {bytes_label(2 * asset.size)} free and "
            f"{bytes_label(free)} is left"
        )
    archive = downloads / asset.name
    download(asset.url, archive, asset.sha256, asset.size, "Downloading Ollama", on_progress)
    target = managed_executable(system)
    if name == "windows":
        _run_windows_installer(archive, popen=popen)
    else:
        if on_progress is not None:
            on_progress("Unpacking Ollama", 0, 0)
        folder = home / OLLAMA_VERSION
        stage = home / f".{OLLAMA_VERSION}.partial"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        if asset.name.endswith(".tar.zst"):
            _extract_zstd(archive, stage, zstd_tool=zstd_tool, run=run)
        else:
            extract_archive(archive, stage)
        shutil.rmtree(folder, ignore_errors=True)
        from varyz.filesystem import publish_directory

        publish_directory(stage, folder)
    with contextlib.suppress(OSError):
        archive.unlink()
    if not target.is_file():
        raise OllamaUnavailable(f"the Ollama install left no program at {target}")
    return target


def _run_windows_installer(installer: Path, *, popen: Callable[..., Any]) -> None:
    # The app shows its welcome window on a first start; this marker, which
    # Ollama's own install.ps1 writes, tells it to start hidden instead.
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    marker = Path(local) / "Ollama" / "upgraded"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    # Only the installer is waited on: it starts the app, which stays running.
    process = popen(
        [str(installer), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        code = process.wait(timeout=_WINDOWS_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        raise OllamaUnavailable("the Ollama installer did not finish in 15 minutes") from None
    if code != 0:
        raise OllamaUnavailable(f"the Ollama installer failed with exit code {code}")


def _extract_zstd(
    archive: Path, destination: Path, *, zstd_tool: str | None, run: Callable[..., Any]
) -> None:
    tar = archive.with_name(archive.name.removesuffix(".zst"))
    try:
        if zstd_tool is not None:
            done = run(
                [zstd_tool, "-d", "-q", "-f", "-o", str(tar), str(archive)],
                capture_output=True,
                text=True,
                timeout=15 * 60,
                check=False,
            )
            if done.returncode != 0:
                raise OllamaUnavailable(f"zstd could not unpack Ollama: {done.stderr.strip()}")
        else:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14

            with zstd.open(archive, "rb") as source, tar.open("wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
        extract_archive(tar, destination)
    finally:
        with contextlib.suppress(OSError):
            tar.unlink()


# -- serve ---------------------------------------------------------------------


def _record_path() -> Path:
    return ollama_home() / "server.json"


def serve(
    executable: Path, host: str, port: int, *, popen: Callable[..., Any] = subprocess.Popen
) -> None:
    """Start `ollama serve` on host:port, detached, its log beside the install."""
    home = ollama_home()
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    with (home / "serve.log").open("ab") as log:
        popen(  # noqa: S603 - a fixed program found or installed by setup
            [str(executable), "serve"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            close_fds=True,
            start_new_session=sys.platform != "win32",
        )


def wait_until_up(
    base_url: str,
    *,
    timeout: float = START_TIMEOUT,
    probe: Callable[[str], tuple[int, ...] | None] = server_version,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, ...] | None:
    deadline = clock() + timeout
    while True:
        version = probe(base_url)
        if version is not None or clock() >= deadline:
            return version
        sleep(0.5)


def record_server(executable: Path, host: str, port: int) -> None:
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"executable": str(executable), "host": host, "port": port}),
        encoding="utf-8",
    )


def start_recorded(
    host: str, port: int, *, popen: Callable[..., Any] = subprocess.Popen
) -> bool:
    """Start the Ollama setup started on host:port, if there is one; True when started.

    A start is tried at most once a minute, so a program that cannot start is
    not started again by every question asked while it is down.
    """
    path = _record_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if record.get("host") != host or record.get("port") != port:
        return False
    executable = Path(str(record.get("executable", "")))
    if not executable.is_file():
        return False
    attempt = path.with_name("server.started")
    with contextlib.suppress(OSError):
        if time.time() - attempt.stat().st_mtime < RETRY_AFTER:
            return False
    try:
        attempt.touch()
        serve(executable, host, port, popen=popen)
    except OSError:
        return False
    return True


# -- the model -----------------------------------------------------------------


def has_model(base_url: str, model: str, *, get: Callable[..., Any] = httpx.get) -> bool:
    """Whether the model is pulled; a name without a tag matches any tag, as the classifier's."""
    response = get(f"{base_url}/api/tags", timeout=5.0)
    response.raise_for_status()
    base = model.split(":")[0]
    for entry in response.json().get("models", []):
        name = str(entry.get("name", ""))
        if name == model or (":" not in model and name.split(":")[0] == base):
            return True
    return False


def pull(
    base_url: str,
    model: str,
    on_progress: Progress | None = None,
    *,
    stream: Callable[..., Any] = httpx.stream,
) -> None:
    """Pull the model through the server, reporting bytes as they arrive."""
    # Connecting is quick; between two progress lines a slow registry may pause.
    timeout = httpx.Timeout(10.0, read=300.0)
    phase = f"Downloading {model}"
    try:
        with stream(
            "POST", f"{base_url}/api/pull", json={"model": model, "stream": True},
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            status = ""
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("error"):
                    raise OllamaUnavailable(f"Ollama could not pull {model}: {event['error']}")
                status = str(event.get("status", ""))
                total = int(event.get("total") or 0)
                if on_progress is not None and total:
                    on_progress(phase, int(event.get("completed") or 0), total)
    except httpx.HTTPError as exc:
        raise OllamaUnavailable(f"Ollama could not pull {model}: {exc}") from exc
    if status != "success":
        raise OllamaUnavailable(f"the pull of {model} ended at '{status or 'nothing'}'")


# -- setup ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifierSetup:
    detail: str
    # The URL the classifier now uses, when setup had to move it.
    moved_to: str | None = None


def ensure_classifier(
    cfg: Any,
    cfg_path: Path,
    on_progress: Progress | None = None,
    *,
    probe: Callable[[str], tuple[int, ...] | None] = server_version,
    version_of: Callable[[Path], tuple[int, ...] | None] = binary_version,
    find: Callable[[], list[Path]] = candidates,
    installer: Callable[[Progress | None], Path] = install,
    start: Callable[[Path, str, int], None] = serve,
    wait: Callable[[str], tuple[int, ...] | None] = wait_until_up,
    pulled: Callable[[str, str], bool] = has_model,
    puller: Callable[[str, str, Progress | None], None] = pull,
) -> ClassifierSetup:
    """An Ollama new enough on the classifier's URL, with its model pulled.

    Raises OllamaUnavailable with the reason when that needs the owner.
    """
    from varyz.configfile import set_classifier_url

    model = cfg.workflows.classifier_model
    host, port, base_url = endpoint(cfg.workflows.classifier_url)
    notes: list[str] = []
    moved_to = None
    running = probe(base_url)
    if running is None or running < MIN_VERSION:
        usable = [path for path in find() if (version_of(path) or (0,)) >= MIN_VERSION]
        if usable:
            executable = usable[0]
        else:
            executable = installer(on_progress)
            notes.append(f"installed Ollama {OLLAMA_VERSION}")
            # Windows' installer upgrades an older Ollama in place and starts it.
            running = probe(base_url)
    if running is not None and running < MIN_VERSION:
        # An older Ollama holds the port, and it is the owner's: the new one
        # serves beside it, and the classifier is pointed there.
        port = _free_port(host, port + 1)
        _, _, base_url = endpoint(f"http://{host}:{port}")
        moved_to = base_url
        set_classifier_url(cfg_path, base_url)
        notes.append(
            f"Ollama {version_label(running)} is too old for {model}, "
            f"so a newer one serves on port {port}"
        )
        running = None
    if running is None:
        start(executable, host, port)
        if wait(base_url) is None:
            raise OllamaUnavailable(
                f"Ollama did not start on {base_url}; its log is {ollama_home() / 'serve.log'}"
            )
        record_server(executable, host, port)
    try:
        ready = pulled(base_url, model)
    except (httpx.HTTPError, ValueError) as exc:
        raise OllamaUnavailable(f"Ollama did not list its models: {exc}") from exc
    if ready:
        notes.append(f"{model} ready")
    else:
        puller(base_url, model, on_progress)
        notes.append(f"{model} pulled")
    return ClassifierSetup("; ".join(notes), moved_to)


def _free_port(host: str, first: int) -> int:
    import socket

    for port in range(first, first + 100):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise OllamaUnavailable(f"no free port for Ollama from {first} to {first + 99}")


__all__ = [
    "ASSETS",
    "MIN_VERSION",
    "OLLAMA_VERSION",
    "ClassifierSetup",
    "OllamaAsset",
    "OllamaUnavailable",
    "binary_version",
    "candidates",
    "endpoint",
    "ensure_classifier",
    "has_model",
    "install",
    "managed_executable",
    "ollama_home",
    "platform_asset",
    "pull",
    "record_server",
    "serve",
    "server_version",
    "start_recorded",
    "wait_until_up",
]
