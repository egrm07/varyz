"""LLM significance refinement: Claude reads whole chat-days and promotes the
messages that matter to the life story; everything else stays off the timeline.

Only cloud-eligible entries are ever sent (the sensitivity gate applies);
sensitive chats keep their local heuristic score forever. Search and ask are
unaffected — this only tunes the timeline's default view.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from varyz.domain import Sensitivity, new_id
from varyz.privacy.broker import EgressBroker
from varyz.privacy.models import ContextItem, ContextRequest, Interaction, LabelSource
from varyz.privacy.policy import PolicyEngine
from varyz.privacy.receipts import ReceiptStore

log = logging.getLogger(__name__)

SYSTEM = """You curate a personal life timeline from the user's own chat history.
Given one day of one conversation, identify ONLY messages that genuinely matter to
the user's life story: announcements, decisions, plans being made, milestones,
strong emotional moments, facts worth remembering about people. Everyday banter,
reactions, logistics and jokes must be omitted.

Reply with STRICT JSON, nothing else:
{"important": [{"i": <message number>, "score": <1-10>, "label": "<short reason>"}]}
Return {"important": []} if nothing qualifies. Scores: 10 = life-changing,
7 = clearly worth remembering, 4 = mildly notable."""

DEMOTED_SCORE = 0.10  # the model saw it and left it out
LIFE_EVENT_MIN = 7


@dataclass
class ChatDay:
    thread_id: str
    day: str
    entries: list[sqlite3.Row]

    def transcript(self) -> str:
        return "\n".join(self.lines())

    def lines(self) -> list[str]:
        lines = []
        for n, row in enumerate(self.entries, 1):
            body = row["body"]
            sender = "?"
            if body.startswith("From:"):
                head, _, body = body.partition("\n\n")
                sender = head.splitlines()[0].removeprefix("From:").strip()
            time = (row["occurred_start"] or "")[11:16]
            lines.append(f"{n}. [{time}] {sender}: {body.strip()[:500]}")
        return lines


def pending_chat_days(conn: sqlite3.Connection, limit: int | None = None) -> list[ChatDay]:
    """Chat-days containing at least one message not yet refined.
    Sensitive entries never leave the machine, so they are not even grouped."""
    rows = conn.execute(
        """SELECT e.* FROM entries e
           JOIN enrichment_jobs j ON j.entry_id = e.id
             AND j.task = 'significance' AND j.status = 'pending'
           WHERE e.kind = 'message' AND e.sensitivity IN ('public', 'personal')
           ORDER BY e.thread_id, e.occurred_start"""
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (row["thread_id"] or "", (row["occurred_start"] or "")[:10])
        groups.setdefault(key, []).append(row)
    days = [ChatDay(thread_id=k[0], day=k[1], entries=v) for k, v in groups.items()]
    return days[:limit] if limit else days


def estimate_tokens(days: list[ChatDay]) -> int:
    chars = sum(len(d.transcript()) for d in days) + len(days) * len(SYSTEM)
    return chars // 4


def refine_chat_day(
    conn: sqlite3.Connection,
    provider,
    day: ChatDay,
    *,
    destination: str = "cloud:fake",
    policy_version: str = "1",
    privacy_mode: str = "enforce",
) -> int:
    """Send one chat-day to the model, apply scores. Returns promoted count."""
    from varyz.destinations.llm import LLMDestinationAdapter

    request_id = new_id()
    intent = f"Curate timeline significance for {day.thread_id} on {day.day}"
    request = ContextRequest(
        request_id=request_id,
        actor="local_user",
        purpose="enrich_timeline",
        destination=destination,
        intent=intent,
        intent_hash=hashlib.sha256(intent.encode()).hexdigest(),
        requested_types=("evidence_excerpt",),
        requested_scope={"thread_id": day.thread_id, "day": day.day},
        max_items=len(day.entries),
        max_chars=sum(len(line) for line in day.lines()) + len(day.entries),
        interaction=Interaction.BACKGROUND,
        user_present=False,
        reversible=True,
        parameters={
            "payload_kind": "llm",
            "system_prompt": SYSTEM,
            "user_prefix": f"Conversation on {day.day}:\n\n",
            "user_suffix": "",
            "context_separator": "\n",
        },
    )
    candidates = tuple(
        ContextItem(
            item_id=f"entry:{row['id']}",
            resource_type="evidence_excerpt",
            resource_id=row["id"],
            content=line,
            sensitivity=Sensitivity(row["sensitivity"]),
            label_source=LabelSource.INHERITED,
            label_reasons=("entry_sensitivity",),
            source=row["source"],
            source_ids=(row["id"],),
        )
        for row, line in zip(day.entries, day.lines(), strict=True)
    )
    broker = EgressBroker(
        ReceiptStore(conn),
        policy=PolicyEngine(version=policy_version),
        adapters=(LLMDestinationAdapter(destination, provider, stream=False),),
        mode=privacy_mode,
    )
    prepared = broker.prepare(request, candidates)
    if prepared.blocked:
        raise PermissionError("timeline enrichment context was blocked by policy")
    reply = str(broker.execute(prepared).response)
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    parsed = json.loads(match.group(0)) if match else {"important": []}
    important = {
        int(item["i"]): item
        for item in parsed.get("important", [])
        if 1 <= int(item.get("i", 0)) <= len(day.entries)
    }

    with conn:
        for n, row in enumerate(day.entries, 1):
            if n in important:
                item = important[n]
                sig = round(max(0.0, min(int(item["score"]), 10)) / 10, 3)
                conn.execute("UPDATE entries SET significance=? WHERE id=?", (sig, row["id"]))
                if int(item["score"]) >= LIFE_EVENT_MIN and item.get("label"):
                    conn.execute(
                        """INSERT INTO life_events (id, entry_id, label, weight)
                           VALUES (?,?,?,?)""",
                        (new_id(), row["id"], str(item["label"])[:200], sig),
                    )
            else:
                conn.execute(
                    "UPDATE entries SET significance=? WHERE id=?",
                    (DEMOTED_SCORE, row["id"]),
                )
            conn.execute(
                """UPDATE enrichment_jobs SET status='done'
                   WHERE entry_id=? AND task='significance'""",
                (row["id"],),
            )
    return len(important)


def refine_significance(
    conn: sqlite3.Connection,
    provider,
    *,
    destination: str = "cloud:fake",
    policy_version: str = "1",
    privacy_mode: str = "enforce",
    limit: int | None = None,
    on_progress=None,
) -> tuple[int, int]:
    """Returns (chat_days_processed, messages_promoted)."""
    days = pending_chat_days(conn, limit)
    promoted = 0
    for i, day in enumerate(days, 1):
        try:
            promoted += refine_chat_day(
                conn,
                provider,
                day,
                destination=destination,
                policy_version=policy_version,
                privacy_mode=privacy_mode,
            )
        except Exception:
            log.exception("significance refinement failed for %s %s", day.thread_id, day.day)
        if on_progress:
            on_progress(i, len(days), promoted)
    return len(days), promoted
