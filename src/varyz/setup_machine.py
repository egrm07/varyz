"""Conservative, read-only assessment; no model is downloaded or loaded.

Admission thresholds leave room for other apps. They are not latency promises.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from varyz.config import Config, ConfigError, load_config

GIB = 1024**3
MODES = ("local", "conversations")


@dataclass(frozen=True)
class Machine:
    system: str
    architecture: str
    cores: int
    total_memory: int | None
    available_memory: int | None
    free_disk: int | None
    gpu_used_mib: int | None = None
    gpu_total_mib: int | None = None


@dataclass(frozen=True)
class Assessment:
    comfortable: bool
    reasons: tuple[str, ...]
    machine: Machine

    def view(self) -> dict:
        return asdict(self)


def _output(args: list[str]) -> str:
    return subprocess.check_output(
        args, text=True, timeout=3, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def memory(system: str) -> tuple[int | None, int | None]:
    try:
        if system == "Windows":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page_total", "page_available",
                     "virtual_total", "virtual_available", "extended")
                ]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total), int(status.available)
        elif system == "Darwin":
            total = int(_output(["sysctl", "-n", "hw.memsize"]).strip())
            stats = _output(["vm_stat"])
            matched = re.search(r"page size of (\d+) bytes", stats)
            if matched is None:
                return None, None
            page_size = int(matched[1])
            counts = dict(re.findall(r"^(Pages [\w ]+):\s+(\d+)\.", stats, re.M))
            available = sum(int(counts[name]) for name in
                            ("Pages free", "Pages inactive", "Pages speculative")) * page_size
            return total, available
        elif system == "Linux":
            linux_stats = dict(re.findall(
                r"^(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M))
            return int(linux_stats["MemTotal"]) * 1024, int(linux_stats["MemAvailable"]) * 1024
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        pass
    return None, None


def _existing_parent(path: Path) -> Path:
    path = path.expanduser().resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def setup_config(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError:
        if path.exists():
            raise
        return Config(base_dir=path.parent)


def inspect_machine(config_path: Path) -> Machine:
    from varyz.resolution.local_classifier import gpu_memory_mib

    system = platform.system()
    total, available = memory(system)
    cfg = setup_config(config_path)
    destinations = [cfg.data_dir, Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama"))]
    try:
        free = min(shutil.disk_usage(_existing_parent(path)).free for path in destinations)
    except OSError:
        free = None
    gpu = gpu_memory_mib()
    return Machine(system, platform.machine().lower(), os.cpu_count() or 0,
                   total, available, free, *(gpu or (None, None)))


def assess(machine: Machine) -> Assessment:
    reasons = []
    if machine.total_memory is None or machine.total_memory < 15 * GIB:
        reasons.append("At least 16 GB of memory is recommended for Qwen alongside your apps.")
    # Qwen, the search models, runtime buffers and room for other apps.
    if machine.available_memory is None or machine.available_memory < 8 * GIB:
        if machine.available_memory is None:
            available = "Available memory could not be checked."
        else:
            available = f"Only {machine.available_memory / GIB:.1f} GB of memory is available now."
        reasons.append(f"{available} Local learning needs at least 8 GB free.")
    if machine.free_disk is None or machine.free_disk < 12 * GIB:
        reasons.append("Keep at least 12 GB free for the model, runtime and working space.")
    if machine.cores < 4:
        reasons.append("At least four logical CPU cores are recommended.")
    apple_gpu = machine.system == "Darwin" and machine.architecture in {"arm64", "aarch64"}
    nvidia_gpu = (
        machine.gpu_total_mib is not None and machine.gpu_used_mib is not None
        and machine.gpu_total_mib - machine.gpu_used_mib - 4608 >= machine.gpu_total_mib * 0.3
    )
    if not apple_gpu and not nvidia_gpu:
        reasons.append("No supported GPU with comfortable model headroom was detected.")
    return Assessment(not reasons, tuple(reasons), machine)


def plan(path: Path) -> dict:
    assessment = assess(inspect_machine(path))
    settings = setup_config(path).workflows
    saved = settings.inference_mode
    selected = saved if saved in MODES else ("local" if assessment.comfortable else "conversations")
    if not assessment.comfortable:
        selected = "conversations"
    return {
        **assessment.view(), "selected": selected,
        "cloud_conversations": settings.rule_reader_cloud and saved == "conversations",
        "choices": list(MODES) if assessment.comfortable else ["conversations"],
    }
