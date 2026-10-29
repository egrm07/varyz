"""Pinned, Varyz-owned llama.cpp runtime and GGUF model lifecycle."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlparse

import httpx

from varyz.config import Config
from varyz.db.connection import FileLock
from varyz.diskspace import bytes_label, free_bytes

log = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class RuntimeAsset:
    name: str
    url: str
    sha256: str
    size: int


RELEASE = "b10221"
RELEASE_ROOT = f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}"
RUNTIME_ASSETS: dict[tuple[str, str, str], RuntimeAsset] = {
    ("Windows", "x86_64", "vulkan"): RuntimeAsset(
        "llama-b10221-bin-win-vulkan-x64.zip",
        f"{RELEASE_ROOT}/llama-b10221-bin-win-vulkan-x64.zip",
        "0d33b349f685f34182dfa5ebd463737c31510c4f93e49865fe0e77e2a6aff78f",
        34_089_103,
    ),
    ("Windows", "x86_64", "cuda"): RuntimeAsset(
        "llama-b10221-bin-win-cuda-13.3-x64.zip",
        f"{RELEASE_ROOT}/llama-b10221-bin-win-cuda-13.3-x64.zip",
        "a544e77f2124812dd52120ee7f3b8f52b7a076b7a62f9b382904d74fdb07db99",
        146_509_334,
    ),
    ("Windows", "x86_64", "cpu"): RuntimeAsset(
        "llama-b10221-bin-win-cpu-x64.zip",
        f"{RELEASE_ROOT}/llama-b10221-bin-win-cpu-x64.zip",
        "61dd281e1c5be8a7ae0b403efd0feee9da126e40605c27df35f9d5bef49caaf9",
        18_352_727,
    ),
    ("Darwin", "arm64", "auto"): RuntimeAsset(
        "llama-b10221-bin-macos-arm64.tar.gz",
        f"{RELEASE_ROOT}/llama-b10221-bin-macos-arm64.tar.gz",
        "e0cf88b3611cd9bedaff14235cc61b36e38a391204966daa49e73b41d7eba155",
        10_941_074,
    ),
    ("Darwin", "x86_64", "auto"): RuntimeAsset(
        "llama-b10221-bin-macos-x64.tar.gz",
        f"{RELEASE_ROOT}/llama-b10221-bin-macos-x64.tar.gz",
        "93f9c2de8fcf27e60f7d0098b455e0f437968fa7b158af7fb0d9ecfe73692198",
        11_220_273,
    ),
    ("Linux", "x86_64", "vulkan"): RuntimeAsset(
        "llama-b10221-bin-ubuntu-vulkan-x64.tar.gz",
        f"{RELEASE_ROOT}/llama-b10221-bin-ubuntu-vulkan-x64.tar.gz",
        "d6de3b5deeaa9c06587ffa75cab457096552293812b9223f09b308fccb30c4d7",
        32_424_670,
    ),
    ("Linux", "x86_64", "cpu"): RuntimeAsset(
        "llama-b10221-bin-ubuntu-x64.tar.gz",
        f"{RELEASE_ROOT}/llama-b10221-bin-ubuntu-x64.tar.gz",
        "db681790ca20204480abb4ce4ed157c45a8e0db801f775c7634ad710001ba54b",
        16_437_476,
    ),
    ("Linux", "arm64", "cpu"): RuntimeAsset(
        "llama-b10221-bin-ubuntu-arm64.tar.gz",
        f"{RELEASE_ROOT}/llama-b10221-bin-ubuntu-arm64.tar.gz",
        "b98f59809d395b600c1beb01694249432835ba39c919a55989cbf8ddaa684857",
        13_340_687,
    ),
}


# The CUDA build links against the CUDA runtime libraries, which ship as a
# separate archive and must land in the same directory as llama-server.exe.
COMPANION_ASSETS: dict[tuple[str, str, str], RuntimeAsset] = {
    ("Windows", "x86_64", "cuda"): RuntimeAsset(
        "cudart-llama-bin-win-cuda-13.3-x64.zip",
        f"{RELEASE_ROOT}/cudart-llama-bin-win-cuda-13.3-x64.zip",
        "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e",
        390_970_417,
    ),
}


def has_nvidia_gpu() -> bool:
    """Whether an NVIDIA driver is installed and reports at least one GPU.

    `nvidia-smi` ships with the driver, so its absence is a reliable enough
    "do not download half a gigabyte of CUDA runtime". A machine that has the
    driver but a GPU the build does not cover still falls back to Vulkan when
    the server fails to start.
    """
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


@dataclass(frozen=True)
class HostMemory:
    """How much physical memory the machine has, and how much is left of it.

    Not a process figure. The thing this exists to catch does not belong to a
    process: pages pinned by the display driver on behalf of unified-memory
    allocations are invisible to Task Manager, to a sum of every working set on
    the machine, and to every other user-mode counter. What they are visible in
    is this -- the total stops matching what anything can account for.

    Measured on 2026-09-18. After a night of compiles: 0.6 GB free of 31.1,
    with every process together holding 5 GB and 22.8 GB unattributable to any
    of them. Nineteen minutes after the reboot that cleared it: 19.5 GB free,
    4.5 GB unattributable. The card was idle in both readings.
    """

    total_mib: int
    free_mib: int


def host_memory() -> HostMemory | None:
    """What is left of physical memory, or None where we cannot ask.

    Windows answers through `GlobalMemoryStatusEx`, macOS through `vm_stat`,
    Linux through `sysconf`. Anything unexpected reads as "cannot say" rather
    than as an empty machine: this guards a run, and a guard that cannot see
    must never stop one.
    """
    if os.name == "nt":
        return _host_memory_windows()
    if sys.platform == "darwin":
        return _host_memory_macos()
    try:
        sysconf = os.sysconf  # type: ignore[attr-defined,unused-ignore]
        pages = sysconf("SC_AVPHYS_PAGES")
        total = sysconf("SC_PHYS_PAGES")
        size = sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages < 0 or total < 0 or size <= 0:
        return None
    return HostMemory(total_mib=total * size // 1_048_576, free_mib=pages * size // 1_048_576)


def _host_memory_macos(vm_stat: str | None = None) -> HostMemory | None:
    """Free physical memory on a Mac, from `vm_stat`.

    macOS has no `SC_AVPHYS_PAGES`, so the POSIX path always answered "cannot
    say" there and the guard never ran -- on Apple Silicon, where the model's
    weights live in the same memory as everything else, it matters most.
    Free, inactive and speculative pages are what the system hands out without
    swapping; inactive ones are reclaimed first, which is why Activity Monitor
    counts them as available too.
    """
    if vm_stat is None:
        try:
            vm_stat = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        sysconf = os.sysconf  # type: ignore[attr-defined,unused-ignore]
        total = sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    lines = vm_stat.splitlines()
    size_words = lines[0].split("page size of ") if lines else []
    try:
        page_size = int(size_words[1].split()[0]) if len(size_words) == 2 else 0
    except ValueError:
        page_size = 0
    counts: dict[str, int] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        with contextlib.suppress(ValueError):
            counts[name.strip()] = int(value.strip().rstrip("."))
    wanted = ("Pages free", "Pages inactive", "Pages speculative")
    if page_size <= 0 or total <= 0 or any(name not in counts for name in wanted):
        return None
    free = sum(counts[name] for name in wanted) * page_size
    return HostMemory(total_mib=total // 1_048_576, free_mib=free // 1_048_576)


def _host_memory_windows() -> HostMemory | None:
    import ctypes

    class _Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _Status()
    status.dwLength = ctypes.sizeof(_Status)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return HostMemory(
        total_mib=int(status.ullTotalPhys // 1_048_576),
        free_mib=int(status.ullAvailPhys // 1_048_576),
    )


def _machine(value: str) -> str:
    lowered = value.casefold()
    if lowered in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if lowered in {"arm64", "aarch64"}:
        return "arm64"
    return lowered


def resolve_variant(
    variant: str, system: str, machine: str, *, nvidia: bool | None = None
) -> str:
    """Turn `auto` into a concrete backend for this machine."""
    if system == "Darwin":
        return "auto"
    if variant != "auto":
        return variant
    if machine != "x86_64":
        return "cpu"
    if nvidia is None:
        nvidia = has_nvidia_gpu()
    if nvidia and (system, machine, "cuda") in RUNTIME_ASSETS:
        return "cuda"
    return "vulkan"


def runtime_asset(
    *,
    system: str | None = None,
    machine: str | None = None,
    variant: str = "auto",
    nvidia: bool | None = None,
) -> RuntimeAsset:
    system = system or platform.system()
    machine = _machine(machine or platform.machine())
    selected = resolve_variant(variant, system, machine, nvidia=nvidia)
    try:
        return RUNTIME_ASSETS[(system, machine, selected)]
    except KeyError as exc:
        raise RuntimeError(
            f"managed local inference is not packaged for {system}/{machine}/{selected}; "
            "set [local_model] backend = \"ollama\" to use an existing Ollama service"
        ) from exc


def companion_asset(
    *,
    system: str | None = None,
    machine: str | None = None,
    variant: str = "auto",
    nvidia: bool | None = None,
) -> RuntimeAsset | None:
    """The extra archive this backend needs beside the server, if any."""
    system = system or platform.system()
    machine = _machine(machine or platform.machine())
    selected = resolve_variant(variant, system, machine, nvidia=nvidia)
    return COMPANION_ASSETS.get((system, machine, selected))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> None:
    member = PurePosixPath(name.replace("\\", "/"))
    if member.is_absolute() or ".." in member.parts:
        raise RuntimeError(f"runtime archive contains unsafe path: {name}")


def _sibling_link(target: str) -> bool:
    """Whether a symlink points at a file beside it, and nowhere else.

    The macOS and Linux builds ship versioned shared libraries, and the server
    is linked against the versioned name: `libllama.dylib -> libllama.0.dylib`
    and so on. Refusing every link made those archives uninstallable. A link
    to a sibling by bare name is all they need; anything with a separator can
    reach out of the folder and stays refused.
    """
    return bool(target) and "/" not in target and "\\" not in target and target != ".."


def _unwrap_single_folder(destination: Path) -> None:
    """Lift the contents of an archive's one top-level folder into place.

    The Windows zips are flat. The macOS and Linux tarballs wrap everything in
    `llama-<release>/`, which left the server one level below where the
    install looks for it.
    """
    entries = list(destination.iterdir())
    if len(entries) != 1 or entries[0].is_symlink() or not entries[0].is_dir():
        return
    # Renamed first, so a file inside that shares the folder's name can land.
    folder = entries[0].rename(destination / f".unwrap-{os.getpid()}")
    for child in folder.iterdir():
        os.replace(child, destination / child.name)
    folder.rmdir()


def download_verified(
    url: str,
    destination: Path,
    expected_sha256: str,
    expected_size: int,
    phase: str,
    on_progress: Progress | None,
) -> None:
    """Fetch ``url`` to ``destination``, resuming a partial file, then verify it.

    Nothing reaches ``destination`` until its size and SHA-256 match.
    """
    if destination.is_file():
        correct_size = destination.stat().st_size == expected_size
        if correct_size and _sha256(destination) == expected_sha256:
            return
        raise RuntimeError(f"existing {phase} file failed verification: {destination}")
    part = destination.with_suffix(destination.suffix + ".part")
    if part.is_file() and part.stat().st_size == expected_size:
        if _sha256(part) != expected_sha256:
            raise RuntimeError(f"existing partial {phase} failed verification: {part}")
        os.replace(part, destination)
        return
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.stream(
        "GET", url, headers=headers, follow_redirects=True, timeout=timeout
    ) as response:
        response.raise_for_status()
        append = offset > 0 and response.status_code == 206
        if offset and not append:
            offset = 0
        mode = "ab" if append else "wb"
        downloaded = offset
        with part.open(mode) as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(phase, downloaded, expected_size)
    if part.stat().st_size != expected_size:
        raise RuntimeError(
            f"{phase} download size mismatch: {part.stat().st_size} != {expected_size}"
        )
    actual = _sha256(part)
    if actual != expected_sha256:
        raise RuntimeError(
            f"{phase} download checksum mismatch: {actual} != {expected_sha256}"
        )
    os.replace(part, destination)


def extract_archive(archive: Path, destination: Path) -> None:
    """Unpack a .zip, .tar.gz, .tgz or .tar, refusing any path or link that leaves it."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                _safe_member(entry.filename)
            zipped.extractall(destination)
        return
    if archive.name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:" if archive.name.endswith(".tar") else "r:gz"
        with tarfile.open(archive, mode) as tarred:
            for member in tarred.getmembers():
                _safe_member(member.name)
                if member.islnk() or (
                    member.issym() and not _sibling_link(member.linkname)
                ):
                    raise RuntimeError(
                        f"runtime archive contains unsupported link: {member.name}"
                    )
            tarred.extractall(destination, filter="data")
        _unwrap_single_folder(destination)
        return
    raise RuntimeError(f"unsupported runtime archive: {archive.name}")


def _installed_asset(runtime_dir: Path) -> str | None:
    """Which archive a runtime directory was built from, per its receipt."""
    try:
        receipt = json.loads((runtime_dir / "INSTALL.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    asset = receipt.get("asset")
    return str(asset) if asset else None


@dataclass(frozen=True)
class LocalRuntimeStatus:
    backend: str
    runtime_installed: bool
    model_installed: bool
    runtime_path: Path
    model_path: Path
    detail: str


@dataclass
class _RunningServer:
    process: subprocess.Popen[bytes] | _PidProcess
    base_url: str
    api_key: str
    log_handle: BinaryIO
    log_path: Path
    last_used: float = 0.0
    owner_lease: _FileLease | None = None
    state_path: Path | None = None
    state_lock_path: Path | None = None
    usage_lock_path: Path | None = None
    lease_token: str | None = None
    # Whether this child was started in its own process group and so can be
    # asked to stop rather than killed. False for one adopted from another
    # process, whose group we do not know. See `_ask_to_stop`.
    askable: bool = False


@dataclass(frozen=True)
class ManagedEndpoint:
    base_url: str
    api_key: str
    server_pid: int
    lease_token: str


class ManagedServerLeaseHeldError(RuntimeError):
    """A live Varyz process owns an incompatible or unready managed server."""


class LocalModelNotInstalled(RuntimeError):
    """The managed model this command needs has not been downloaded yet.

    A missing optional download is a state of the machine, not a fault in the
    program, and the fix is one command. Typed so `main` can print the sentence
    instead of a traceback: on a tool somebody installed five minutes ago a
    traceback reads as a broken install.
    """


# The alpha holds the installer back (docs/SHIPPING.md): its build has no
# `varyz.llm.local_install`, so it never downloads the managed model, and a
# message telling someone to run `varyz local-model install` names a command
# that is not there.
def installer_is_shipped() -> bool:
    # find_spec, not an import: the module stays unloaded until the command runs.
    try:
        return importlib.util.find_spec("varyz.llm.local_install") is not None
    except ModuleNotFoundError as exc:
        # Only the module itself being absent means "held back".
        if exc.name != "varyz.llm.local_install":
            raise
        return False


# The managed model with its runtime and room for the owner's other apps;
# the same line setup draws before it offers local learning.
MANAGED_MEMORY_BYTES = 15 * 1024**3


def too_little_memory() -> int | None:
    """This computer's memory in whole GB when it is too small for the managed
    model, else None (enough, or unknown)."""
    import platform

    from varyz.setup_machine import memory

    total = memory(platform.system())[0]
    if total is None or total >= MANAGED_MEMORY_BYTES:
        return None
    return round(total / 1024**3)


def install_hint() -> str:
    """What to do about a managed model that is not installed."""

    # The test Mac (8 GB) was told the model was configured and to install it.
    gigabytes = too_little_memory()
    if gigabytes is not None:
        return (f"this computer has {gigabytes} GB of memory and the local model needs 16 GB; "
                "Varyz learns from your agents' conversations instead")
    if installer_is_shipped():
        return "run: varyz local-model install"
    return "this build of varyz does not install it"


def compile_command() -> str:
    """The personal-model compile this build can run.

    A semantic compile needs the managed model, so a build without the
    installer compiles the explicit claims only.
    """

    if installer_is_shipped():
        return "varyz model compile --run"
    return "varyz model compile --literal-only --run"


def default_graph_extractor() -> str:
    """The graph extractor this build can run without being told.

    The contextual extractor reads with the managed model; a build without the
    installer builds the deterministic graph and refuses the others.
    """

    return "contextual_facets" if installer_is_shipped() else "deterministic"


def _console_attached() -> bool:
    """Whether this process has a console for a child to share.

    Only a child on our own console can be sent a Ctrl-Break; one given its
    own hidden console is unreachable, and one started by a scheduler has no
    console at all. `GetConsoleCP` answers for a hidden console too, which
    `GetConsoleWindow` does not -- it returns nothing for a console with no
    window, which is precisely the case that matters here.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]
    except Exception:
        return False


def _process_alive(pid: int) -> bool:
    """Probe a pid without accidentally terminating it on Windows."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return kernel32.GetLastError() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _same_server(state: dict[str, object], pid: int) -> bool:
    """The recorded server is still this pid, not a later process that got its
    number (after a reboot, say): only then may this runtime stop it."""
    from varyz.autosync import process_started_at

    if not _process_alive(pid):
        return False
    recorded = state.get("last_used")
    started = process_started_at(pid)
    return (not isinstance(recorded, int | float) or started is None
            or started <= float(recorded) + 2.0)


class _PidProcess:
    """The small Popen surface needed after adopting an orphaned server."""

    def __init__(self, pid: int):
        self.pid = pid

    def poll(self) -> int | None:
        return None if _process_alive(self.pid) else 1

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while _process_alive(self.pid):
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout or 0.0)
            time.sleep(0.05)
        return 0


class _FileLease:
    """Process-death-safe file lease, matching Varyz' archive lock primitive."""

    def __init__(self, path: Path, *, shared: bool = False):
        self.path = path
        self.shared = shared
        self._lock: FileLock | None = None

    def acquire(self) -> bool:
        lock = FileLock(self.path, shared=self.shared)
        if not lock.acquire():
            return False
        self._lock = lock
        return True

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
        self._lock = None


def _read_lease_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_lease_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    stage.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        stage.chmod(0o600)
    # Windows refuses to replace a file another handle has open, and this one is
    # read without a lock on every request. Writers are serialised against each
    # other; they are not serialised against readers, and with several requests
    # in flight the collision is routine rather than rare. It clears in
    # microseconds, so it is waited out rather than raised — the alternative was
    # a whole group of episodes lost to a bookkeeping rename.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(stage, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                stage.unlink(missing_ok=True)
                raise
            time.sleep(0.01)


def _state_pid(state: dict[str, object] | None, key: str) -> int:
    if state is None:
        return 0
    value = state.get(key, 0)
    if not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


# How long adoption keeps asking a running server whether it is alive before
# it gives up on it. Sized for a server that is busy rather than one that is
# gone: a health probe queued behind six decoding slots is late, not dead.
ADOPTION_HEALTH_TIMEOUT_SECONDS = 5.0
ADOPTION_HEALTH_BUDGET_SECONDS = 30.0

_SERVERS: dict[Path, _RunningServer] = {}
_SERVER_LOCK = threading.Lock()
_IDLE_TIMER: threading.Timer | None = None


def _schedule_idle_unload(after_seconds: float) -> None:
    """Arm a timer that shuts idle servers down, replacing any previous one.

    Called on the way out of every endpoint handout, so the deadline always
    measures from the last request rather than from the first. Callers hold
    `_SERVER_LOCK`, which is what keeps two threads from arming two timers.
    """
    global _IDLE_TIMER
    if _IDLE_TIMER is not None:
        _IDLE_TIMER.cancel()
    if after_seconds <= 0:
        _IDLE_TIMER = None
        return
    timer = threading.Timer(after_seconds, _unload_if_idle, args=(after_seconds,))
    timer.daemon = True
    _IDLE_TIMER = timer
    timer.start()


def _unload_if_idle(idle_seconds: float) -> None:
    """Stop idle servers only after every process has left its usage lease."""
    now = time.monotonic()
    wall_now = time.time()
    stopping: list[tuple[_RunningServer, _FileLease | None]] = []
    with _SERVER_LOCK:
        for key, server in list(_SERVERS.items()):
            state = _read_lease_state(server.state_path) if server.state_path else None
            remote_used = state.get("last_used") if state else None
            remotely_idle = not isinstance(remote_used, int | float) or (
                wall_now - float(remote_used) >= idle_seconds
            )
            if now - server.last_used < idle_seconds or not remotely_idle:
                continue
            usage = (
                _FileLease(server.usage_lock_path)
                if server.usage_lock_path is not None
                else None
            )
            if usage is not None and not usage.acquire():
                continue
            _SERVERS.pop(key)
            stopping.append((server, usage))
        if _SERVERS:
            _schedule_idle_unload(idle_seconds)
    for server, usage in stopping:
        try:
            _terminate(server)
        finally:
            if usage is not None:
                usage.release()


# How long a child gets to put itself away before it is stopped the hard way.
# Freeing sixteen gigabytes of unified-memory mappings is not instant and the
# whole point is to let it finish; a child that has not gone by then was not
# going to, and the old hard stop still follows.
STOP_GRACE_SECONDS = 30.0


def _ask_to_stop(server: _RunningServer) -> bool:
    """Send the child the Ctrl-Break a person would, and wait for it to go.

    This is the difference between a server that frees what it mapped into the
    card's host window and one that does not. On POSIX `terminate()` already
    sends a signal a program can handle, so this is Windows-only; there,
    `terminate()` is `TerminateProcess` and nothing in the child ever runs
    again. Returns whether the child is gone, so the caller can fall through
    to the hard stop without knowing why this did not work.
    """
    if not server.askable or os.name != "nt":
        return False
    pid = getattr(server.process, "pid", None)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    except (OSError, AttributeError, ValueError):
        return False
    try:
        server.process.wait(timeout=STOP_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return True


def _terminate(server: _RunningServer) -> None:
    if server.process.poll() is None and not _ask_to_stop(server):
        with contextlib.suppress(OSError):
            server.process.terminate()
        try:
            server.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                server.process.kill()
            # A killed child still holds the card until the driver has unloaded
            # its weights, and on 2026-09-10 that took minutes: `graph rebuild`
            # returned, the lease file was gone, and a compile started straight
            # after found nine gigabytes still resident and refused to run.
            # Waiting here costs the shutdown a few seconds and means that when
            # this returns, the GPU is free.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                server.process.wait(timeout=60)
    server.log_handle.close()
    state_guard = (
        _FileLease(server.state_lock_path)
        if server.state_lock_path is not None
        else None
    )
    if state_guard is not None:
        while not state_guard.acquire():
            time.sleep(0.01)
    try:
        if server.state_path is not None and server.lease_token is not None:
            state = _read_lease_state(server.state_path)
            if state is not None and state.get("token") == server.lease_token:
                with contextlib.suppress(OSError):
                    server.state_path.unlink()
    finally:
        if state_guard is not None:
            state_guard.release()
    if server.owner_lease is not None:
        server.owner_lease.release()


def _detach(server: _RunningServer) -> None:
    """Hand a busy child to its last cross-process user during owner shutdown."""
    server.log_handle.close()
    if server.owner_lease is not None:
        server.owner_lease.release()
        server.owner_lease = None


class ManagedLlamaRuntime:
    """Install and run a private llama-server child process on demand."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings = cfg.local_model
        self.variant = resolve_variant(
            self.settings.runtime_variant, platform.system(), _machine(platform.machine())
        )
        self.asset = runtime_asset(variant=self.variant)
        self.companion = companion_asset(variant=self.variant)
        root = cfg.data_dir / "runtime" / "llama.cpp"
        # The lock is archive-global, not backend-specific: two configured
        # variants still cannot coexist safely on one GPU.
        self.lease_path = root / "managed-server.owner.lock"
        self.usage_lock_path = root / "managed-server.users.lock"
        self.state_lock_path = root / "managed-server.state.lock"
        self.state_path = root / "managed-server.json"
        # Backends live in separate directories: they ship the same executable
        # name and different libraries, so switching in place would leave a
        # half-Vulkan, half-CUDA install. Older installs predate the suffix.
        self.runtime_dir = root / f"{self.settings.runtime_release}-{self.variant}"
        legacy = root / self.settings.runtime_release
        if not self.runtime_dir.exists() and _installed_asset(legacy) == self.asset.name:
            self.runtime_dir = legacy
        filename = Path(urlparse(self.settings.model_url).path).name
        if not filename.casefold().endswith(".gguf"):
            filename = self.settings.model.replace(":", "-") + ".gguf"
        configured = self.settings.model_path
        if configured is None:
            self.model_path = cfg.data_dir / "models" / filename
            self._managed_model = True
        else:
            self.model_path = (
                configured
                if configured.is_absolute()
                else (cfg.base_dir / configured).resolve()
            )
            self._managed_model = False
        self.download_dir = cfg.data_dir / "downloads"

    @property
    def executable(self) -> Path:
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        return self.runtime_dir / name

    @property
    def model_receipt(self) -> Path:
        return self.model_path.with_suffix(self.model_path.suffix + ".sha256")

    def runtime_available(self) -> bool:
        return self.executable.is_file()

    def model_available(self) -> bool:
        if not self.model_path.is_file():
            return False
        if not self._managed_model:
            return True
        try:
            receipt = self.model_receipt.read_text(encoding="ascii").strip()
            size_matches = self.model_path.stat().st_size == self.settings.model_size_bytes
        except OSError:
            return False
        return size_matches and receipt == self.settings.model_sha256

    def available(self) -> bool:
        return self.runtime_available() and self.model_available()

    def status(self) -> LocalRuntimeStatus:
        runtime_ok = self.runtime_available()
        model_ok = self.model_available()
        if runtime_ok and model_ok:
            detail = "ready; starts headlessly on demand"
        elif not runtime_ok and not model_ok:
            detail = f"not installed; {install_hint()}"
        elif not runtime_ok:
            detail = f"model present, runtime missing; {install_hint()}"
        else:
            detail = f"runtime present, model missing; {install_hint()}"
        return LocalRuntimeStatus(
            backend="managed llama.cpp",
            runtime_installed=runtime_ok,
            model_installed=model_ok,
            runtime_path=self.executable,
            model_path=self.model_path,
            detail=detail,
        )

    def _still_to_download(self, destination: Path, size: int) -> int:
        """Of `size` bytes, how many this disk has not already got.

        Downloads resume: a half-finished .part is bytes already paid for, and
        counting them again would refuse an install that only needs the rest.
        """
        for candidate in (destination, destination.with_suffix(destination.suffix + ".part")):
            if candidate.is_file():
                return max(0, size - candidate.stat().st_size)
        return size

    def bytes_still_needed(self) -> int:
        """Room the install has yet to claim, counting what is already on disk."""
        needed = 0
        if not self.runtime_available():
            for asset in (self.asset, self.companion):
                if asset is None:
                    continue
                archive = self.download_dir / asset.name
                # The archive is extracted beside itself and only the extraction
                # survives, but both exist at once, so it is paid for twice.
                needed += self._still_to_download(archive, asset.size) + asset.size
        if self._managed_model and not self.model_available():
            needed += self._still_to_download(self.model_path, self.settings.model_size_bytes)
        return needed

    def install(
        self,
        *,
        dry_run: bool = False,
        on_progress: Progress | None = None,
    ) -> LocalRuntimeStatus:
        if dry_run:
            return self.status()
        # Seven gigabytes of model downloaded onto a disk that cannot hold it
        # ends as a write error partway through a progress bar, having filled
        # the disk the archive itself lives on. The sizes are pinned and the
        # free space is one call away, so ask before starting.
        needed = self.bytes_still_needed()
        free = free_bytes(self.cfg.data_dir)
        if free is not None and needed > free:
            raise RuntimeError(
                f"not enough free space: the install still needs about "
                f"{bytes_label(needed)} and {bytes_label(free)} is free on "
                f"{self.cfg.data_dir}. Free up {bytes_label(needed - free)} and retry"
            )
        self.install_runtime(on_progress=on_progress)
        self.install_model(on_progress=on_progress)
        return self.status()

    def install_runtime(self, *, on_progress: Progress | None = None) -> Path:
        if self.runtime_available():
            return self.executable
        if self.runtime_dir.exists():
            raise RuntimeError(
                f"managed runtime directory is incomplete: {self.runtime_dir}; "
                "move it aside and retry"
            )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        archive = self.download_dir / self.asset.name
        self._download(
            self.asset.url,
            archive,
            self.asset.sha256,
            self.asset.size,
            "runtime",
            on_progress,
        )
        self.runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="llama-stage-", dir=self.runtime_dir.parent))
        try:
            self._extract(archive, stage)
            executable = stage / self.executable.name
            if not executable.is_file():
                raise RuntimeError(
                    f"runtime archive did not contain {self.executable.name}"
                )
            if os.name != "nt":
                executable.chmod(executable.stat().st_mode | 0o111)
            if self.companion is not None:
                companion_archive = self.download_dir / self.companion.name
                self._download(
                    self.companion.url,
                    companion_archive,
                    self.companion.sha256,
                    self.companion.size,
                    "cuda runtime",
                    on_progress,
                )
                # Into the same directory: llama-server.exe loads these by name
                # from its own folder, and PATH is set to it at startup.
                self._extract(companion_archive, stage)
            (stage / "INSTALL.json").write_text(
                json.dumps(
                    {
                        "release": self.settings.runtime_release,
                        "asset": self.asset.name,
                        "sha256": self.asset.sha256,
                        "variant": self.variant,
                        "companion": self.companion.name if self.companion else None,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(stage, self.runtime_dir)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return self.executable

    def install_model(self, *, on_progress: Progress | None = None) -> Path:
        if self.model_available():
            return self.model_path
        if not self._managed_model:
            raise RuntimeError(f"configured local model does not exist: {self.model_path}")
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if self._adopt_legacy_ollama_blob(on_progress=on_progress):
            return self.model_path
        self._download(
            self.settings.model_url,
            self.model_path,
            self.settings.model_sha256,
            self.settings.model_size_bytes,
            "model",
            on_progress,
        )
        self.model_receipt.write_text(
            self.settings.model_sha256 + "\n", encoding="ascii"
        )
        return self.model_path

    def _adopt_legacy_ollama_blob(
        self, *, on_progress: Progress | None = None
    ) -> bool:
        """Reuse exact existing weights during migration; Ollama is not required."""
        legacy = (
            Path.home()
            / ".ollama"
            / "models"
            / "blobs"
            / f"sha256-{self.settings.model_sha256}"
        )
        if not legacy.is_file() or legacy.stat().st_size != self.settings.model_size_bytes:
            return False
        if on_progress:
            on_progress("verifying existing model", 0, self.settings.model_size_bytes)
        if _sha256(legacy) != self.settings.model_sha256:
            return False
        try:
            os.link(legacy, self.model_path)
        except OSError:
            shutil.copy2(legacy, self.model_path)
        self.model_receipt.write_text(
            self.settings.model_sha256 + "\n", encoding="ascii"
        )
        if on_progress:
            on_progress(
                "verifying existing model",
                self.settings.model_size_bytes,
                self.settings.model_size_bytes,
            )
        return True

    @staticmethod
    def _download(
        url: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        phase: str,
        on_progress: Progress | None,
    ) -> None:
        download_verified(url, destination, expected_sha256, expected_size, phase, on_progress)

    @staticmethod
    def _extract(archive: Path, destination: Path) -> None:
        extract_archive(archive, destination)

    def _lease_state(self, token: str, server: _RunningServer) -> dict[str, object]:
        return {
            "version": 1,
            "status": "ready",
            "owner_pid": os.getpid(),
            "server_pid": server.process.pid,
            "base_url": server.base_url,
            "api_key": server.api_key,
            "runtime_dir": str(self.runtime_dir.resolve()),
            "model_path": str(self.model_path.resolve()),
            "serving_shape": self._serving_shape(),
            "token": token,
            "last_used": time.time(),
        }

    def _serving_shape(self) -> str:
        """How the server was told to serve, as one comparable string.

        Slots, window and cache type are fixed when llama-server starts. A
        server already running with different ones answers every request as if
        the configuration had never changed — which is how a raised
        `parallel_slots` can be measured and found to do nothing at all.
        """

        return (
            f"ctx={self.settings.context_window}"
            f":slots={self.settings.parallel_slots}"
            f":kv={self.settings.kv_cache_type}"
            f":layers={self.settings.gpu_layers}"
            f":cram={self.settings.prompt_cache_ram_mib}"
            f":uvm={int(self.settings.cuda_unified_memory)}"
            f":sleep={self.settings.sleep_idle_seconds}"
        )

    def _state_matches(self, state: dict[str, object]) -> bool:
        return (
            state.get("runtime_dir") == str(self.runtime_dir.resolve())
            and state.get("model_path") == str(self.model_path.resolve())
            # A state written before this field existed carries none, and is
            # replaced rather than trusted: it was started by an unknown shape.
            and state.get("serving_shape") == self._serving_shape()
        )

    def _touch_state(self, token: str) -> None:
        lock = _FileLease(self.state_lock_path)
        deadline = time.monotonic() + 1.0
        while not lock.acquire():
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)
        try:
            state = _read_lease_state(self.state_path)
            if state is not None and state.get("token") == token:
                state["last_used"] = time.time()
                # This records when the server was last wanted, which decides
                # only when it is unloaded. Nothing here is worth failing an
                # inference request that has already been paid for, and the
                # unlockable case above returns silently for the same reason.
                with contextlib.suppress(OSError):
                    _write_lease_state(self.state_path, state)
        finally:
            lock.release()

    def _endpoint_from_state(self, state: dict[str, object]) -> ManagedEndpoint | None:
        if state.get("status") != "ready" or not self._state_matches(state):
            return None
        base_url = state.get("base_url")
        api_key = state.get("api_key")
        token = state.get("token")
        server_pid = _state_pid(state, "server_pid")
        if not all(isinstance(value, str) and value for value in (base_url, api_key, token)):
            return None
        return ManagedEndpoint(str(base_url), str(api_key), server_pid, str(token))

    def _held_error(self, state: dict[str, object] | None, detail: str) -> Exception:
        owner_pid = _state_pid(state, "owner_pid")
        holder = f"process {owner_pid}" if owner_pid else "an unknown process"
        configured = ""
        if state is not None and not self._state_matches(state):
            configured = (
                f"; it serves model {state.get('model_path', 'unknown')} with runtime "
                f"{state.get('runtime_dir', 'unknown')}"
            )
        return ManagedServerLeaseHeldError(
            f"managed local server lease is held by {holder}{configured}; {detail}"
        )

    def loaded(self) -> bool:
        """Whether the model is on the card now, asked without starting or waking it.

        A request to a server that is not running, or is asleep, is answered
        only after the model loads, which takes 20 seconds or more.
        """
        state = _read_lease_state(self.state_path)
        endpoint = self._endpoint_from_state(state) if state is not None else None
        if endpoint is None or not _process_alive(endpoint.server_pid):
            return False
        try:
            response = httpx.get(
                f"{endpoint.base_url}/props",
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                # Asked before every judged question; a server too busy to
                # say within this is not going to answer one quickly either.
                timeout=1.0,
            )
            response.raise_for_status()
            return response.json().get("is_sleeping") is False
        except Exception:
            return False

    def _answers_health(self, endpoint: ManagedEndpoint) -> bool:
        """Ask a running server whether it is still serving, patiently.

        This used to be one GET with a one-second timeout, and a server that
        missed it was declared stale -- whereupon adoption refused to start a
        second server and raised, leaving no way back: the same lease was read
        again by the next caller, and the same second elapsed, and every
        remaining call in the process failed identically.  A llama.cpp server
        decoding on all its slots does not always answer a health probe inside
        a second, so the question has to be asked more than once before its
        answer is allowed to end anything.
        """

        deadline = time.monotonic() + ADOPTION_HEALTH_BUDGET_SECONDS
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        while True:
            try:
                response = httpx.get(
                    f"{endpoint.base_url}/health",
                    headers=headers,
                    timeout=ADOPTION_HEALTH_TIMEOUT_SECONDS,
                )
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline or not _process_alive(endpoint.server_pid):
                return False
            time.sleep(0.5)

    def _adopt_or_start(self, owner: _FileLease) -> _RunningServer:
        prior = _read_lease_state(self.state_path)
        prior_endpoint = self._endpoint_from_state(prior) if prior is not None else None
        if prior_endpoint is not None and _process_alive(prior_endpoint.server_pid):
            healthy = self._answers_health(prior_endpoint)
            if healthy:
                log_path = self.cfg.data_dir / "logs" / "local-model.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_handle = log_path.open("ab")
                server = _RunningServer(
                    _PidProcess(prior_endpoint.server_pid),
                    prior_endpoint.base_url,
                    prior_endpoint.api_key,
                    log_handle,
                    log_path,
                    owner_lease=owner,
                    state_path=self.state_path,
                    state_lock_path=self.state_lock_path,
                    usage_lock_path=self.usage_lock_path,
                    lease_token=prior_endpoint.lease_token,
                )
                _write_lease_state(
                    self.state_path,
                    self._lease_state(prior_endpoint.lease_token, server),
                )
                return server
            owner.release()
            raise RuntimeError(
                f"the managed local server (pid {prior_endpoint.server_pid}, "
                f"{prior_endpoint.base_url}) is running but did not answer its "
                f"authenticated health endpoint within "
                f"{ADOPTION_HEALTH_BUDGET_SECONDS:.0f}s; refusing to start a second "
                "server beside it. Stop that process and run the command again."
            )

        # A server whose shape does not match is invisible to the adoption
        # above, and until now the next thing that happened was a second
        # llama-server starting beside it on a card with room for exactly one.
        # That is what the refusal above exists to prevent, and it only fired
        # when the shape matched -- which is the wrong condition, because a
        # shape that has changed is exactly when a second server gets started.
        # Adding `uvm=` to the shape on 2026-09-07 cost three runs to it: the
        # server left over from the previous run read as a different server,
        # a new one was launched, and it died allocating the weights.
        #
        # Stopping it is ours to do: we hold the owner lease, so no other
        # varyz process is using it, and the pid is one this runtime wrote
        # down when it started it. Deliberately narrower than
        # `Reset-LocalModel`, which kills every llama-server on the machine.
        stale_pid = _state_pid(prior, "server_pid")
        if prior is not None and stale_pid > 0 and _same_server(prior, stale_pid):
            log.info(
                "stopping the managed server at pid %d: it serves %s and this run needs %s",
                stale_pid,
                prior.get("serving_shape", "an unrecorded shape"),
                self._serving_shape(),
            )
            stale = _PidProcess(stale_pid)
            with contextlib.suppress(OSError):
                stale.terminate()
            try:
                stale.wait(timeout=30)
            except subprocess.TimeoutExpired:
                owner.release()
                raise RuntimeError(
                    f"the managed local server (pid {stale_pid}) serves a different "
                    f"configuration and did not stop within 30s; starting a second "
                    f"server beside it would run the card out of memory. Stop that "
                    "process and run the command again."
                ) from None
        token = secrets.token_hex(16)
        _write_lease_state(
            self.state_path,
            {
                "version": 1,
                "status": "starting",
                "owner_pid": os.getpid(),
                "runtime_dir": str(self.runtime_dir.resolve()),
                "model_path": str(self.model_path.resolve()),
                "token": token,
                "last_used": time.time(),
            },
        )
        try:
            server = self._start_server()
        except Exception:
            state = _read_lease_state(self.state_path)
            if state is not None and state.get("token") == token:
                self.state_path.unlink(missing_ok=True)
            owner.release()
            raise
        server.owner_lease = owner
        server.state_path = self.state_path
        server.state_lock_path = self.state_lock_path
        server.usage_lock_path = self.usage_lock_path
        server.lease_token = token
        _write_lease_state(self.state_path, self._lease_state(token, server))
        return server

    def _managed_endpoint(self) -> ManagedEndpoint:
        if not self.available():
            raise LocalModelNotInstalled(
                f"Managed local model is not installed; {install_hint()}"
            )
        key = self.runtime_dir.resolve()
        idle = self.settings.idle_unload_seconds
        with _SERVER_LOCK:
            current = _SERVERS.get(key)
            if current is not None and current.process.poll() is None:
                state = _read_lease_state(current.state_path) if current.state_path else None
                if state is not None and not self._state_matches(state):
                    raise self._held_error(state, "the live lease cannot be reused")
                current.last_used = time.monotonic()
                if current.lease_token is not None:
                    self._touch_state(current.lease_token)
                _schedule_idle_unload(idle)
                return ManagedEndpoint(
                    current.base_url,
                    current.api_key,
                    current.process.pid,
                    current.lease_token or "local",
                )
            if current is not None:
                _SERVERS.pop(key, None)
                _terminate(current)

            deadline = time.monotonic() + self.settings.startup_timeout_seconds
            while True:
                owner = _FileLease(self.lease_path)
                if owner.acquire():
                    server = self._adopt_or_start(owner)
                    break
                state = _read_lease_state(self.state_path)
                if state is not None and not self._state_matches(state):
                    raise self._held_error(state, "the live lease cannot be reused")
                remote = self._endpoint_from_state(state) if state is not None else None
                if remote is not None:
                    self._touch_state(remote.lease_token)
                    _schedule_idle_unload(idle)
                    return remote
                if time.monotonic() >= deadline:
                    raise self._held_error(
                        state,
                        "it did not publish a reusable endpoint before the startup deadline",
                    )
                time.sleep(0.05)
            server.last_used = time.monotonic()
            _SERVERS[key] = server
            _schedule_idle_unload(idle)
            return ManagedEndpoint(
                server.base_url,
                server.api_key,
                server.process.pid,
                server.lease_token or "local",
            )

    def endpoint(self) -> tuple[str, str]:
        endpoint = self._managed_endpoint()
        return endpoint.base_url, endpoint.api_key

    def endpoint_alive(self, endpoint: ManagedEndpoint) -> bool:
        """Whether this endpoint's server is still the one we are talking to.

        Asked every 50 ms by the in-flight deadline loop, and a False answer
        aborts the request, so the question has to be answered conservatively:
        the cost of a wrong True is one more poll, and the cost of a wrong False
        is a whole group of episodes thrown away.

        The operating system is the authority on whether the process died. The
        lease file is bookkeeping, and it is rewritten by ``_touch_state`` on
        every request while being read here without a lock — the collision that
        ``_write_lease_state`` calls routine rather than rare. An unreadable
        file is therefore evidence about a rename in progress, not about a
        process we can see running, and only a state we actually read may
        contradict the endpoint.
        """
        if not _process_alive(endpoint.server_pid):
            return False
        state = _read_lease_state(self.state_path)
        if state is None:
            return True
        return (
            state.get("token") == endpoint.lease_token
            and _state_pid(state, "server_pid") == endpoint.server_pid
        )

    def _cleanup_orphan(self, endpoint: ManagedEndpoint) -> None:
        lease = _FileLease(self.lease_path)
        if not lease.acquire():
            return
        usage = _FileLease(self.usage_lock_path)
        if not usage.acquire():
            lease.release()
            return
        owner: _FileLease | None = lease
        try:
            state = _read_lease_state(self.state_path)
            if state is None or state.get("token") != endpoint.lease_token:
                return
            log_path = self.cfg.data_dir / "logs" / "local-model.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            server = _RunningServer(
                _PidProcess(endpoint.server_pid),
                endpoint.base_url,
                endpoint.api_key,
                log_path.open("ab"),
                log_path,
                owner_lease=owner,
                state_path=self.state_path,
                state_lock_path=self.state_lock_path,
                lease_token=endpoint.lease_token,
            )
            owner = None
            _terminate(server)
        finally:
            usage.release()
            if owner is not None:
                owner.release()

    @contextmanager
    def request_endpoint(self) -> Iterator[ManagedEndpoint]:
        """Hold a shared usage lease for the full inference request."""
        for _attempt in range(3):
            endpoint = self._managed_endpoint()
            usage = _FileLease(self.usage_lock_path, shared=True)
            if not usage.acquire():
                time.sleep(0.05)
                continue
            state = _read_lease_state(self.state_path)
            if state is None or state.get("token") != endpoint.lease_token:
                usage.release()
                continue
            self._touch_state(endpoint.lease_token)
            try:
                yield endpoint
            finally:
                self._touch_state(endpoint.lease_token)
                usage.release()
                self._cleanup_orphan(endpoint)
            return
        raise RuntimeError("managed local server changed while acquiring its usage lease")

    def _require_host_memory(self) -> None:
        """Refuse to start when the machine has no physical memory left.

        Unified memory makes the card reach into host RAM, and a server that is
        killed rather than asked to stop leaves what it reached for pinned by
        the driver. Across a night of phases that accumulates until the machine
        is paging and the next phase either crawls or dies -- which reads, from
        inside, as the model being slow, and cost four wrong diagnoses the first
        time it happened. One refused start with the number in it is cheaper.

        The number to watch is free physical memory, not the card's BAR1 window:
        on Windows the display driver maps that window in full at boot, so it
        reports itself nearly exhausted on a machine with 19 GB free and tells
        us nothing.
        """
        floor = self.settings.host_memory_floor_mib
        if floor <= 0:
            return
        memory = host_memory()
        if memory is None or memory.free_mib >= floor:
            return
        if sys.platform == "darwin":
            # The Mac's GPU has no memory of its own: the model loads into this.
            why = (
                "On a Mac the model loads into this same memory; quit what is "
                "using it (Activity Monitor, Memory) or lower the floor."
            )
        else:
            why = (
                "If little of it is claimed by any running process, the display "
                "driver is holding it: unified-memory pages left behind by servers "
                "that were killed rather than stopped, which only a driver reload "
                "or a reboot returns."
            )
        raise RuntimeError(
            f"the machine has {memory.free_mib} MiB of memory free of "
            f"{memory.total_mib}, below the floor a local model needs. {why} "
            f"Set local_model.host_memory_floor_mib = 0 to start anyway."
        )

    def _start_server(self) -> _RunningServer:
        self._require_host_memory()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        api_key = secrets.token_urlsafe(24)
        key_path = self.runtime_dir / f".api-key-{secrets.token_hex(8)}"
        key_path.write_text(api_key + "\n", encoding="ascii")
        with contextlib.suppress(OSError):
            key_path.chmod(0o600)
        log_dir = self.cfg.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "local-model.log"
        log_handle = log_path.open("ab")
        command = [
            str(self.executable),
            "--model",
            str(self.model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # llama-server divides --ctx-size between slots, so the window is
            # multiplied out: raising parallelism must not silently truncate
            # the prompts a single request is allowed to hold.
            "--ctx-size",
            str(self.settings.context_window * self.settings.parallel_slots),
            "--gpu-layers",
            str(self.settings.gpu_layers),
            "--parallel",
            str(self.settings.parallel_slots),
            # Attention caches are the only per-slot cost, so what they are
            # stored as decides how many slots the card holds. Passed always,
            # so the running server's shape can be compared against this.
            "--cache-type-k",
            self.settings.kv_cache_type,
            "--cache-type-v",
            self.settings.kv_cache_type,
            # Saved slot states live in host memory alongside the compiler's own
            # episodes, and the server's default budget for them assumes it has
            # the machine to itself. Passed always: 0 turns the cache off. See
            # `prompt_cache_ram_mib`.
            "--cache-ram",
            str(self.settings.prompt_cache_ram_mib),
            # Unloads the model when nobody is asking, even when no varyz
            # process is left to stop the server. See `sleep_idle_seconds`.
            "--sleep-idle-seconds",
            str(self.settings.sleep_idle_seconds or -1),
            # No ``--spec-type``. Drafting the next few tokens from n-grams
            # already in the window looked made for annotation, where half of
            # every candidate is JSON the grammar leaves no choice about and
            # most of the rest is a quotation copied out of the prompt. It was
            # tried over 780 episodes and the model kept 9.9% of what was
            # drafted -- 5,986 tokens of 60,758 -- which is not a saving but a
            # second forward pass bought for nothing, and calls spent 53%
            # longer writing their answers.
            #
            # It looked promising because it was first watched on a warm card
            # with one request on it, where the model writes at around 60 tokens
            # a second. Under six slots a request gets about 12, because the
            # card is shared. Speculation spends spare arithmetic to save
            # waiting, and a card running six slots has no spare arithmetic to
            # spend -- which is the condition compiling is always in, and never
            # the one a warm single request measures.
            "--no-webui",
            "--api-key-file",
            str(key_path),
            "--jinja",
            "--reasoning",
            "off",
        ]
        environment = os.environ.copy()
        environment["PATH"] = str(self.runtime_dir) + os.pathsep + environment.get("PATH", "")
        # Set here rather than left to whoever launches the compile: the point
        # of it is that a run started by a scheduler at three in the morning
        # survives, and an environment variable someone has to remember is not
        # a setting. See `cuda_unified_memory`.
        if self.settings.cuda_unified_memory:
            environment["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] = "1"
        else:
            environment.pop("GGML_CUDA_ENABLE_UNIFIED_MEMORY", None)
        # On Windows `terminate()` is `TerminateProcess`: the child is stopped
        # between two instructions and never runs the code that frees what it
        # allocated, which is how unified-memory mappings pile up in the card's
        # host window. A child in its own process group can instead be sent the
        # same Ctrl-Break a person would send, and clean up after itself -- but
        # only if it shares our console, so a process launched without one
        # keeps the old flag and the old hard stop. See `_ask_to_stop`.
        askable = os.name == "nt" and _console_attached()
        if os.name != "nt":
            creationflags = 0
        elif askable:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                command,
                cwd=self.runtime_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception:
            log_handle.close()
            with contextlib.suppress(OSError):
                key_path.unlink()
            raise
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        headers = {"Authorization": f"Bearer {api_key}"}
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                response = httpx.get(f"{base_url}/health", headers=headers, timeout=1.0)
                if response.status_code == 200:
                    with contextlib.suppress(OSError):
                        key_path.unlink()
                    return _RunningServer(
                        process,
                        base_url,
                        api_key,
                        log_handle,
                        log_path,
                        askable=askable,
                    )
            except Exception:
                pass
            time.sleep(0.25)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        log_handle.close()
        with contextlib.suppress(OSError):
            key_path.unlink()
        detail = ""
        with contextlib.suppress(OSError):
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"managed llama.cpp failed to start: {detail.strip()}")


def stop_managed_servers() -> None:
    """Stop unused children; hand busy ones to their cross-process users."""
    global _IDLE_TIMER
    with _SERVER_LOCK:
        if _IDLE_TIMER is not None:
            _IDLE_TIMER.cancel()
            _IDLE_TIMER = None
        servers = list(_SERVERS.values())
        _SERVERS.clear()
    for server in servers:
        usage = (
            _FileLease(server.usage_lock_path)
            if server.usage_lock_path is not None
            else None
        )
        if usage is not None and not usage.acquire():
            _detach(server)
            continue
        try:
            _terminate(server)
        finally:
            if usage is not None:
                usage.release()


atexit.register(stop_managed_servers)
