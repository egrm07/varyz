"""Find an existing Codex runtime without changing the user's shell or installing software."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _native(path: Path) -> Path | None:
    """Prefer npm's native binary to a shim that needs Node on PATH."""
    resolved = path.resolve()
    roots = [path.parent / "node_modules" / "@openai" / "codex"]
    if resolved.parent.name == "bin":
        roots.insert(0, resolved.parent.parent)
    system = {"darwin": "apple-darwin", "win32": "pc-windows-msvc"}.get(sys.platform, "linux")
    arch = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    name = "codex.exe" if sys.platform == "win32" else "codex"
    for root in roots:
        patterns = [f"vendor/{arch}-*{system}*/codex/{name}",
                    f"vendor/{arch}-*{system}*/bin/{name}",
                    f"node_modules/@openai/codex-*/vendor/{arch}-*{system}*/bin/{name}",
                    f"node_modules/@openai/codex-*/vendor/{arch}-*{system}*/codex/{name}"]
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate
    if sys.platform == "win32" and path.suffix.lower() != ".exe":
        return None
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _version(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(path)))


def find_codex() -> str | None:
    """Look in PATH, known installations, editor extensions, then macOS app metadata."""
    home = Path.home()
    override = os.environ.get("VARYZ_CODEX_PATH")
    found = shutil.which("codex")
    candidates = [Path(p) for p in (override, found) if p]
    candidates += [home / ".local/bin/codex", home / ".cargo/bin/codex",
                   home / ".npm-global/bin/codex", home / ".volta/bin/codex"]
    if sys.platform == "darwin":
        candidates += [root / "Codex.app/Contents/Resources/codex"
                       for root in [Path("/Applications"), home / "Applications"]]
        candidates += [Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
    if sys.platform == "win32":
        for variable, relative in [("APPDATA", "npm/codex.cmd"),
                                   ("LOCALAPPDATA", "Programs/Codex/resources/codex.exe")]:
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / relative)
    for pattern in [".nvm/versions/node/*/bin/codex",
                    ".local/share/fnm/node-versions/*/installation/bin/codex"]:
        candidates += sorted(home.glob(pattern), key=_version, reverse=True)[:20]
    arch = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    name = "codex.exe" if sys.platform == "win32" else "codex"
    for folder in [".vscode", ".vscode-insiders", ".vscode-oss", ".cursor"]:
        candidates += sorted(home.glob(
            f"{folder}/extensions/openai.chatgpt-*/bin/{system}-{arch}/{name}"
        ), key=_version, reverse=True)[:20]
    for candidate in candidates:
        if executable := _native(candidate):
            return str(executable)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/mdfind", 'kMDItemFSName == "Codex.app" && '
                 'kMDItemContentType == "com.apple.application-bundle"'],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for app in result.stdout.splitlines()[:10] if result.returncode == 0 else []:
                if executable := _native(Path(app) / "Contents/Resources/codex"):
                    return str(executable)
        except (OSError, subprocess.SubprocessError):
            pass
    return None
