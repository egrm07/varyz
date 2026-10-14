"""Where everything sits on the card, computed once and drawn twice (SVG and PNG).

The layout is plain data: text runs at baseline positions, rectangles and the
Varyz mark. Both renderers draw the same list, so the SVG and the PNG carry the
same line breaks and the same truncations. A rule too long for its slot keeps
its own words up to the point it stops fitting and ends in a visible ellipsis;
nothing is reworded.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from varyz.card.compose import CardContent, card_text
from varyz.card.fonts import ROLES, Role, raster_font

ELLIPSIS = "…"
OPEN_QUOTE = "“"
CLOSE_QUOTE = "”"

NIGHT = "#11120f"
INK = "#f4f1e8"
MUTED = "#aeada2"
DIM = "#8b8c82"
SIGNAL = "#ffc23d"  # marigold


def _blend(fg: str, bg: str, alpha: float) -> str:
    def rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    mixed = [
        round(a * alpha + b * (1 - alpha)) for a, b in zip(rgb(fg), rgb(bg), strict=True)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


LINE = _blend(INK, NIGHT, 0.16)

Size = Literal["square", "story"]
SIZES: dict[Size, tuple[int, int]] = {"square": (1080, 1080), "story": (1080, 1920)}


class Measurer(Protocol):
    def __call__(self, text: str, role: Role, size: int) -> float: ...


def heuristic_measure(text: str, role: Role, size: int) -> float:
    """Average-character width: used only when Pillow cannot measure."""
    return len(text) * ROLES[role].em_ratio * size


def pillow_measure(text: str, role: Role, size: int) -> float:
    return float(raster_font(role, size).getlength(text))


def default_measurer() -> Measurer:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return heuristic_measure
    return pillow_measure


# -- text fitting ---------------------------------------------------------------


def fit_lines(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    max_lines: int,
    *,
    closing: str = "",
) -> tuple[list[str], bool]:
    """Wrap ``text`` into at most ``max_lines`` lines; (lines, truncated).

    ``closing`` (a closing quote) is kept on the last line. When the text does not
    fit, the last line ends at a word boundary followed by an ellipsis.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    index = 0
    while index < len(words):
        word = words[index]
        candidate = f"{current} {word}" if current else word
        if measure(candidate) <= max_width:
            current = candidate
            index += 1
            continue
        if not current:
            # One word wider than the line: break it by characters.
            head = ""
            for char in word:
                if measure(head + char) > max_width:
                    break
                head += char
            head = head or word[:1]
            lines.append(head)
            words[index] = word[len(head):]
            if len(lines) == max_lines:
                break
            continue
        lines.append(current)
        current = ""
        if len(lines) == max_lines:
            break
    truncated = index < len(words)
    if not truncated and current:
        lines.append(current)
    if not lines:
        return ([closing] if closing else []), False
    if len(lines) > max_lines:
        truncated = True
        lines = lines[:max_lines]
    if truncated:
        last = lines[-1]
        tail = ELLIPSIS + closing
        while last and measure(last + tail) > max_width:
            cut = last.rsplit(" ", 1)
            last = cut[0] if len(cut) == 2 else last[:-1]
        lines[-1] = last.rstrip(" ,;:.-—") + tail
    elif closing:
        if measure(lines[-1] + closing) <= max_width:
            lines[-1] += closing
        elif len(lines) < max_lines:
            # Never a closing quote alone on a line: the last word goes down with it.
            head, _, last_word = lines[-1].rpartition(" ")
            if head:
                lines[-1] = head
                lines.append(last_word + closing)
            else:
                lines.append(closing)
        else:
            last = lines[-1]
            while last and measure(last + ELLIPSIS + closing) > max_width:
                cut = last.rsplit(" ", 1)
                last = cut[0] if len(cut) == 2 else last[:-1]
            lines[-1] = last.rstrip(" ,;:.-—") + ELLIPSIS + closing
            truncated = True
    return lines, truncated


def balanced_lines(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    max_lines: int,
    *,
    closing: str = "",
) -> tuple[list[str], bool]:
    """``fit_lines``, with the lines of a wrapped text made about equally long.

    The narrowest width that still gives the same number of lines, unshortened,
    is used, so a rule never ends on one stranded word. Text that is shortened
    anyway, or fits on one line, is left as ``fit_lines`` has it.
    """
    lines, truncated = fit_lines(text, measure, max_width, max_lines, closing=closing)
    if truncated or len(lines) < 2:
        return lines, truncated
    low, high = max_width * 0.5, max_width
    for _ in range(_BALANCE_STEPS):
        middle = (low + high) / 2
        trial, cut = fit_lines(text, measure, middle, max_lines, closing=closing)
        if cut or len(trial) != len(lines):
            low = middle
        else:
            high = middle
    return fit_lines(text, measure, high, max_lines, closing=closing)


_BALANCE_STEPS = 14


# -- the layout model -------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    x: float
    y: float  # baseline
    text: str
    role: Role
    size: int
    fill: str
    anchor: Literal["start", "end"] = "start"


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float
    fill: str


@dataclass(frozen=True)
class Mark:
    """The Varyz bird (62 x 34 units) at ``x, y`` scaled by ``scale``."""

    x: float
    y: float
    scale: float
    fill: str


Element = Text | Box | Mark


@dataclass(frozen=True)
class Placed:
    """What one slot shows, exactly as drawn."""

    slot: str
    rule_id: str
    lines: tuple[str, ...]
    truncated: bool


@dataclass
class Layout:
    size: Size
    width: int
    height: int
    background: str
    elements: list[Element] = field(default_factory=list)
    placed: list[Placed] = field(default_factory=list)

    def texts(self) -> list[str]:
        return [element.text for element in self.elements if isinstance(element, Text)]


@dataclass(frozen=True)
class Spec:
    margin: int
    brand_size: int
    handle_size: int
    title_size: int
    title_min: int
    archetype_size: int
    line_size: int
    label_size: int
    quote_size: int
    quote_lines: int
    item_size: int
    item_lines: int
    items: int
    footer_size: int
    gap: int


SPECS: dict[Size, Spec] = {
    "square": Spec(
        margin=80, brand_size=38, handle_size=25, title_size=84, title_min=64,
        archetype_size=44, line_size=28, label_size=19, quote_size=38, quote_lines=2,
        item_size=27, item_lines=2, items=3, footer_size=22, gap=40,
    ),
    "story": Spec(
        margin=96, brand_size=48, handle_size=32, title_size=132, title_min=90,
        archetype_size=66, line_size=40, label_size=25, quote_size=54, quote_lines=4,
        item_size=38, item_lines=4, items=5, footer_size=29, gap=64,
    ),
}
# The one small label left on the card: above the list, so the numbers read as
# the owner's rules. The quote needs none; its quotation marks say what it is.
LIST_LABEL = "House rules"

# Tried in order until everything fits: first as designed, then with less room
# for each slot, then with fewer list items. The first variant that fits wins.
_SQUEEZE: tuple[dict[str, int], ...] = (
    {},
    {"item_lines": 1},
    {"quote_lines": 2, "item_lines": 1},
    {"quote_lines": 2, "item_lines": 1, "items": 3},
    {"quote_lines": 1, "item_lines": 1, "items": 2},
    {"quote_lines": 1, "item_lines": 1, "items": 1},
)
# Leftover room widens the gaps between sections, never the type, and only up to
# this factor; what is still left is split above and below, a little more below
# (the optical centre sits above the geometric one). A card with one or two
# rules stays a compact block instead of spreading into empty bands.
_MAX_GAP_GROWTH = 1.4
_GROWTH_STEPS = 12
_ABOVE = 0.44


def _with_gap(spec: Spec, factor: float) -> Spec:
    return replace(spec, gap=round(spec.gap * factor))


def _spread(
    content: CardContent, spec: Spec, width: int, measure: Measurer, room: float,
    body: _Builder,
) -> _Builder:
    """The widest gaps (up to _MAX_GAP_GROWTH) with which the body still fits in ``room``."""
    for step in range(1, _GROWTH_STEPS + 1):
        factor = 1 + (_MAX_GAP_GROWTH - 1) * step / _GROWTH_STEPS
        candidate = _body(content, _with_gap(spec, factor), width, measure, top=0)
        if candidate.y > room:
            break
        body = candidate
    return body


def _line_height(size: int, factor: float = 1.28) -> int:
    return round(size * factor)


class _Builder:
    def __init__(self, width: int, measure: Measurer) -> None:
        self.width = width
        self.measure = measure
        self.elements: list[Element] = []
        self.placed: list[Placed] = []
        self.y = 0.0

    def block(
        self,
        text: str,
        *,
        x: float,
        role: Role,
        size: int,
        fill: str,
        max_width: float,
        max_lines: int,
        leading: float = 1.28,
        closing: str = "",
        balance: bool = False,
    ) -> tuple[list[str], bool]:
        fit = balanced_lines if balance else fit_lines
        lines, truncated = fit(
            text, lambda value: self.measure(value, role, size), max_width, max_lines,
            closing=closing,
        )
        step = _line_height(size, leading)
        for number, line in enumerate(lines):
            baseline = self.y + size * 0.92 + number * step
            self.elements.append(Text(x, baseline, line, role, size, fill))
        self.y += size * 0.92 + max(0, len(lines) - 1) * step + size * 0.30
        return lines, truncated


def _title_size(content: CardContent, spec: Spec, width: float, measure: Measurer) -> int:
    size = spec.title_size
    while size > spec.title_min and measure(content.headline, "serif", size) > width:
        size -= 2
    return size


def quote_fits(text: str, measure: Measurer | None = None) -> bool:
    """Whether ``text`` fits the square card's quote in two lines, unshortened.

    The square is the tighter of the two sizes, so a quote that fits there fits
    the story too.
    """
    measure = measure or default_measurer()
    spec = SPECS["square"]
    size = spec.quote_size
    width, _ = SIZES["square"]
    _, truncated = fit_lines(
        OPEN_QUOTE + text, lambda value: measure(value, "serif_italic", size),
        width - 2 * spec.margin, 2, closing=CLOSE_QUOTE,
    )
    return not truncated


def _body(
    content: CardContent, spec: Spec, width: int, measure: Measurer, top: float
) -> _Builder:
    """Everything between the brand row and the footer, laid out from ``top``."""
    inner = width - 2 * spec.margin
    left = spec.margin
    b = _Builder(width, measure)
    b.y = top
    b.block(content.headline, x=left, role="serif", size=_title_size(content, spec, inner,
            measure), fill=INK, max_width=inner, max_lines=1, leading=1.0)
    b.y += spec.gap * 0.25
    b.block(content.verdict.archetype.title, x=left, role="serif", size=spec.archetype_size,
            fill=SIGNAL, max_width=inner, max_lines=2, leading=1.08)
    b.y += spec.gap * 0.02
    b.block(content.verdict.archetype.line, x=left, role="serif_italic", size=spec.line_size,
            fill=MUTED, max_width=inner, max_lines=2)

    if content.strongest is not None:
        b.y += spec.gap * 1.25
        lines, truncated = b.block(
            OPEN_QUOTE + (card_text(content.strongest) or ""), x=left, role="serif_italic",
            size=spec.quote_size, fill=INK, max_width=inner, max_lines=spec.quote_lines,
            leading=1.22, closing=CLOSE_QUOTE, balance=True,
        )
        b.placed.append(Placed("strongest", content.strongest.id, tuple(lines), truncated))

    items = content.house_rules[: spec.items]
    if items:
        b.y += spec.gap * 1.3
        b.block(LIST_LABEL.upper(), x=left, role="sans_bold", size=spec.label_size,
                fill=DIM, max_width=inner, max_lines=1)
        b.y += spec.label_size * 0.6
        indent = round(spec.item_size * 2.0)
        for number, rule in enumerate(items, 1):
            if number > 1:
                b.y += spec.item_size * 0.55
            start = b.y
            lines, truncated = b.block(
                card_text(rule) or "", x=left + indent, role="sans", size=spec.item_size,
                fill=INK, max_width=inner - indent, max_lines=spec.item_lines, leading=1.3,
                balance=True,
            )
            b.elements.append(Text(
                left, start + spec.item_size * 0.92, f"{number:02d}", "sans_bold",
                spec.item_size, SIGNAL,
            ))
            b.placed.append(Placed(f"rule {number}", rule.id, tuple(lines), truncated))
    return b


def build_layout(
    content: CardContent, size: Size, measure: Measurer | None = None
) -> Layout:
    """Lay the card out at ``size``, squeezing slots until everything fits."""
    measure = measure or default_measurer()
    width, height = SIZES[size]
    base = SPECS[size]
    brand_bottom = base.margin + base.brand_size * 1.15
    footer_top = height - base.margin - base.footer_size * 1.6
    room = footer_top - brand_bottom - base.gap * 1.6
    body = None
    spec = base
    for squeeze in _SQUEEZE:
        spec = replace(base, **squeeze)
        body = _body(content, spec, width, measure, top=0)
        if body.y <= room:
            break
    assert body is not None
    if body.y < room:
        body = _spread(content, spec, width, measure, room, body)
    offset = brand_bottom + base.gap * 0.8 + max(0.0, (room - body.y) * _ABOVE)
    layout = Layout(size, width, height, NIGHT)

    # Brand row: the bird mark and the wordmark; the bird's notch sits on the baseline.
    mark_scale = base.brand_size * 1.1 / 62
    layout.elements.append(Mark(
        base.margin, base.margin + base.brand_size * 0.78 - 30 * mark_scale, mark_scale, SIGNAL,
    ))
    layout.elements.append(Text(
        base.margin + 62 * mark_scale + base.brand_size * 0.3,
        base.margin + base.brand_size * 0.78, "Varyz", "serif", base.brand_size, INK,
    ))
    if content.handle:
        # The owner's @handle, only when given. It is validated to a short token,
        # so it always fits; the corner stays empty without one.
        layout.elements.append(Text(
            width - base.margin, base.margin + base.brand_size * 0.78, content.handle, "sans",
            base.handle_size, MUTED, anchor="end",
        ))
    for element in body.elements:
        layout.elements.append(_shift(element, offset))
    layout.placed.extend(body.placed)

    # Footer: at most one short stat, and the only link back.
    layout.elements.append(Box(base.margin, footer_top, width - 2 * base.margin, 2, LINE))
    baseline = height - base.margin
    stats = content.stats_line()
    if stats:
        layout.elements.append(Text(
            base.margin, baseline, stats, "sans", base.footer_size, DIM,
        ))
    layout.elements.append(Text(
        width - base.margin, baseline, "varyzai.com", "sans_bold", base.footer_size, SIGNAL,
        anchor="end",
    ))
    return layout


def _shift(element: Element, dy: float) -> Element:
    return replace(element, y=element.y + dy)


def layouts(
    content: CardContent, sizes: Sequence[Size], measure: Measurer | None = None
) -> list[Layout]:
    return [build_layout(content, size, measure) for size in sizes]


__all__ = [
    "CLOSE_QUOTE",
    "ELLIPSIS",
    "OPEN_QUOTE",
    "LIST_LABEL",
    "SIZES",
    "Box",
    "Layout",
    "Mark",
    "Measurer",
    "Placed",
    "Size",
    "Text",
    "build_layout",
    "default_measurer",
    "fit_lines",
    "heuristic_measure",
    "layouts",
    "quote_fits",
]
