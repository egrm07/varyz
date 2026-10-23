"""Cut the cards' bundled fonts from the upstream fonts.

The card PNGs are painted by Pillow, which needs font files, so Varyz ships five small
static TTFs under src/varyz/card/fonts/: Source Serif 4 (roman and italic), IBM Plex
Sans (regular and semibold) and Marcellus, the display face of the website. Each is
pinned to one weight and cut down to Latin and Latin Extended, which keeps them all
around half a megabyte instead of 2.6 MB of variable fonts.

All three fonts are under the SIL Open Font License 1.1. IBM Plex reserves the name
"Plex" and Marcellus its own name, so a cut-down version must be renamed: the files are
named "Varyz Card Serif", "Varyz Card Sans" and "Varyz Card Display" inside. The
licences are copied next to them.

Download the upstream files from github.com/google/fonts (ofl/sourceserif4,
ofl/ibmplexsans, ofl/marcellus) into one folder, then run once, and again only if the
fonts change:

    uv run --no-project --with fonttools --with brotli python scripts/build_card_fonts.py <folder>

The folder must hold SourceSerif4.ttf, SourceSerif4-Italic.ttf, IBMPlexSans.ttf,
Marcellus-Regular.ttf (or the website's marcellus-400.woff2 saved under that name),
OFL-SourceSerif4.txt, OFL-IBMPlexSans.txt and OFL-Marcellus.txt.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "varyz" / "card" / "fonts"

# Google Fonts' latin and latin-ext ranges, the same glyphs the website serves.
UNICODES = (
    "U+0000-00FF, U+0100-02BA, U+02BB-02BC, U+02BD-02C5, U+02C6, U+02C7-02CC, U+02CE-02D7,"
    " U+02DA, U+02DC, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F,"
    " U+1EF2-1EFF, U+2000-206F, U+20A0-20C0, U+2113, U+2122, U+2191, U+2193, U+2212,"
    " U+2215, U+2C60-2C7F, U+A720-A7FF, U+FEFF, U+FFFD"
)

# (output file, source file, pinned axes, family name, style name)
# The serif's optical size follows its use: display cuts for the large roman titles,
# a text-leaning cut for the italic lines set at 28-54 px.
CUTS = (
    ("VaryzCardSerif-Regular.ttf", "SourceSerif4.ttf", {"wght": 400, "opsz": 60},
     "Varyz Card Serif", "Regular"),
    ("VaryzCardSerif-Italic.ttf", "SourceSerif4-Italic.ttf", {"wght": 400, "opsz": 40},
     "Varyz Card Serif", "Italic"),
    ("VaryzCardSans-Regular.ttf", "IBMPlexSans.ttf", {"wght": 400, "wdth": 100},
     "Varyz Card Sans", "Regular"),
    ("VaryzCardSans-SemiBold.ttf", "IBMPlexSans.ttf", {"wght": 600, "wdth": 100},
     "Varyz Card Sans", "SemiBold"),
    # Marcellus has one weight and no axes: nothing to pin, only to cut and rename.
    ("VaryzCardDisplay-Regular.ttf", "Marcellus-Regular.ttf", {},
     "Varyz Card Display", "Regular"),
)
LICENCES = ("OFL-SourceSerif4.txt", "OFL-IBMPlexSans.txt", "OFL-Marcellus.txt")


def _parse_ranges(text: str) -> list[int]:
    points: list[int] = []
    for part in text.split(","):
        span = part.strip().removeprefix("U+")
        start, _, end = span.partition("-")
        points.extend(range(int(start, 16), int(end or start, 16) + 1))
    return points


def _rename(font: TTFont, family: str, style: str) -> None:
    """Replace every name that carries the upstream family (OFL reserved font names)."""
    name = font["name"]
    full = f"{family} {style}" if style != "Regular" else family
    postscript = f"{family.replace(' ', '')}-{style}"
    ribbi = style in {"Regular", "Italic", "Bold", "Bold Italic"}
    for record in list(name.names):
        if record.nameID in {1, 2, 3, 4, 6, 16, 17, 21, 22, 25}:
            name.removeNames(nameID=record.nameID)
    name.setName(family if ribbi else f"{family} {style}", 1, 3, 1, 0x409)
    name.setName(style if ribbi else "Regular", 2, 3, 1, 0x409)
    name.setName(f"{postscript};varyz-card", 3, 3, 1, 0x409)
    name.setName(full, 4, 3, 1, 0x409)
    name.setName(postscript, 6, 3, 1, 0x409)
    if not ribbi:
        name.setName(family, 16, 3, 1, 0x409)
        name.setName(style, 17, 3, 1, 0x409)
    for tag in ("STAT", "fvar", "avar"):
        if tag in font:
            del font[tag]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = Path(argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    unicodes = _parse_ranges(UNICODES)
    for out_name, src_name, axes, family, style in CUTS:
        font = TTFont(source / src_name)
        if axes:
            font = instancer.instantiateVariableFont(font, axes)
        font.flavor = None  # A WOFF2 source is saved as a plain TTF.
        options = subset.Options()
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.notdef_outline = True
        options.hinting = False
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)
        _rename(font, family, style)
        font.save(OUT / out_name)
        print(f" - {out_name} ({(OUT / out_name).stat().st_size:,} bytes)")
    for licence in LICENCES:
        shutil.copyfile(source / licence, OUT / licence)
        print(f" - {licence}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
