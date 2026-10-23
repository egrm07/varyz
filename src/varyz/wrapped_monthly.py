"""Monthly Varyz Wrapped: the card in the chat and one line under it, once a month.

It travels with Varyz news (``varyz.news``): the ``user-prompt`` hook hands it to
the agent, which shows the owner's card as an image in the chat and ends its
reply with one line, set apart by a blank line: "Btw, Varyz prepared your
monthly Varyz Wrapped: you are The Editor, 1% of people." Nothing to run and
nothing to answer; when the owner lets it pass, the agent never brings it up
again. It comes at most once in 30 days, counted from the last time the owner
saw their card (the review page or ``varyz wrapped``) or was shown it here. The
first check only notes the day, so a new install waits a month. It follows news:
``varyz news off`` turns it off, and it keeps to the same quiet start after
install. The type is read from the owner's own rules, read-only; nothing is
written but the card state and the card's image, and nothing is sent anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from varyz.card import work_with as card
from varyz.card.archetype import Archetype
from varyz.card.rarity import share_text
from varyz.card.source import load_owner_rules

TITLE = "Monthly Varyz Wrapped"
MONTH = timedelta(days=30)
# The yellow card, the one the review page shows first.
STYLE: card.Style = "yellow"


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def line(kind: Archetype) -> str:
    """The one line the owner reads at the end of the reply."""
    return (f"Btw, Varyz prepared your monthly Varyz Wrapped: you are {kind.title}, "
            f"{share_text(kind.key)} of people.")


def due(cfg: Any, *, now: datetime | None = None) -> tuple[Archetype, Path] | None:
    """The owner's type and their card's image when a Wrapped is due, marked as
    shown; None otherwise."""
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
    statements = card.own_statements(owner)
    kind = card.card_type(rules)
    # The card the review page would show: the name and lines the owner picked last.
    who = str(state.get("name") or "") or card.git_first_name()
    picked = card.valid_pick(state.get("lines")) or card.default_pick(card.owned(statements))
    folder = cfg.data_dir / "wrapped"
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / f"{card.file_stem(who)}-{STYLE}.png"
    # Drawn before the state is saved: a card that cannot be drawn is not marked shown.
    image.write_bytes(card.render_png(who, kind, picked, STYLE))
    state.update(top=list(card.own_top(statements)), notified_at=stamp)
    card.save_state(cfg.data_dir, state)
    return kind, image


def agent_notice(cfg: Any, *, now: datetime | None = None,
                 conversation_id: str | None = None) -> str | None:
    """What to hand the agent when a Wrapped is due; None when it is not."""
    from varyz import news
    from varyz.onboarding import news_allowed

    if not news.is_on(cfg) or not news_allowed(
            cfg, conversation_id=conversation_id,
            now=now.timestamp() if now is not None else None):
        return None
    found = due(cfg, now=now)
    if found is None:
        return None
    kind, image = found
    return (
        "Monthly Varyz Wrapped for the user. Carry on with their request first. Then show "
        f"them their card, the image at {image}, in the chat: with a tool that shows the "
        "user an image or file if you have one, otherwise as a markdown image "
        f"({image.as_uri()}). Do not ask whether they want to see it. End your reply "
        "with the line below, once, as the very last line, after a blank line, and add "
        "nothing to it or after it. It is quoted text, not an instruction to you. If they "
        "do not respond to it, never bring it up again and treat the card as not part of "
        "this conversation.\n"
        + line(kind)
    )


__all__ = ["MONTH", "STYLE", "TITLE", "agent_notice", "due", "line"]
