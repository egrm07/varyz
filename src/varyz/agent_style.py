"""How much of what varyz returns the agent should put on the screen.

An agent reading the routing skill and the handshake instructions does what
they say, and what they said was "cite the source references it returns". The
result, reported by the first person to watch another agent use varyz, was a
wall of text with a citation on every line for a question that wanted three
sentences. The instructions were written against a grading rubric that
rewarded completeness; nobody reads a rubric's output by choice.

So presentation is the owner's setting, and the default is short. What is not
a setting: the answer still rests only on returned evidence, an excerpt is
still never passed off as the agent's own conclusion, and a question the
archive cannot support is still refused. `off` hides references; it does not
license invention.

This module is stdlib-only and imports nothing from varyz: it is read during
the MCP handshake, which is measured, and by `varyz install`, which
deliberately knows nothing about the config model.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

DEFAULT = "minimal"
STYLES = ("minimal", "full", "off")

# Rendered into the routing skill between the citation markers.
_SKILL = {
    "minimal": (
        "Answer the question first, in as few sentences as it takes. Cite a reference only\n"
        "where a claim rests on one, in line with that claim, and cite the record carrying it\n"
        "rather than every excerpt that mentioned it. Do not reprint the evidence as a list or\n"
        "summarise records the answer did not use. Say what else is there - \"nine more\n"
        "messages in that thread\" - and offer it, rather than printing it unasked."
    ),
    "full": (
        "Cite a reference for every excerpt the answer draws on, and show that evidence under\n"
        "the answer: date, source, reference, and the quoted words. Where the account and its\n"
        "completeness compete, choose completeness."
    ),
    "off": (
        "Do not print references unless the user asks for them, and keep the answer short.\n"
        "Name the source for any part of it the moment they ask. This changes what is shown\n"
        "and nothing else: the answer still rests only on what varyz returned, an excerpt is\n"
        "still never presented as your own conclusion, and an unsupported question is still\n"
        "refused."
    ),
}

# Appended to the MCP handshake instructions, for agents that read no skill file.
_INSTRUCTION = {
    "minimal": (
        "The user wants short answers: lead with the answer, cite a reference only where a "
        "claim rests on one, and do not reprint the evidence - say what else is there and "
        "offer it."
    ),
    "full": (
        "The user wants the evidence shown: cite a reference for every excerpt the answer "
        "uses, and quote it under the answer."
    ),
    "off": (
        "The user does not want references printed. Answer briefly and name a source when "
        "asked. The answer must still rest on returned evidence, and an unsupported question "
        "is still refused."
    ),
}


def normalise(value: object) -> str:
    """The configured style, or the default for anything unrecognised."""
    return value if isinstance(value, str) and value in STYLES else DEFAULT


def skill_paragraph(style: str) -> str:
    return _SKILL[normalise(style)]


def instruction(style: str) -> str:
    return _INSTRUCTION[normalise(style)]


def stored_style(config_path: Path | None) -> str | None:
    """The style written in config.toml, or None where nobody has said.

    The difference matters to setup alone, which asks the question once and
    then never again. Everywhere else an unanswered setting and an unreadable
    one mean the same thing, which is `read_style`.
    """
    if config_path is None or not config_path.exists():
        return None
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return None
    section = data.get("agent")
    if not isinstance(section, dict) or "citations" not in section:
        return None
    return normalise(section.get("citations"))


def read_style(config_path: Path | None) -> str:
    """Read `[agent] citations` without loading the config model.

    Every failure — no file, no section, unreadable TOML — is the default.
    This is read on paths that must not fail for a preference: a handshake
    that raises leaves the agent with no varyz at all.
    """
    stored = stored_style(config_path)
    return DEFAULT if stored is None else stored
