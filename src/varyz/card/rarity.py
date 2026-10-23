"""How rare each AI type is: its share of public AI rule files on GitHub.

Written by ``scripts/type_rarity.py count``; do not edit by hand. 1109 files
(CLAUDE.md, AGENTS.md, .cursorrules, copilot-instructions.md, GEMINI.md), one
per repository, each with at least 5 bullet rules, classified by
``varyz.card.archetype``. A type no file had shows as "<1%".
"""

from __future__ import annotations

FILES = 1109
BASIS = "Share of 1,109 public AI rule files on GitHub, October 2026."
SHARE: dict[str, float] = {
    "guard": 0.0325,
    "proof": 0.2182,
    "pilot": 0.0577,
    "finish": 0.0352,
    "autopilot": 0.0135,
    "lean": 0.0361,
    "craft": 0.1470,
    "speed": 0.0018,
    "money": 0.0036,
    "auditor": 0.0225,
    "bouncer": 0.0045,
    "shipper": 0.0018,
    "nightguard": 0.0036,
    "hermit": 0.0009,
    "curator": 0.0072,
    "getaway": 0.0000,
    "banker": 0.0000,
    "scientist": 0.0388,
    "inspector": 0.0162,
    "madscientist": 0.0009,
    "engineer": 0.0198,
    "perfectionist": 0.0370,
    "speedrunner": 0.0063,
    "accountant": 0.0018,
    "captain": 0.0063,
    "director": 0.0009,
    "surgeon": 0.0045,
    "editor": 0.0126,
    "pitcrew": 0.0000,
    "cfo": 0.0000,
    "machine": 0.0018,
    "sniper": 0.0009,
    "artisan": 0.0045,
    "sprinter": 0.0000,
    "contractor": 0.0000,
    "clockmaker": 0.0036,
    "showrunner": 0.0009,
    "rocket": 0.0000,
    "thrifty": 0.0000,
    "purist": 0.0063,
    "hacker": 0.0000,
    "monk": 0.0000,
    "sketch": 0.0009,
    "thrift": 0.0000,
    "hustler": 0.0000,
    "pragmatist": 0.2498,
}


def share_text(key: str) -> str:
    """The share as the card prints it: a whole percent, "<1%" below one."""
    percent = SHARE.get(key, 0.0) * 100
    return "<1%" if percent < 1 else f"{percent:.0f}%"


__all__ = ["BASIS", "FILES", "SHARE", "share_text"]
