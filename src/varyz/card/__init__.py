"""The "My Way" card (`varyz wrapped`): how the owner wants things done.

Built only from the owner's own explicit rules (``owner_principles`` and
``owner_knowledge``), general rules only unless the owner names a project rule,
with an archetype computed by fixed word lists rather than a model. Everything
runs offline and the only output is local SVG and PNG files.
"""

from __future__ import annotations

from varyz.card.archetype import ARCHETYPES, Archetype, Verdict, classify
from varyz.card.compose import (
    CardContent,
    HandleError,
    Selection,
    SelectionError,
    compose,
    normalize_handle,
    select,
)
from varyz.card.layout import SIZES, Layout, Size, build_layout, fit_lines
from varyz.card.render import png_available, to_svg, write_png
from varyz.card.service import (
    CardPlan,
    EmptyCardError,
    output_paths,
    plan_card,
    preview_lines,
    write_card,
)
from varyz.card.source import CardRule, LedgerCounts, OwnerRules, load_owner_rules

__all__ = [
    "ARCHETYPES",
    "SIZES",
    "Archetype",
    "CardContent",
    "CardPlan",
    "CardRule",
    "EmptyCardError",
    "HandleError",
    "Layout",
    "LedgerCounts",
    "OwnerRules",
    "Selection",
    "SelectionError",
    "Size",
    "Verdict",
    "build_layout",
    "classify",
    "compose",
    "fit_lines",
    "load_owner_rules",
    "normalize_handle",
    "output_paths",
    "plan_card",
    "png_available",
    "preview_lines",
    "select",
    "to_svg",
    "write_card",
    "write_png",
]
