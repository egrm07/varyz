"""Canonical scope labels shared by local records and the hosted service.

Only complete, explicit general labels become '*'. Other names remain project
names; no fuzzy matching or inference may widen a project's preferences.
"""

from __future__ import annotations

import re
import unicodedata

GLOBAL_SCOPE = "*"
_GLOBAL_ALIASES = frozenset({
    "", "*", "all", "any", "every", "global", "everywhere", "general", "universal",
})
_GLOBAL_PROJECT_LABEL = re.compile(
    r"(?:all|any|every|each)(?: of)?(?: my| the)? "
    r"(?:projects?|repos?|repositor(?:y|ies)|codebases?)"
)
_FOREIGN_PREFIXES = ("org:", "published:")
_FOREIGN_ID_RESERVED = frozenset({"", "*", "all", "any", "every", "global", "everywhere"})
_VERSION_TAIL = re.compile(
    r"^(?P<base>.*\w)[-_ ]v\d+(?:\.\d+)*$|^(?P<name>.*[^\W\d_]\d+)(?:\.\d+)+$"
)
_CHECKOUT_TAIL = re.compile(r"(?:-(?:main|master))+$")


def _general_label(value: str) -> bool:
    # Separators are equivalent only for reserved labels, not for project names.
    label = " ".join(re.split(r"[\s_-]+", value))
    return label in _GLOBAL_ALIASES or _GLOBAL_PROJECT_LABEL.fullmatch(label) is not None


def checkout_scope(scope: str) -> str:
    """Fold default-branch download names; preserve exact report version identities."""
    value = " ".join(unicodedata.normalize("NFKC", scope).casefold().split())
    if value.startswith(_FOREIGN_PREFIXES):
        return value
    base = _CHECKOUT_TAIL.sub("", value)
    # A project alias must never acquire General's wider scope.
    return base if base and not _general_label(base) else value


def normalize_scope(scope: str | None) -> str:
    """General '*', a foreign origin with its id, or a project without its version."""
    value = " ".join(unicodedata.normalize("NFKC", scope or "").casefold().split())
    for prefix in _FOREIGN_PREFIXES:
        if value.startswith(prefix):
            identity = value[len(prefix):].strip()
            # General labels describe owner scope, not foreign organisation IDs.
            if identity in _FOREIGN_ID_RESERVED:
                raise ValueError(f"scope {prefix}<id> needs an id")
            return prefix + identity
    if _general_label(value):
        return GLOBAL_SCOPE
    while True:
        canonical = checkout_scope(value)
        version = _VERSION_TAIL.match(canonical)
        if version:
            canonical = version.group("base") or version.group("name")
        if canonical == value or _general_label(canonical):
            return value
        value = canonical


def stored_scope(scope: str | None) -> str:
    """Read legacy scopes canonically without rewriting append-only records."""
    try:
        return normalize_scope(scope)
    except ValueError:
        # Preserve malformed foreign scopes as foreign, never promote to general.
        value = " ".join(unicodedata.normalize("NFKC", scope or "").casefold().split())
        return value or GLOBAL_SCOPE
