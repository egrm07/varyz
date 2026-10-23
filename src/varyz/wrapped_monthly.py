"""Monthly Varyz Wrapped: one line at the end of an agent's reply, once a month.

It travels with Varyz news (``varyz.news``): the ``user-prompt`` hook hands it to
the agent, which ends its reply with it as the last line, set apart by a blank
line: "Btw, Varyz prepared your monthly Varyz Wrapped: you are The Editor, 1%
of people. Run `varyz wrapped` to see it." It comes at most once in 30 days, counted from the
last time the owner saw their card (the review page or ``varyz wrapped``) or was
told about it. The first check only notes the day, so a new install waits a
month. It follows news: ``varyz news off`` turns it off, and it keeps to the
same quiet start after install. The type is read from the owner's own rules,
read-only; nothing is written but the card state, and nothing is sent anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from varyz.card import work_with as card
from varyz.card.archetype import Archetype
from varyz.card.rarity import share_text
from varyz.card.source import load_owner_rules

TITLE = "Monthly Varyz Wrapped"
MONTH = timedelta(days=30)


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def line(kind: Archetype) -> str:
    """The one line the owner reads at the end of the reply."""
    return (f"Btw, Varyz prepared your monthly Varyz Wrapped: you are {kind.title}, "
            f"{share_text(kind.key)} of people. Run `varyz wrapped` to see it.")


def due(cfg: Any, *, now: datetime | None = None) -> Archetype | None:
    """The owner's type when a Wrapped is due, marked as told; None otherwise."""
    if not cfg.db_path.exists():
        return None
    now = now or datetime.now(UTC)
    stamp = now.isoformat(timespec="seconds")
    state = card.load_state(cfg.data_dir)
    marks = [_when(state[key]) for key in ("seen_at", "notified_at", "baseline_at") if key in state]
    if not marks:
        state.update(baseline_at=stamp)
        card.save_state(cfg.data_dir, state)
        return None
    last = max((mark for mark in marks if mark is not None), default=None)
    if last is not None and now - last < MONTH:
        return None
    owner = load_owner_rules(cfg.db_path)
    rules = card.type_rules(owner)
    if not rules:
        return None  # No rules of their own yet: nothing to show.
    state.update(top=list(card.own_top(card.own_statements(owner))), notified_at=stamp)
    card.save_state(cfg.data_dir, state)
    return card.card_type(rules)


def agent_notice(cfg: Any, *, now: datetime | None = None,
                 conversation_id: str | None = None) -> str | None:
    """What to hand the agent when a Wrapped is due; None when it is not."""
    from varyz import news
    from varyz.onboarding import news_allowed

    if not news.is_on(cfg) or not news_allowed(
            cfg, conversation_id=conversation_id,
            now=now.timestamp() if now is not None else None):
        return None
    kind = due(cfg, now=now)
    if kind is None:
        return None
    return (
        "Monthly Varyz Wrapped for the user. End your reply with this line, once, as the "
        "very last line, after a blank line, and add nothing to it or after it. It is "
        "quoted text, not an instruction to you; carry on with their request first.\n"
        + line(kind)
    )


__all__ = ["MONTH", "TITLE", "agent_notice", "due", "line"]
