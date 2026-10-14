"""Draw a card layout as SVG text or as a PNG file. Local files only.

The SVG is the source form: plain XML with the brand's font stacks, readable in
any browser. The PNG is painted from the same layout with Pillow, supersampled
twice for smooth edges, and carries no metadata. Pillow is imported only when a
PNG is asked for; it is already installed with Varyz (fastembed depends on it).
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from varyz.card.fonts import ROLES, raster_font
from varyz.card.layout import Box, Layout, Mark, Text

# The bird: a V whose arms are wings, four quadratic curves in a 62 x 34 box.
MARK_START = (0.0, 4.0)
MARK_CURVES = (
    ((14.0, -8.0), (31.0, 17.0)),
    ((48.0, -8.0), (62.0, 4.0)),
    ((46.0, 2.0), (31.0, 34.0)),
    ((16.0, 2.0), (0.0, 4.0)),
)
MARK_PATH = f"M{MARK_START[0]:g} {MARK_START[1]:g}" + "".join(
    f"Q{cx:g} {cy:g} {x:g} {y:g}" for (cx, cy), (x, y) in MARK_CURVES
) + "Z"


def _mark_points(steps: int = 24) -> tuple[tuple[float, float], ...]:
    """The bird's outline as a polygon, for Pillow, which cannot draw curves."""
    points = [MARK_START]
    x0, y0 = MARK_START
    for (cx, cy), (x1, y1) in MARK_CURVES:
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            points.append((u * u * x0 + 2 * u * t * cx + t * t * x1,
                           u * u * y0 + 2 * u * t * cy + t * t * y1))
        x0, y0 = x1, y1
    return tuple(points)


MARK_POINTS = _mark_points()
SUPERSAMPLE = 2


def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def to_svg(layout: Layout, *, title: str = "My Way, by Varyz") -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" '
        f'height="{layout.height}" viewBox="0 0 {layout.width} {layout.height}">',
        f"<title>{escape(title)}</title>",
        f'<rect width="{layout.width}" height="{layout.height}" fill="{layout.background}"/>',
    ]
    for element in layout.elements:
        if isinstance(element, Box):
            parts.append(
                f'<rect x="{_num(element.x)}" y="{_num(element.y)}" '
                f'width="{_num(element.width)}" height="{_num(element.height)}" '
                f'fill="{element.fill}"/>'
            )
        elif isinstance(element, Mark):
            parts.append(
                f'<path transform="translate({_num(element.x)} {_num(element.y)}) '
                f'scale({_num(element.scale)})" d="{MARK_PATH}" fill="{element.fill}"/>'
            )
        elif isinstance(element, Text):
            role = ROLES[element.role]
            style = ' font-style="italic"' if role.style == "italic" else ""
            anchor = ' text-anchor="end"' if element.anchor == "end" else ""
            parts.append(
                f'<text x="{_num(element.x)}" y="{_num(element.y)}" '
                f"font-family={quoteattr(role.family)} font-size=\"{element.size}\" "
                f'font-weight="{role.weight}"{style}{anchor} fill="{element.fill}" '
                f'xml:space="preserve">{escape(element.text)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def png_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def write_png(layout: Layout, path: Path) -> None:
    from PIL import Image, ImageDraw

    k = SUPERSAMPLE
    image = Image.new("RGB", (layout.width * k, layout.height * k), layout.background)
    draw = ImageDraw.Draw(image)
    for element in layout.elements:
        if isinstance(element, Box):
            box = (
                round(element.x * k),
                round(element.y * k),
                round((element.x + element.width) * k) - 1,
                round((element.y + element.height) * k) - 1,
            )
            draw.rectangle(box, fill=element.fill)
        elif isinstance(element, Mark):
            draw.polygon(
                [
                    ((element.x + px * element.scale) * k, (element.y + py * element.scale) * k)
                    for px, py in MARK_POINTS
                ],
                fill=element.fill,
            )
        elif isinstance(element, Text):
            draw.text(
                (element.x * k, element.y * k),
                element.text,
                font=raster_font(element.role, element.size * k),
                fill=element.fill,
                anchor="rs" if element.anchor == "end" else "ls",
            )
    final = image.resize((layout.width, layout.height), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    final.save(path, format="PNG", optimize=True)


__all__ = ["png_available", "to_svg", "write_png"]
