"""Finding credentials in text the user is about to be shown or send.

An archive built from agent transcripts and private messages contains, with
certainty, things that are not evidence about a person: an API key pasted into
a chat to debug it, a `.env` line quoted in a message, a recovery code sent to
somebody. Varyz has no business displaying those back, and less business
sending them to a cloud model.

The contract is the one the release plan states: **show the detected type and
source while withholding the value.** A quote that silently vanished would make
the Mirror look like it had less evidence than it does; a quote with
`[AWS access key removed]` in it is honest about both the evidence and the
redaction.

Two deliberate limits on what this is.

It is **pattern-based and conservative**. Every pattern here matches a format
that is issued by one identifiable system and is not plausible prose — a
prefixed key, a PEM header, a JWT's three base64url segments. There is no
entropy heuristic, because the false positives land on exactly the material
this product exists to read: identifiers, hashes, and foreign-language text.
A missed secret is bad; a Mirror that redacts a third of somebody's messages is
useless, and the user cannot tell it is happening.

It is also the egress hard block. The broker scans every transformed item and
then the complete JSON-shaped payload, including prompt fields. Inputs beyond
the bounded scanner contract are withheld whole instead of partially scanned.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

MAX_SCAN_CHARS = 1_000_000


@dataclass(frozen=True)
class SecretKind:
    name: str
    pattern: re.Pattern[str]
    # Which capture group holds the value. 0 means the whole match; a named
    # group lets a pattern keep its context ("password=") and redact only what
    # follows it.
    group: int | str = 0


# Ordered longest-context-first: a PEM block contains base64 that would
# otherwise be nibbled at by narrower patterns.
KINDS: tuple[SecretKind, ...] = (
    SecretKind(
        "private key",
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    SecretKind("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    SecretKind("OpenAI API key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    SecretKind("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    SecretKind("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    SecretKind("Slack token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    SecretKind("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    SecretKind("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretKind("Stripe key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    SecretKind(
        "obfuscated API key",
        re.compile(
            r"(?:sk-ant-|sk-(?:proj-)?|github_pat_|gh[pousr]_)"
            r"[A-Za-z0-9_-]{0,20}(?:(?:\r?\n|[\"'`\\])+"
            r"[A-Za-z0-9_-]{12,})+"
        ),
    ),
    SecretKind(
        "JSON web token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    SecretKind(
        "bearer token",
        re.compile(r"(?i)\bbearer\s+(?P<v>[A-Za-z0-9._~+/=-]{20,})"),
        group="v",
    ),
    SecretKind(
        "credentials in a URL",
        # scheme://user:secret@host — the password, not the username.
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:@/]+:(?P<v>[^\s@/]{3,})@"),
        group="v",
    ),
    SecretKind(
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|credential|token|private[_ -]?key|"
            r"api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"recovery[_-]?code)\b\s*(?::|=|\bis\b|\bcalled\b)\s*"
            r"[\"'](?P<v>[^\r\n\"']{4,})[\"']"
        ),
        group="v",
    ),
    SecretKind(
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|credential|token|private[_ -]?key|"
            r"api[_-]?key|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|recovery[_-]?code)\b"
            r"\s*(?::|=|\bis\b|\bcalled\b)\s*[\"']?(?P<v>[^\s\"',;]{6,})",
        ),
        group="v",
    ),
)

# Long digit runs are only redacted when they pass Luhn, so order numbers,
# phone numbers, and ids stay readable.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?P<v>(?:[A-Za-z0-9+/]{4}[ \t\r\n]*){6,}"
    r"(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?)(?![A-Za-z0-9+/])"
)


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _raw_findings(text: str) -> list[Finding]:
    found: list[Finding] = []
    for kind in KINDS:
        for match in kind.pattern.finditer(text):
            start, end = match.span(kind.group)
            # An optional group that did not participate spans (-1, -1).
            if start < 0:
                continue
            found.append(Finding(kind.name, start, end))

    for match in re.finditer(r"-----BEGIN[ A-Z]*PRIVATE KEY-----", text):
        covered = any(
            item.kind == "private key" and item.start <= match.start() < item.end
            for item in found
        )
        if not covered:
            found.append(Finding("malformed private key", match.start(), len(text)))

    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append(Finding("payment card number", *match.span(0)))
    for match in _BASE64_RE.finditer(text):
        encoded = re.sub(r"\s", "", match.group("v"))
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if _raw_findings(decoded):
            found.append(Finding("base64-encoded credential", *match.span("v")))
    return found


def find(text: str) -> tuple[Finding, ...]:
    """Locate credential values, keeping the earliest of any overlap."""
    if len(text) > MAX_SCAN_CHARS:
        return (Finding("oversized unscanned content", 0, len(text)),)

    found = _raw_findings(text)
    variants = {
        unicodedata.normalize("NFKC", text),
        html.unescape(text),
        unquote(text),
    }
    if any(value != text and _raw_findings(value) for value in variants):
        # Normalization changes offsets. Withhold the whole value instead of
        # risking a redaction against the wrong bytes.
        found.append(Finding("normalization-obfuscated credential", 0, len(text)))

    found.sort(key=lambda item: (item.start, -(item.end - item.start)))
    kept: list[Finding] = []
    reached = -1
    for item in found:
        if item.start >= reached:
            kept.append(item)
            reached = item.end
    return tuple(kept)


def redact(text: str) -> tuple[str, tuple[str, ...]]:
    """Replace credential values with a named placeholder.

    Returns the safe text and the kinds removed, so a caller can tell the user
    *that* something was withheld and *what sort of thing it was* without ever
    putting the value on screen.
    """
    findings = find(text)
    if not findings:
        return text, ()
    out: list[str] = []
    cursor = 0
    for item in findings:
        out.append(text[cursor:item.start])
        out.append(f"[{item.kind} removed]")
        cursor = item.end
    out.append(text[cursor:])
    # dict.fromkeys: report each kind once, in the order it first appeared.
    return "".join(out), tuple(dict.fromkeys(item.kind for item in findings))


def redact_tree(value: Any) -> tuple[Any, tuple[str, ...]]:
    """Redact every string anywhere inside a JSON-shaped result.

    The MCP capsules hand a nested dict straight to a cloud agent, and a
    credential could sit in any excerpt, title, or quote in it. Walking the
    whole structure is the only version of this that cannot be defeated by
    adding a field later. Scanning identifiers as well as prose is safe because
    `find` is deliberately conservative — entry ids are ULIDs, commit refs are
    git SHAs, and both are among its tested negative cases.
    """
    if isinstance(value, str):
        return redact(value)
    kinds: list[str] = []
    if isinstance(value, dict):
        out_map = {}
        for key, item in value.items():
            out_map[key], found = redact_tree(item)
            kinds.extend(found)
        return out_map, tuple(dict.fromkeys(kinds))
    if isinstance(value, (list, tuple)):
        out_seq = []
        for item in value:
            cleaned, found = redact_tree(item)
            out_seq.append(cleaned)
            kinds.extend(found)
        return type(value)(out_seq), tuple(dict.fromkeys(kinds))
    return value, ()
