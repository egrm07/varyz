"""Small, safe edits to config.toml shared by the CLI and the web UI.

We rewrite only the specific lines we own (the [llm] provider, a connector
section) and never reformat the rest of the user's file.

Every write here replaces the whole file, so every write goes through
``_write``: config.toml is the file that says where the archive is and
which sources are connected, and there is no version of it that is safe
to leave half-written.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from varyz.filesystem import atomic_replace


def _write(cfg_path: Path, text: str) -> None:
    """Replace config.toml in one step, never in place.

    write_text truncates the file and then writes it. A crash, a full disk
    or a Ctrl-C in between left the user with a config.toml cut off
    mid-section -- which is to say with no archive, no sources and no
    command that would load it, after doing nothing more dangerous than
    pausing a source in the trust centre.
    """
    temporary = cfg_path.with_name(f"{cfg_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        atomic_replace(temporary, cfg_path)
    finally:
        temporary.unlink(missing_ok=True)


def set_provider(cfg_path: Path, provider: str) -> None:
    """Point [llm] provider at a connected account, creating the key/section."""
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    if re.search(r"(?m)^[ \t]*provider\s*=", text):
        text = re.sub(
            r'(?m)^[ \t]*provider[ \t]*=[ \t]*"[^"]*"', f'provider = "{provider}"', text
        )
    elif re.search(r"(?m)^\[llm\]", text):
        text = re.sub(
            r"(?m)^\[llm\][ \t]*$",
            f'[llm]\nprovider = "{provider}"',
            text,
            count=1,
        )
    else:
        text += f'\n[llm]\nprovider = "{provider}"\n'
    _write(cfg_path, text)


def set_agent_citations(cfg_path: Path, value: str) -> None:
    """Record how much evidence agents should show, creating [agent] if needed.

    The MCP handshake reads this at every launch, so a restarted client needs
    nothing else; `varyz install` also renders it into any routing skill it
    installs, which a Codex session then picks up at its next start.
    """
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    assignment = re.compile(r'(?m)^[ \t]*citations[ \t]*=[ \t]*"[^"]*"')
    header = re.search(r"(?m)^[ \t]*\[agent\][ \t]*(?:#.*)?$", text)
    if header is None:
        prefix = "" if not text or text.endswith("\n\n") else "\n"
        _write(cfg_path, text + prefix + f'[agent]\ncitations = "{value}"\n')
        return
    # Only this section's body: a `citations` line under some other header
    # belongs to whoever wrote it. `[ \t]`, not `\s`, for the reason given in
    # `upsert_connector` — `^\s*` matches into the preceding blank line.
    body_start = text.find("\n", header.start())
    if body_start < 0:
        text += "\n"
        body_start = len(text) - 1
    following = re.search(r"(?m)^[ \t]*\[", text[body_start + 1 :])
    end = body_start + 1 + following.start() if following else len(text)
    body = text[body_start + 1 : end]
    if assignment.search(body):
        body = assignment.sub(f'citations = "{value}"', body, count=1)
    else:
        body = f'citations = "{value}"\n' + body
    _write(cfg_path, text[: body_start + 1] + body + text[end:])


def _set_in_section(cfg_path: Path, section: str, key: str, value: str, old: str) -> None:
    """Set `key = value` in `[section]`, adding whatever is missing.

    ``old`` matches the value being replaced, so a comment after it stays.
    """
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    _write(cfg_path, _replace_in_section(text, section, key, value, old))


def _replace_in_section(text: str, section: str, key: str, value: str, old: str) -> str:
    """Pure text edit so a multi-setting consent change can be published atomically."""
    line = f"{key} = {value}"
    assignment = re.compile(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*(?:{old})")
    header = re.search(rf"(?m)^[ \t]*\[{re.escape(section)}\][ \t]*(?:#.*)?$", text)
    if header is None:
        prefix = "" if not text or text.endswith("\n\n") else "\n"
        return text + prefix + f"[{section}]\n{line}\n"
    body_start = text.find("\n", header.start())
    if body_start < 0:
        text += "\n"
        body_start = len(text) - 1
    following = re.search(r"(?m)^[ \t]*\[", text[body_start + 1 :])
    end = body_start + 1 + following.start() if following else len(text)
    body = text[body_start + 1 : end]
    body = (
        assignment.sub(line, body, count=1) if assignment.search(body) else f"{line}\n{body}"
    )
    return text[: body_start + 1] + body + text[end:]


def set_resolution_port(cfg_path: Path, port: int) -> None:
    """Give this archive's daemon its own port, creating [resolution] if needed.

    Every config starts on the default port, and a second archive on the same
    machine would otherwise be answered by the first one's daemon.
    """
    _set_in_section(cfg_path, "resolution", "port", str(port), r"\d+")


def set_graph_extractor(cfg_path: Path, extractor: str) -> None:
    """Set `[graph] extractor`, creating the section or the line if needed.

    Setup writes it when the config names an extractor this build cannot run:
    the example config names the semantic one, and the alpha refuses it.
    """
    _set_in_section(
        cfg_path, "graph", "extractor", json.dumps(extractor), r"\"[^\"\n]*\"|'[^'\n]*'"
    )


def set_rule_reader_cloud(cfg_path: Path, allowed: bool) -> None:
    """Set `[workflows] rule_reader_cloud`: the owner's answer to `rules mine --cloud`."""
    _set_in_section(
        cfg_path, "workflows", "rule_reader_cloud", "true" if allowed else "false",
        r"true|false",
    )


def set_classifier_url(cfg_path: Path, url: str) -> None:
    """Set `[workflows] classifier_url`: setup moves it when an older Ollama holds the port."""
    _set_in_section(
        cfg_path, "workflows", "classifier_url", json.dumps(url), r"\"[^\"\n]*\"|'[^'\n]*'"
    )


def set_inference_mode(cfg_path: Path, mode: str, *, cloud_conversations: bool) -> None:
    """Persist setup's mode and consent without widening any source approvals."""
    if mode not in {"local", "conversations"}:
        raise ValueError("inference mode must be local or conversations")
    local = mode == "local"
    cloud = cloud_conversations and not local
    values = {
        "inference_mode": mode,
        "task_classifier": "model" if local else "embedding",
        "redo_classifier": "model" if local else "rule",
        "prompt_classifier": "model" if local else "rule",
        "scope_classifier": "model" if local else "rule",
        "paraphrase_verifier": "model" if local else "off",
        "evidence_verifier": "model" if local else "off",
        "rule_miner": "model" if local or cloud else "lexical",
        "rule_reader_cloud": cloud,
        "rule_reader_agent": cloud,
        "live_reading": cloud,
    }
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    for key, value in values.items():
        encoded = json.dumps(value)
        text = _replace_in_section(text, "workflows", key, encoded,
                                   r'"[^"\n]*"|\x27[^\x27\n]*\x27|true|false')
    _write(cfg_path, text)


def ensure_section(cfg_path: Path, section: str, content: str) -> bool:
    """Append a [connectors.<section>] block unless it already exists.
    Returns True if it was added."""
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    header = re.compile(
        rf"(?m)^[ \t]*\[connectors\.{re.escape(section)}\][ \t]*(?:#.*)?$"
    )
    if header.search(text):
        return False
    block = content if text.endswith("\n\n") or not text else "\n" + content
    _write(cfg_path, text + block)
    return True


def _toml_value(value: Any) -> str:
    """Encode the small TOML value subset connector settings use."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported connector setting: {type(value).__name__}")


def upsert_connector(cfg_path: Path, section: str, values: dict[str, Any]) -> None:
    """Create or update owned keys in one connector section.

    Unknown keys and comments are preserved.  This keeps web onboarding safe
    for hand-tuned configs while allowing reconnects to update an account or
    approved paths.
    """
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    header = f"[connectors.{section}]"
    # `[ \t]`, never `\s`: in multiline mode `^\s*` happily matches at the start
    # of the blank line above the header and swallows the newline, so the match
    # begins a line early, the section body measures as empty, and the new keys
    # are written above the header -- into whatever section came before. That
    # turned a second `varyz connect` (or one trust toggle) into a config file
    # no varyz command would load again.
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(header)}[ \t]*(?:#.*)?$",
        text,
    )
    if match is None:
        prefix = "" if not text or text.endswith("\n\n") else "\n"
        lines = [header, *(f"{key} = {_toml_value(value)}" for key, value in values.items())]
        _write(cfg_path, text + prefix + "\n".join(lines) + "\n")
        return

    start = match.start()
    body_start = text.find("\n", start)
    if body_start < 0:
        body_start = len(text)
        text += "\n"
    next_section = re.search(r"(?m)^\[", text[body_start + 1 :])
    end = (
        body_start + 1 + next_section.start()
        if next_section
        else len(text)
    )
    body = text[body_start + 1 : end]
    for key, value in values.items():
        encoded = _toml_value(value)
        pattern = re.compile(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*$")
        if pattern.search(body):
            # A callable replacement preserves JSON/TOML backslashes literally.
            # Passing a Windows path as a replacement string would otherwise
            # reinterpret ``\\`` and corrupt sequences such as ``\Users``.
            body = pattern.sub(
                lambda _match, key=key, encoded=encoded: f"{key} = {encoded}",
                body,
                count=1,
            )
        else:
            if body and not body.endswith("\n"):
                body += "\n"
            body += f"{key} = {encoded}\n"
    _write(cfg_path, text[: body_start + 1] + body + text[end:])


def remove_connector(cfg_path: Path, section: str) -> None:
    """Remove one connector section while leaving the rest of TOML untouched."""
    if not cfg_path.exists():
        return
    text = cfg_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?ms)^\[connectors\.{re.escape(section)}\]\s*\n.*?(?=^\[|\Z)"
    )
    _write(cfg_path, pattern.sub("", text, count=1))
