"""Local distillation: a local LLM reads candidate memories and returns only
the minimal excerpts needed to answer, so the cloud model receives a compact,
relevance-filtered context instead of full entries.

Candidates are already sensitivity-filtered (cloud-eligible) before they reach
here, so a distilled excerpt can never carry sensitive content to the cloud.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from varyz.domain import SearchHit

DISTILL_SYSTEM = """You are a local privacy filter running on the user's own machine.
You are given a QUESTION and numbered CANDIDATE excerpts from the user's personal
archive. Select ONLY the excerpts that are actually needed to answer the question,
and trim each to the minimal relevant sentence(s). Drop everything irrelevant.
Never invent or add information.

Reply with STRICT JSON and nothing else:
{"keep": [{"i": <candidate number>, "excerpt": "<minimal relevant text>"}]}
Return {"keep": []} if none are relevant."""

CAND_CHARS = 600  # how much of each candidate the local model sees


@dataclass
class Excerpt:
    hit: SearchHit
    text: str


def _candidate_block(hits: list[SearchHit]) -> str:
    lines = []
    for n, h in enumerate(hits, 1):
        date = (h.entry.occurred_start or "undated")[:10]
        body = (h.chunk or h.entry.body)[:CAND_CHARS]
        lines.append(f"[{n}] {date} · {h.entry.kind}: {body}")
    return "\n".join(lines)


def _parse_keep(reply: str) -> list[dict]:
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group(0)).get("keep", [])
    except json.JSONDecodeError:
        return []


def distill(question: str, hits: list[SearchHit], provider, *, max_keep: int) -> list[Excerpt]:
    """Run the local model; return the excerpts it chose (capped at max_keep).
    Raises on provider/transport errors — callers fall back to chunk context."""
    user = (
        f"QUESTION: {question}\n\nCANDIDATES:\n{_candidate_block(hits)}\n\n"
        f"Return at most {max_keep} kept excerpts as JSON."
    )
    keep = _parse_keep(provider.complete(DISTILL_SYSTEM, user))
    out: list[Excerpt] = []
    seen: set[int] = set()
    for item in keep:
        try:
            i = int(item.get("i", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= len(hits)) or i in seen:
            continue
        seen.add(i)
        excerpt = str(item.get("excerpt", "")).strip() or (hits[i - 1].chunk or "")
        out.append(Excerpt(hit=hits[i - 1], text=excerpt[:800]))
        if len(out) >= max_keep:
            break
    return out
