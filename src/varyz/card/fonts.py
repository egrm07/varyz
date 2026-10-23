"""The cards' typefaces, in one place.

Source Serif 4 for titles and IBM Plex Sans for the small print, the same two
families as the website (the owner's choice, 2026-09-25), and Marcellus, the
website's display face, for the AI type card. The PNG is painted
with the cut-down copies bundled in ``fonts/`` (built by
``scripts/build_card_fonts.py``; OFL 1.1, licences alongside), so every machine
draws the same card. The SVG carries a CSS stack instead, and the viewer's
machine picks the first font it has. If a bundled file cannot be opened, the PNG
falls back to the first installed file that matches the same stack, then to
Pillow's built-in scalable font. Swapping the card's type is an edit to
``ROLES`` and nothing else.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from PIL.ImageFont import FreeTypeFont, ImageFont

Role = Literal["serif", "serif_italic", "sans", "sans_bold", "display"]

# Each stack falls back to a generic family, so an SVG opened on a machine
# without any named font still renders as serif or sans, never blank.
SERIF_STACK = "'Source Serif 4', 'Palatino Linotype', Palatino, Georgia, 'DejaVu Serif', serif"
SANS_STACK = "'IBM Plex Sans', 'Segoe UI', 'Helvetica Neue', Arial, 'Liberation Sans', sans-serif"
DISPLAY_STACK = "Marcellus, 'Palatino Linotype', Palatino, Georgia, 'DejaVu Serif', serif"

# The bundled cuts, found before any installed font.
BUNDLED = Path(__file__).resolve().parent / "fonts"


@dataclass(frozen=True)
class FontRole:
    """One typeface role: the CSS stack for SVG and the files that match it for PNG."""

    family: str
    style: Literal["normal", "italic"]
    weight: int
    files: tuple[str, ...]
    # Width of an average character as a share of the font size, for measuring
    # when no font file can be opened (the SVG is then laid out conservatively).
    em_ratio: float


def _font_dirs() -> tuple[Path, ...]:
    dirs: list[Path] = [BUNDLED]
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        dirs.append(windir / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    else:
        dirs += [
            Path("/usr/share/fonts/opentype/urw-base35"),
            Path("/usr/share/fonts/type1/urw-base35"),
            Path("/usr/share/fonts/truetype/dejavu"),
            Path("/usr/share/fonts/truetype/liberation"),
            Path("/usr/share/fonts/TTF"),
            Path.home() / ".local" / "share" / "fonts",
        ]
    return tuple(dirs)


ROLES: dict[Role, FontRole] = {
    "serif": FontRole(
        family=SERIF_STACK,
        style="normal",
        weight=400,
        files=("VaryzCardSerif-Regular.ttf", "pala.ttf", "Palatino.ttc", "georgia.ttf",
               "DejaVuSerif.ttf"),
        em_ratio=0.50,
    ),
    "serif_italic": FontRole(
        family=SERIF_STACK,
        style="italic",
        weight=400,
        files=("VaryzCardSerif-Italic.ttf", "palai.ttf", "Palatino.ttc", "georgiai.ttf",
               "DejaVuSerif-Italic.ttf"),
        em_ratio=0.47,
    ),
    "sans": FontRole(
        family=SANS_STACK,
        style="normal",
        weight=400,
        files=("VaryzCardSans-Regular.ttf", "segoeui.ttf", "HelveticaNeue.ttc", "arial.ttf",
               "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
        em_ratio=0.52,
    ),
    "sans_bold": FontRole(
        family=SANS_STACK,
        style="normal",
        weight=600,
        files=("VaryzCardSans-SemiBold.ttf", "seguisb.ttf", "segoeuib.ttf", "arialbd.ttf",
               "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"),
        em_ratio=0.55,
    ),
    "display": FontRole(
        family=DISPLAY_STACK,
        style="normal",
        weight=400,
        files=("VaryzCardDisplay-Regular.ttf", "pala.ttf", "Palatino.ttc", "georgia.ttf",
               "DejaVuSerif.ttf"),
        em_ratio=0.52,
    ),
}


@cache
def font_file(role: Role) -> Path | None:
    """The first installed file for ``role``, or None when this machine has none."""
    for name in ROLES[role].files:
        for directory in _font_dirs():
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def raster_font(role: Role, size: int) -> FreeTypeFont | ImageFont:
    """A Pillow font for painting the PNG (Pillow is imported only here and in render)."""
    from PIL import ImageFont as PILImageFont

    path = font_file(role)
    if path is not None:
        try:
            return PILImageFont.truetype(str(path), size)
        except OSError:
            pass
    return PILImageFont.load_default(size)


__all__ = [
    "BUNDLED", "DISPLAY_STACK", "ROLES", "SANS_STACK", "SERIF_STACK", "FontRole", "Role",
    "font_file", "raster_font",
]
