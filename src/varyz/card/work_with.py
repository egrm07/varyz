"""The AI type card: the owner's AI type, how rare it is, and three lines.

The type is ``card.archetype``'s verdict over the owner's own rules (fixed word
lists, never a model), and its share comes from ``card.rarity``. Below it are
three short lines the owner's agents already follow. Each line stands for one
rule, and a line is the owner's own only when one of their confirmed rules says
it: a general rule (scope ``*``), stated by the owner, not a trial, not a seeded
starter. Someone with fewer than three such rules gets starter lines to fill
the card; they are marked as starters, and keeping the card keeps each one as
its ``rule``, so the card never says something the owner's agents were not told.

Two styles, both painted with the bundled fonts so every machine draws the same
card: yellow (marigold, all type black) and black (night, type in ink, the
share and payoff in marigold). The PNG carries no metadata and nothing leaves
the machine.
"""

from __future__ import annotations

import base64
import io
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from varyz.card.archetype import ARCHETYPES, Archetype, classify
from varyz.card.layout import INK, NIGHT, SIGNAL
from varyz.card.rarity import share_text
from varyz.card.source import (
    FOREIGN_SCOPE_PREFIXES,
    GENERAL_SCOPE,
    STARTER_SOURCE,
    CardRule,
    OwnerRules,
)
from varyz.scopes import stored_scope

Style = Literal["yellow", "black"]
STYLES: tuple[Style, ...] = ("yellow", "black")
SIZE = (1080, 1350)
PAYOFF = "My AI got the memo."
SITE = "varyzai.com"
CARD_SOURCE = "owner:card"
# A name longer than this would not fit the label at any size worth reading.
NAME_LIMIT = 20
LINES_ON_CARD = 3


@dataclass(frozen=True)
class CardLine:
    key: str
    # As printed; "|" marks where the line breaks, the way you would pause saying it.
    text: str
    # The rule agents are given when the owner keeps this line as a starter.
    rule: str
    # Which of the owner's rules say the same thing.
    pattern: re.Pattern[str]

    @property
    def plain(self) -> str:
        return " ".join(self.text.replace("|", " ").split())


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Strongest first: the default card takes the first three the owner has.
LINES: tuple[CardLine, ...] = (
    CardLine(
        "say-less", "Say less.",
        "Keep replies short: say only what is needed.",
        _words(r"\b(concise|be brief|keep (it|replies|answers|messages|responses) "
               r"(short|brief)|short (replies|answers|messages|responses)|no fluff|"
               r"say less|fewer words)\b"),
    ),
    CardLine(
        "talk-normal", "Talk normal.",
        "Use plain, everyday words. No jargon.",
        _words(r"\b(plain (language|english|words|wording)|everyday (words|language)|"
               r"no jargon|avoid jargon|without jargon|speak normally|talk normal(ly)?)\b"),
    ),
    CardLine(
        "undo", "If it can be undone, | don’t ask.",
        "Act without asking on anything that can be undone. Ask before anything that cannot.",
        _words(r"\b(reversible|can be undone|undoable|easy to undo)\b"),
    ),
    CardLine(
        "one-number", "One number. | Not a range.",
        "When asked for a figure, give one number with its unit, not a range.",
        _words(r"\b(one number|single number|not a range|no ranges?)\b"),
    ),
    CardLine(
        "prove-it", "Don’t say fixed. | Prove it.",
        "Verify a fix actually works before calling it done.",
        _words(r"\b(prove (it|that)|verify (that )?(the |a |each )?fix(es)?|"
               r"(before|until) (calling|saying|claiming) (it|something|anything) "
               r"(is )?(done|fixed)|(say|claim)(ing)? (it'?s |it is )?fixed)\b"),
    ),
    CardLine(
        "numbers-first", "Numbers first. | Failures included.",
        "Report numbers first, failures included.",
        _words(r"\b(numbers first|failures included|include (the )?failures|"
               r"report (the )?failures)\b"),
    ),
    CardLine(
        "no-em-dashes", "No em dashes.",
        "Do not use em dashes.",
        _words(r"\b(no|not|never|don'?t|avoid|without)\b[^.]*\bem[ -]?dash"),
    ),
)
BY_KEY: dict[str, CardLine] = {line.key: line for line in LINES}


# -- which lines are the owner's ------------------------------------------------------


def own_statements(owner: OwnerRules) -> list[str]:
    """The owner's general rules, as ``card.source`` reads them from the archive."""
    return [rule.text for rule in owner.rules if rule.kind == "rule" and rule.general]


def _own_rule_rows(items: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Confirmed rules the owner stated: no trials, no seeded starters."""
    found: list[Mapping[str, Any]] = []
    for item in items:
        provenance = item.get("provenance") or {}
        if (item.get("status") != "confirmed" or item.get("kind") != "rule"
                or str(item.get("source") or "") == STARTER_SOURCE
                or provenance.get("trial") is not None
                or (provenance.get("trial_ended") or {}).get("how") == "promoted"):
            continue
        found.append(item)
    return found


def own_statements_from_principles(items: Iterable[Mapping[str, Any]]) -> list[str]:
    """The same rules from the daemon's ``principles("confirmed")`` rows."""
    return [" ".join(str(item.get("statement") or "").split())
            for item in _own_rule_rows(items)
            if stored_scope(str(item.get("scope") or GENERAL_SCOPE)) == GENERAL_SCOPE]


# -- the type ----------------------------------------------------------------------------


def type_rules(owner: OwnerRules) -> list[CardRule]:
    """Everything the owner stated, every project included: the type reads it all."""
    return list(owner.rules)


def type_rules_from_principles(items: Iterable[Mapping[str, Any]]) -> list[CardRule]:
    """The owner's rules from the daemon's rows, when the archive cannot be read."""
    rules: list[CardRule] = []
    for number, item in enumerate(_own_rule_rows(items)):
        scope = stored_scope(str(item.get("scope") or GENERAL_SCOPE))
        if scope.startswith(FOREIGN_SCOPE_PREFIXES):
            continue
        rules.append(CardRule(
            id=str(item.get("id") or number),
            text=" ".join(str(item.get("statement") or "").split()),
            kind="rule", scope=scope, domain=str(item.get("domain") or ""),
            created_at=str(item.get("created_at") or ""),
        ))
    return rules


def card_type(rules: Sequence[CardRule]) -> Archetype:
    return classify(rules).archetype


def card_title(name: str, kind: Archetype) -> str:
    """The card in words: "Egor's AI type: The Editor"."""
    who = clean_name(name)
    return f"{who}'s AI type: {kind.title}" if who else f"My AI type: {kind.title}"


def owned(statements: Iterable[str]) -> tuple[str, ...]:
    """Keys of the lines the owner's rules say, strongest first."""
    texts = list(statements)
    return tuple(line.key for line in LINES
                 if any(line.pattern.search(text) for text in texts))


def own_top(statements: Iterable[str]) -> tuple[str, ...]:
    """The owner's own lines that make the default card (no starters)."""
    return owned(statements)[:LINES_ON_CARD]


def default_pick(own: Sequence[str]) -> list[str]:
    """The owner's strongest lines, starters after, printed strongest first."""
    chosen = [key for key in own if key in BY_KEY][:LINES_ON_CARD]
    chosen += [line.key for line in LINES if line.key not in chosen][
        : LINES_ON_CARD - len(chosen)]
    return [line.key for line in LINES if line.key in chosen]


def valid_pick(keys: object) -> list[str] | None:
    """Three different known lines, or None."""
    if not isinstance(keys, list) or len(keys) != LINES_ON_CARD:
        return None
    if not all(isinstance(key, str) and key in BY_KEY for key in keys):
        return None
    return list(keys) if len(set(keys)) == LINES_ON_CARD else None


# -- the name ----------------------------------------------------------------------------


def clean_name(text: object) -> str:
    """A first name fit for the title; empty when nothing usable was given."""
    if not isinstance(text, str):
        return ""
    words = "".join(
        ch for ch in unicodedata.normalize("NFC", text)
        if ch == " " or ch in "'-.’" or unicodedata.category(ch)[0] in {"L", "M"}
    ).split()
    return " ".join(words)[:NAME_LIMIT].strip()


def git_first_name() -> str:
    """The first word of git's user.name, the name most developers already gave."""
    import subprocess

    try:
        result = subprocess.run(["git", "config", "--global", "user.name"],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    words = clean_name(result.stdout).split()
    return words[0] if words else ""


def title_name(name: str) -> str:
    return clean_name(name) or "me"


def label(name: str) -> str:
    """The small line over the type: "EGOR'S AI TYPE", or "MY AI TYPE"."""
    who = clean_name(name)
    return f"{who.upper()}'S AI TYPE" if who else "MY AI TYPE"


def file_stem(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", clean_name(name))
                  .encode("ascii", "ignore").decode("ascii").casefold()).strip("-")
    return (slug or "my") + "-ai-type"


# -- painting ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    background: str
    ink: str  # label, type, lines and numbers
    soft: str  # the thin lines between rules
    accent: str  # the share and the payoff
    site: str


PALETTES: dict[Style, Palette] = {
    "yellow": Palette(background=SIGNAL, ink=NIGHT, soft="#d9a42f", accent=NIGHT, site=NIGHT),
    "black": Palette(background=NIGHT, ink=INK, soft="#3a3b35", accent=SIGNAL, site="#9a978c"),
}
# System fonts for a name the bundled Latin fonts cannot draw (Cyrillic, Greek, CJK...).
_NAME_FALLBACKS = ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf", "msyh.ttc",
                   "YuGothR.ttc", "malgun.ttf", "Arial Unicode.ttf", "Helvetica.ttc",
                   "DejaVuSans.ttf", "NotoSans-Regular.ttf", "NotoSansCJK-Regular.ttc")
WIDTH, HEIGHT = SIZE
MARGIN = 96
RULES_TOP = 768


def _latin(text: str) -> bool:
    return all(not ch.isalpha() or unicodedata.name(ch, "").startswith("LATIN") for ch in text)


def _font(role: str, size: int) -> Any:
    from varyz.card.fonts import raster_font

    return raster_font(role, size)  # type: ignore[arg-type]


def _text_font(text: str, role: str, size: int) -> Any:
    """The bundled font, or a system one when ``text`` is not Latin."""
    if _latin(text):
        return _font(role, size)
    from PIL import ImageFont

    from varyz.card.fonts import _font_dirs

    for file in _NAME_FALLBACKS:
        for directory in _font_dirs():
            if (directory / file).is_file():
                try:
                    return ImageFont.truetype(str(directory / file), size)
                except OSError:
                    continue
    return _font(role, size)


def _spaced(draw: Any, xy: tuple[float, float], text: str, font: Any, fill: str,
            spacing: int, right: bool = False) -> None:
    """Letter-spaced text on a baseline, from the left edge or to the right edge."""
    x, y = xy
    if right:
        x -= sum(draw.textlength(ch, font=font) + spacing for ch in text) - spacing
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += draw.textlength(ch, font=font) + spacing


def _wrap(draw: Any, text: str, font: Any, width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _hero(draw: Any, name: str, kind: Archetype, colors: Palette) -> None:
    # "EGOR'S AI TYPE" on the left, "ONE OF 46" on the right of the same line.
    text = label(name)
    _spaced(draw, (MARGIN, 132), text, _text_font(text, "sans_bold", 25), colors.ink, 4)
    _spaced(draw, (WIDTH - MARGIN, 132), f"ONE OF {len(ARCHETYPES)}", _font("sans", 25),
            colors.ink, 4, right=True)
    lead, _, rest = kind.title.partition(" ")
    if not rest:
        lead, rest = "", lead
    size = 250
    while (draw.textlength(rest, font=_font("display", size)) > WIDTH - 2 * MARGIN
           and size > 120):
        size -= 6
    top = 330 + round((250 - size) * 0.8 / 2)  # a shorter name sits in the middle
    name_base = top + 40 + round(size * 0.8)
    draw.text((MARGIN, top), lead, font=_font("display", 112), fill=colors.ink, anchor="ls")
    draw.text((MARGIN - 6, name_base), rest, font=_font("display", size), fill=colors.ink,
              anchor="ls")
    line_size = 58
    while (draw.textlength(kind.line, font=_font("display", line_size)) > WIDTH - 2 * MARGIN
           and line_size > 40):
        line_size -= 2
    draw.text((MARGIN, name_base + 104), kind.line, font=_font("display", line_size),
              fill=colors.ink, anchor="ls")
    # The share in large type, level with the short "The", so it never meets a long name.
    draw.text((WIDTH - MARGIN, top), share_text(kind.key), font=_font("display", 112),
              fill=colors.accent, anchor="rs")
    _spaced(draw, (WIDTH - MARGIN, top + 38), "OF PEOPLE", _font("sans_bold", 22),
            colors.ink, 4, right=True)


def _rules(draw: Any, keys: Sequence[str], colors: Palette, bottom: float) -> None:
    draw.line([(MARGIN, RULES_TOP), (WIDTH - MARGIN, RULES_TOP)], fill=colors.ink, width=3)
    column = MARGIN + 70
    room = WIDTH - MARGIN - column
    texts = [BY_KEY[key].plain for key in keys]
    # One size for all three: the largest, from 60 down to 46, that keeps each on
    # one line. A line still too long at 46 wraps.
    size = 60
    while size > 46 and any(draw.textlength(text, font=_font("display", size)) > room
                            for text in texts):
        size -= 2
    font = _font("display", size)
    leading = round(size * 1.15)
    step = (bottom - RULES_TOP) / max(len(texts), 1)
    for number, text in enumerate(texts):
        lines = _wrap(draw, text, font, room)
        base = RULES_TOP + step * number + step / 2 + 22 - (len(lines) - 1) * leading / 2
        draw.text((MARGIN, base - 30), f"0{number + 1}", font=_font("sans_bold", 26),
                  fill=colors.ink)
        for line in lines:
            draw.text((column, base), line, font=font, fill=colors.ink, anchor="ls")
            base += leading
        if number:
            y = RULES_TOP + step * number
            draw.line([(column, y), (WIDTH - MARGIN, y)], fill=colors.soft, width=2)


def render_png(name: str, kind: Archetype, keys: Sequence[str], style: Style) -> bytes:
    """The card as PNG bytes, 1080 x 1350."""
    from PIL import Image, ImageDraw

    colors = PALETTES[style]
    image = Image.new("RGB", SIZE, colors.background)
    draw = ImageDraw.Draw(image)
    _hero(draw, name, kind, colors)
    foot_y = HEIGHT - MARGIN - 46
    foot_line = foot_y - 44
    _rules(draw, keys, colors, foot_line)
    draw.line([(MARGIN, foot_line), (WIDTH - MARGIN, foot_line)], fill=colors.ink, width=3)
    draw.text((MARGIN, foot_y), PAYOFF, font=_font("display", 46), fill=colors.accent)
    draw.text((WIDTH - MARGIN, foot_y + 14), SITE, font=_font("sans", 28), fill=colors.site,
              anchor="ra")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def write_pngs(name: str, kind: Archetype, keys: Sequence[str], folder: Path) -> list[Path]:
    """Both styles into ``folder``; the paths, yellow first."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for style in STYLES:
        path = folder / f"{file_stem(name)}-{style}.png"
        path.write_bytes(render_png(name, kind, keys, style))
        paths.append(path)
    return paths


# -- what the owner last saw -------------------------------------------------------------


def state_path(data_dir: Path) -> Path:
    return data_dir / "wrapped" / "card.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(data_dir: Path, state: Mapping[str, Any]) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(state), indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def mark_seen(state: dict[str, Any], name: str, statements: Iterable[str],
              shown: Sequence[str]) -> None:
    """Note that the owner just saw their card, for Monthly Varyz Wrapped."""
    state.update(name=name, top=list(own_top(statements)), shown=list(shown),
                 seen_at=datetime.now(UTC).isoformat(timespec="seconds"))


def new_lines(state: Mapping[str, Any], statements: Iterable[str]) -> list[str]:
    """Own lines that would now make the card and the owner has not seen on it."""
    seen = {str(key) for key in [*(state.get("top") or []), *(state.get("shown") or [])]}
    return [key for key in own_top(statements) if key not in seen]


__all__ = [
    "BY_KEY",
    "CARD_SOURCE",
    "LINES",
    "LINES_ON_CARD",
    "PAYOFF",
    "SITE",
    "STYLES",
    "CardLine",
    "Style",
    "card_title",
    "card_type",
    "clean_name",
    "data_url",
    "default_pick",
    "file_stem",
    "git_first_name",
    "label",
    "load_state",
    "mark_seen",
    "new_lines",
    "own_statements",
    "own_statements_from_principles",
    "own_top",
    "owned",
    "render_png",
    "save_state",
    "state_path",
    "title_name",
    "type_rules",
    "type_rules_from_principles",
    "valid_pick",
    "write_pngs",
]
