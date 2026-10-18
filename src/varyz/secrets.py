"""Secrets live in the OS credential store (Windows Credential Manager),
never in config files or the repo."""

from __future__ import annotations

import contextlib
import os

import keyring

from varyz.legacy import LEGACY_KEYRING_SERVICE

SERVICE = "varyz"
# All three, not one: the credential store is the normal home for a key, but a
# machine that already exports the provider's own standard variable has answered
# the question `varyz connect` asks, and covering only Anthropic made that true
# for one provider by accident rather than by decision.
ENV_FALLBACKS = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
}

# Every fixed name varyz ever reads. `varyz secrets set` took any string, so a
# key typed under "anthropic", or under the environment variable's own name,
# went into Windows Credential Manager and nothing ever looked for it -- and
# `varyz secrets show` then reported it present, which reads as connected.
KNOWN_SECRETS = (
    "anthropic_api_key",
    "openai_api_key",
    "deepseek_api_key",
    "claude_oauth_tokens",
    "openai_oauth_tokens",
    "google_oauth_client_json",
)

# The rest carry an account or a label after the colon, and are written by
# `varyz connect`, not by hand.
KNOWN_SECRET_PREFIXES = (
    "cloud:",
    "calendar_subscription_url:",
    "github_token:",
    "gmail_app_password:",
    "google_oauth:",
)


def is_known_secret(name: str) -> bool:
    """Whether anything in varyz would ever read a secret under this name."""
    return name in KNOWN_SECRETS or any(
        name.startswith(prefix) and len(name) > len(prefix)
        for prefix in KNOWN_SECRET_PREFIXES
    )


def suggest_secret(name: str) -> str | None:
    """The name a near miss was reaching for, or None when it is anyone's guess."""
    import difflib

    lowered = name.strip().lower()
    for known in KNOWN_SECRETS:
        aliases = {known, known.removesuffix("_api_key")}
        env = ENV_FALLBACKS.get(known)
        if env:
            aliases.add(env.lower())
        if lowered in aliases:
            return known
    close = difflib.get_close_matches(lowered, KNOWN_SECRETS, n=1, cutoff=0.6)
    return close[0] if close else None


def _stored(name: str) -> str | None:
    """The key in the credential store, moving it from the pre-rename service.

    Keys set before 2026-09-24 sit under the service "varys". The first read
    copies one across and deletes the old entry only once the copy reads back.
    A store that refuses the write still answers with the old value.
    """
    value = keyring.get_password(SERVICE, name)
    if value:
        return value
    legacy = keyring.get_password(LEGACY_KEYRING_SERVICE, name)
    if not legacy:
        return None
    with contextlib.suppress(keyring.errors.KeyringError):
        keyring.set_password(SERVICE, name, legacy)
        if keyring.get_password(SERVICE, name) == legacy:
            keyring.delete_password(LEGACY_KEYRING_SERVICE, name)
    return legacy


def get_secret(name: str) -> str | None:
    value = _stored(name)
    if value:
        return value
    env = ENV_FALLBACKS.get(name)
    return os.environ.get(env) if env else None


def secret_source(name: str) -> str | None:
    """Where the key varyz would use actually comes from, or None for nothing.

    "connected" is a different statement depending on the answer: a key in the
    credential store stays until it is deleted, one read from the environment
    lasts as long as the shell that exported it, and a status line that cannot
    tell them apart says a machine is set up when closing the terminal undoes it.
    """
    if _stored(name):
        return "store"
    env = ENV_FALLBACKS.get(name)
    if env and os.environ.get(env):
        return "env"
    return None


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    for service in (SERVICE, LEGACY_KEYRING_SERVICE):
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, name)
