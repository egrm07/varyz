"""Manual quick capture: `varyz add "..."`. A degenerate connector whose
items are pushed by the CLI rather than discovered by sync."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from varyz.connectors.base import register
from varyz.domain import (
    Cursor,
    Kind,
    NormalizedEntry,
    RawItem,
    TimePrecision,
    new_id,
    utc_now,
)


@register
class ManualConnector:
    connector_id: ClassVar[str] = "manual"

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        return iter(())  # nothing to pull; items arrive via make_item()

    @staticmethod
    def make_item(text: str, *, when: str | None = None, kind: str = "note") -> RawItem:
        payload = {
            "text": text,
            "when": when,
            "kind": kind,
            "captured_at": utc_now().isoformat(timespec="seconds"),
        }
        return RawItem(
            external_id=new_id(),
            payload=json.dumps(payload, ensure_ascii=False),
            payload_ext=".json",
            metadata=payload,
        )

    def normalize(self, item: RawItem) -> NormalizedEntry:
        meta = item.metadata or json.loads(item.payload_bytes().decode("utf-8"))
        captured = datetime.fromisoformat(meta["captured_at"])
        occurred, precision = parse_when(meta.get("when"), captured)
        return NormalizedEntry(
            kind=Kind(meta.get("kind", "note")),
            external_id=item.external_id,
            body=meta["text"],
            occurred_start=occurred,
            time_precision=precision,
            captured_at=captured,
            # A note reached the archive because the owner typed it into
            # `varyz add`. Leaving the flag at its default filed every one of
            # them as text with no known author, and the Mirror's own-words
            # queries -- goals, people, evidence -- select on exactly that
            # column, so a notebook-only archive was invisible to all three.
            authored_by_user=True,
        )


def parse_when(when: str | None, fallback: datetime) -> tuple[datetime, TimePrecision]:
    """'2019', '2019-07', '2019-07-14', or full ISO — else capture time.

    Raises ValueError on anything else, which is why the CLI calls it before
    it captures: reached here from normalize, the failure was a traceback
    printed at a person who had simply written the date another way.
    """
    if not when:
        return fallback, TimePrecision.EXACT
    for fmt, precision in (
        ("%Y-%m-%d", TimePrecision.DAY),
        ("%Y-%m", TimePrecision.MONTH),
        ("%Y", TimePrecision.YEAR),
    ):
        try:
            return datetime.strptime(when, fmt).replace(tzinfo=UTC), precision
        except ValueError:
            continue
    return datetime.fromisoformat(when), TimePrecision.EXACT
