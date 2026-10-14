"""The owner's turns a model may read, and the models that read them.

Most turns to a coding agent are requests for the task in hand; what the owner
wants every time shows across many of them, and telling the two apart takes a
model that reads them (``profile_reader`` gives it the tasks). This module
picks the turns -- the owner's own, never sensitive ones, and for a model off
the machine only those classified public or personal -- and the model: the
local one the classifiers use (``[workflows] classifier_model``, through Ollama;
nothing leaves the machine), or the cloud one ``[llm] provider`` names,
through the sign-in ``varyz connect`` made. The cloud one reads first unless the
owner keeps their turns on this machine (``[workflows] rule_reader_cloud``).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from varyz.domain import Sensitivity
from varyz.resolution.rule_miner import (
    MIN_WORDS,
    MiningStats,
    _turn_scope,
)

REQUEST_TIMEOUT_SECONDS = 300.0
# A long turn that opens the same way in several sessions is a prompt the
# owner pasted or a tool filled in (Claude Code's /doctor "Help me fix these
# issues" is 75-121 words): not their own words about how they work.
PASTED_MIN_WORDS = 25
PASTED_OPENING_WORDS = 12


class ReaderUnavailable(RuntimeError):
    """This model cannot read turns now; the caller tries the next reader."""


class Completer(Protocol):
    def complete(self, system: str, user: str) -> str: ...


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Turn:
    entry_id: str
    content_hash: str
    thread_id: str | None
    occurred: str | None
    scope: str
    text: str
    # The end of the agent's reply this turn answers: context, never the owner's words.
    after: str = ""


def owner_turns(conn: sqlite3.Connection, stats: MiningStats, *, cloud: bool) -> list[Turn]:
    """The owner's own turns a model may read, oldest first.

    Sensitive turns never; for the cloud, only turns classified public or
    personal (unclassified is treated as sensitive, as everywhere else).
    """
    allowed = [s.value for s in Sensitivity if s.cloud_ok] if cloud else [
        s.value for s in Sensitivity if s is not Sensitivity.SENSITIVE
    ]
    marks = ", ".join("?" * len(allowed))
    stats.sensitive_skipped += int(conn.execute(
        f"""SELECT count(*) FROM entries WHERE source = 'ai_conversations'
            AND authored_by_user = 1 AND sensitivity NOT IN ({marks})""",  # noqa: S608 - placeholders only
        allowed,
    ).fetchone()[0])
    rows = conn.execute(
        f"""SELECT id, body, content_hash, thread_id, occurred_start,
                   conversation_metadata_json FROM entries
            WHERE source = 'ai_conversations' AND authored_by_user = 1
              AND sensitivity IN ({marks})
            ORDER BY occurred_start""",  # noqa: S608 - placeholders only
        allowed,
    ).fetchall()
    turns: list[Turn] = []
    sessions: dict[str, set[str | None]] = defaultdict(set)
    for row in rows:
        stats.turns_read += 1
        scope, text = _turn_scope(str(row["body"]))
        if len(text.split()) < MIN_WORDS:
            continue
        turn = Turn(str(row["id"]), str(row["content_hash"]), row["thread_id"],
                    row["occurred_start"], scope, text, _context_before(row))
        turns.append(turn)
        sessions[_opening(text)].add(turn.thread_id)
    kept = []
    for turn in turns:
        if len(turn.text.split()) >= PASTED_MIN_WORDS and len(sessions[_opening(turn.text)]) > 1:
            stats.pasted += 1
        else:
            kept.append(turn)
    return kept


def _context_before(row: sqlite3.Row) -> str:
    try:
        conversation = json.loads(row["conversation_metadata_json"] or "{}")
    except ValueError:
        return ""
    said = conversation.get("context_before") if isinstance(conversation, dict) else None
    return said if isinstance(said, str) else ""


def _opening(text: str) -> str:
    return _fold(" ".join(text.split()[:PASTED_OPENING_WORDS]))


def _unavailable(exc: Exception) -> bool:
    """A failure the next reader should take over: the provider is down, busy or signed out.

    An invalid request is not one: it would fail the same way again.
    """
    from varyz.llm.oauth import OAuthError
    from varyz.llm.providers import NoCredentialsError

    if isinstance(exc, (httpx.TransportError, OSError, OAuthError, NoCredentialsError)):
        return True
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status in {401, 403, 408, 409, 429} or status >= 500)


def reader_from_config(cfg: Any, *, schema: dict[str, Any], max_tokens: int) -> Completer:
    """The `[workflows]` local model, ready to read; ``ReaderUnavailable`` when it is not."""
    from varyz.llm.ollama_local import OllamaProvider
    from varyz.resolution.local_classifier import (
        CONTEXT_WINDOW,
        ClassifierUnavailable,
        local_model_from_config,
    )

    try:
        # Starts the Ollama setup installed, and checks the model is pulled
        # and that loading it leaves the GPU its headroom.
        local_model_from_config(cfg).ready()
    except ClassifierUnavailable as exc:
        raise ReaderUnavailable(str(exc)) from exc
    settings = cfg.workflows
    return OllamaProvider(
        settings.classifier_model,
        url=settings.classifier_url,
        max_tokens=max_tokens,
        # The classifiers' window: a different one would make Ollama load the
        # model a second time.
        context_window=CONTEXT_WINDOW,
        think=False,
        format_schema=schema,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        keep_alive=settings.classifier_keep_alive,
    )


def cloud_reader_from_config(cfg: Any, *, max_tokens: int) -> tuple[Completer, str]:
    """The `[llm]` provider through the owner's sign-in, and the name its reads are kept under."""
    from varyz.llm.providers import NoCredentialsError, build_provider

    try:
        model = build_provider(cfg, role="enrich", max_tokens=max_tokens)
    except NoCredentialsError as exc:
        raise ReaderUnavailable(str(exc)) from exc
    name = cfg.llm.provider
    return guarded(cfg, model), f"{name}:{cfg.llm.models_for(name).enrich_model}"


class UsageGuard:
    """A cloud reader on the owner's Claude or ChatGPT plan that stops before its limits run low.

    `varyz rules mine` is a large reading: before a call, at most every
    ``every`` seconds, it reads how much of the plan is left and stops, keeping
    what it read, at `large_reading_min_left_5h` / `_weekly` percent or less,
    or when that cannot be read.
    """

    def __init__(self, model: Completer, provider: str, gate: Any,
                 read: Callable[[], Any], *, every: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model
        self.provider = provider
        self.gate = gate
        self.read = read
        self.every = every
        self.clock = clock
        self._checked_at: float | None = None

    def complete(self, system: str, user: str) -> str:
        from varyz.resolution.usage import describe

        now = self.clock()
        if self._checked_at is None or now - self._checked_at >= self.every:
            found = self.read()
            if found is None or not found.known:
                raise ReaderUnavailable(
                    f"how much of your {self.provider} plan is left cannot be read")
            if self.gate.blocks(found):
                raise ReaderUnavailable(
                    f"your {self.provider} plan is low ({describe(found)}; large readings stop "
                    f"at {self.gate.five_hour:g}% / {self.gate.weekly:g}%)")
            self._checked_at = now
        return self.model.complete(system, user)


def _signed_in(store: Any) -> Any:
    """The sign-in varyz keeps for a provider while it is valid; None otherwise."""
    with contextlib.suppress(Exception):
        stored = store().load()
        if stored is not None and not stored.expired():
            return stored
    return None


def guarded(cfg: Any, model: Any) -> Any:
    """``model`` behind a `UsageGuard` when it spends a plan's sign-in; an API key has no plan."""
    from varyz.llm import claude, openai_oauth
    from varyz.resolution import usage

    gate = usage.gates(cfg)["full"]
    if isinstance(model, claude.ClaudeProvider) and model.oauth:
        def read_claude() -> Any:
            stored = _signed_in(claude.token_store)
            return usage.read_claude(token=stored.access_token if stored else None)

        return UsageGuard(model, "Claude", gate, read_claude)
    if isinstance(model, openai_oauth.OpenAIOAuthProvider):
        def read_codex() -> Any:
            stored = _signed_in(openai_oauth.token_store)
            account = (stored.extra or {}).get("account_id") if stored else None
            return usage.read_codex(
                credentials=(stored.access_token, str(account)) if account else None)

        return UsageGuard(model, "ChatGPT", gate, read_codex)
    return model


def local_fit(cfg: Any) -> tuple[str, str]:
    """How this machine runs the local model: 'fits', 'tight' or 'no', and why."""
    from varyz.resolution.local_classifier import (
        ClassifierUnavailable,
        free_disk_mib,
        local_model_from_config,
        machine_fit,
    )

    try:
        size = local_model_from_config(cfg).size_bytes()
    except ClassifierUnavailable:
        # Not known until Ollama answers; loading it checks again.
        return "fits", ""
    data_dir = Path(cfg.data_dir)
    return machine_fit(size, free_disk=lambda: free_disk_mib(
        data_dir if data_dir.exists() else Path.home()
    ))


def reading_order(cfg: Any, fit: tuple[str, str]) -> list[str]:
    """Which readers to try, in order: 'cloud', 'local'. Word matching comes after them.

    The cloud reads first unless the owner keeps their turns on this machine;
    the local model takes over where the machine can run it.
    """
    local = [] if fit[0] == "no" else ["local"]
    return ["cloud", *local] if cfg.workflows.rule_reader_cloud else local


__all__ = [
    "Completer",
    "ReaderUnavailable",
    "Turn",
    "UsageGuard",
    "cloud_reader_from_config",
    "guarded",
    "local_fit",
    "owner_turns",
    "reader_from_config",
    "reading_order",
]
