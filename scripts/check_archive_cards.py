"""Render a bounded, read-only selection of real recorded owner rules locally."""
import json
import tomllib
from pathlib import Path

from PIL import Image

from varyz.card.fonts import raster_font
from varyz.card.layout import Text
from varyz.card.service import plan_card, write_card
from varyz.card.source import LedgerCounts, OwnerRules, load_owner_rules
from varyz.config import load_config

root = Path(__file__).resolve().parents[1]
folder = root / "data/promise-archive-eval/round11"
connection = tomllib.loads((Path.home() / ".codex/config.toml").read_text(
    encoding="utf-8"))["mcp_servers"]["varyz"]
args = connection.get("args", [])
path = Path(args[args.index("--config") + 1]) if "--config" in args else None
cfg = load_config(path)
# Three general rules already disclosed for the approved Varyz task, under the
# existing 4000-character owner-context limit. Never read other project rules.
approved = json.loads((folder / "general-rules.json").read_text(encoding="utf-8"))[3:]
ids = {row["id"] for row in approved}
owner = load_owner_rules(cfg.db_path)
rules = tuple(rule for rule in owner.rules if rule.id in ids and rule.scope == "*")
assert {rule.id for rule in rules} == ids
assert sum(len(rule.text) for rule in rules) < 4000
plan = plan_card(OwnerRules(rules, LedgerCounts(len(rules))), handle="egrm07", stats=False)
paths = write_card(plan, folder / "confirmed-cards")
checks = []
for layout in plan.layouts:
    bounds = []
    for item in layout.elements:
        if not isinstance(item, Text):
            continue
        font = raster_font(item.role, item.size)
        x = item.x - (font.getlength(item.text) if item.anchor == "end" else 0)
        left, top, right, bottom = font.getbbox(item.text, anchor="ls")
        bounds.append(x + left >= 0 and x + right <= layout.width
                      and item.y + top >= 0 and item.y + bottom <= layout.height)
    png = folder / "confirmed-cards" / f"varyz-my-way-{layout.size}.png"
    svg = png.with_suffix(".svg").read_text(encoding="utf-8")
    checks.append({"size": layout.size, "dimensions_correct":
                   Image.open(png).size == (layout.width, layout.height),
                   "all_glyphs_within_canvas": all(bounds),
                   "no_remote_font_hotlinks": "fonts.googleapis.com" not in svg,
                   "owner_handle_present": "@egrm07" in layout.texts(),
                   "visible_truncations": sum(p.truncated for p in layout.placed),
                   "shown_source_count": len({p.rule_id for p in layout.placed if p.rule_id})})
(folder / "card-checks.json").write_text(json.dumps({"checks": checks,
    "raw_paths": [str(p) for p in paths], "ledger_writes": False}, indent=2), encoding="utf-8")
print(json.dumps({"checks": checks, "ledger_writes": False}))
