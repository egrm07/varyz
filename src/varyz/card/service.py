"""The Taste Card from end to end: read, choose, lay out, preview, write.

``plan_card`` does everything except touch the disk, so the preview the owner
sees is computed from exactly the layouts that ``write_card`` would draw.
Nothing here opens a socket; the output is only files in a local folder.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from varyz.card.compose import CardContent, Selection, compose, normalize_handle, select
from varyz.card.layout import SIZES, Layout, Measurer, Size, build_layout, quote_fits
from varyz.card.render import png_available, to_svg, write_png
from varyz.card.source import CardRule, OwnerRules

FILE_STEM = "varyz-my-way"


class EmptyCardError(ValueError):
    """No rule is left for the card after the owner's choices."""


@dataclass(frozen=True)
class CardPlan:
    selection: Selection
    content: CardContent
    layouts: tuple[Layout, ...]

    def shown_ids(self) -> set[str]:
        return {
            placed.rule_id
            for layout in self.layouts
            for placed in layout.placed
            if placed.rule_id
        }


def plan_card(
    owner: OwnerRules,
    *,
    handle: str = "",
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    only: Iterable[str] = (),
    sizes: Sequence[Size] = ("square", "story"),
    stats: bool = True,
    measure: Measurer | None = None,
) -> CardPlan:
    handle = normalize_handle(handle)
    selection = select(owner.rules, include=include, exclude=exclude, only=only)
    content = compose(
        selection.chosen,
        handle=handle,
        counts=owner.counts if stats else None,
        quote_fits=lambda text: quote_fits(text, measure),
    )
    if content.strongest is None:
        # Nothing chosen, or only answers that would read as questions.
        raise EmptyCardError("no rule is left for the card")
    return CardPlan(
        selection=selection,
        content=content,
        layouts=tuple(build_layout(content, size, measure) for size in sizes),
    )


def output_paths(plan: CardPlan, out_dir: Path, *, png: bool = True) -> list[Path]:
    paths: list[Path] = []
    for layout in plan.layouts:
        paths.append(out_dir / f"{FILE_STEM}-{layout.size}.svg")
        if png:
            paths.append(out_dir / f"{FILE_STEM}-{layout.size}.png")
    return paths


def write_card(plan: CardPlan, out_dir: Path, *, png: bool = True) -> list[Path]:
    """Write each size as SVG (and PNG when Pillow is present); returns the paths."""
    png = png and png_available()
    out_dir.mkdir(parents=True, exist_ok=True)
    title = f"My Way, by Varyz: {plan.content.verdict.archetype.title}"
    written: list[Path] = []
    for layout in plan.layouts:
        svg_path = out_dir / f"{FILE_STEM}-{layout.size}.svg"
        svg_path.write_text(to_svg(layout, title=title), encoding="utf-8", newline="\n")
        written.append(svg_path)
        if png:
            png_path = out_dir / f"{FILE_STEM}-{layout.size}.png"
            write_png(layout, png_path)
            written.append(png_path)
    return written


def _scope(rule: CardRule) -> str:
    return "all projects" if rule.general else f"project {rule.scope}"


def preview_lines(plan: CardPlan) -> list[str]:
    """Exactly what goes on the card, slot by slot with rule ids, then what was left off."""
    content = plan.content
    lines: list[str] = []
    for layout in plan.layouts:
        width, height = SIZES[layout.size]
        lines.append(f"{layout.size} card ({width}x{height}):")
        lines.append(f"  Varyz{'   ' + content.handle if content.handle else ''}")
        lines.append(f"  {content.headline}")
        lines.append(
            f"  {content.verdict.archetype.title} - {content.verdict.archetype.line}"
        )
        for placed in layout.placed:
            shortened = "  (shortened)" if placed.truncated else ""
            label = f"{placed.slot}] {placed.rule_id}" if placed.rule_id else f"{placed.slot}]"
            lines.append(f"  [{label}{shortened}")
            lines.append(f"      {' '.join(placed.lines)}")
        footer = content.stats_line()
        lines.append(f"  footer: {footer + '   ' if footer else ''}varyzai.com")
        lines.append("")
    lines.append("Why this archetype:")
    if content.verdict.evidence:
        lines.append(f"  {content.verdict.evidence}")
    lines.extend(f"  {line}" for line in content.verdict.explain())
    lines.append("")
    shown = plan.shown_ids()
    questions = {rule.id for rule in content.unprintable}
    unseen = [
        rule for rule in plan.selection.chosen if rule.id not in shown and rule.id not in questions
    ]
    if unseen:
        lines.append(f"Counted for the archetype, no room on the card ({len(unseen)}):")
        lines.extend(f"  {rule.id}  {rule.text}" for rule in unseen)
        lines.append("")
    if content.unprintable:
        lines.append(
            f"Counted for the archetype, never printed: the answer needs its question "
            f"to make sense ({len(content.unprintable)}):"
        )
        lines.extend(f"  {rule.id}  {rule.text}" for rule in content.unprintable)
        lines.append("")
    if plan.selection.excluded:
        lines.append(f"Excluded by you ({len(plan.selection.excluded)}):")
        lines.extend(f"  {rule.id}  {rule.text}" for rule in plan.selection.excluded)
        lines.append("")
    if plan.selection.not_selected:
        lines.append(f"Not in --only ({len(plan.selection.not_selected)}):")
        lines.extend(f"  {rule.id}  {rule.text}" for rule in plan.selection.not_selected)
        lines.append("")
    if plan.selection.project_left_off:
        lines.append(
            f"Project rules, left off unless you name them with --include-project "
            f"({len(plan.selection.project_left_off)}):"
        )
        lines.extend(
            f"  {rule.id}  [{_scope(rule)}]  {rule.text}"
            for rule in plan.selection.project_left_off
        )
        lines.append("")
    return lines


__all__ = [
    "FILE_STEM",
    "CardPlan",
    "EmptyCardError",
    "output_paths",
    "plan_card",
    "preview_lines",
    "write_card",
]
