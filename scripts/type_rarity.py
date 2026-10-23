"""How rare each AI type is, from public AI rule files on GitHub.

  python scripts/type_rarity.py fetch <folder>   # download the files (needs `gh` signed in)
  python scripts/type_rarity.py count <folder>   # classify them, rewrite card/rarity.py

`fetch` searches GitHub code for CLAUDE.md, AGENTS.md, .cursorrules,
copilot-instructions.md and GEMINI.md, keeps one file per repository (forks
skipped) and resumes from the folder's index.json. Only public files are read.

`count` treats each bullet line of 12 to 400 characters as a rule, skips files
with fewer than MIN_RULES, classifies the rest with ``varyz.card.archetype``
and writes the share of each type to ``src/varyz/card/rarity.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from varyz.card.archetype import ARCHETYPES, classify  # noqa: E402
from varyz.card.source import CardRule  # noqa: E402

NAMES = ("CLAUDE.md", "AGENTS.md", ".cursorrules", "copilot-instructions.md", "GEMINI.md")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)")
MIN_RULES = 5
TARGET = ROOT / "src" / "varyz" / "card" / "rarity.py"


def _search(name: str, page: int) -> list[dict]:
    command = ["gh", "api", "-X", "GET", "search/code", "-f", f"q=filename:{name}",
               "-f", "per_page=100", "-f", f"page={page}"]
    for _ in range(4):
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             check=False)
        if run.returncode == 0:
            return list(json.loads(run.stdout).get("items", []))
        print("search retry", name, page, run.stderr[:200], flush=True)
        time.sleep(65)
    return []


def fetch(folder: Path) -> None:
    files = folder / "files"
    files.mkdir(parents=True, exist_ok=True)
    index_path = folder / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    for name in NAMES:
        for page in range(1, 4):  # code search returns at most 300 results per query
            items = _search(name, page)
            print(name, page, len(items), flush=True)
            for item in items:
                repo = item["repository"]["full_name"]
                if repo in index or item["repository"].get("fork"):
                    continue
                url = (f"https://raw.githubusercontent.com/{repo}/HEAD/"
                       f"{urllib.parse.quote(item['path'])}")
                try:
                    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
                        text = response.read(200_000).decode("utf-8", "replace")
                except Exception as error:  # noqa: BLE001 - an unreadable file is skipped
                    print("skip", url, error, flush=True)
                    continue
                target = files / f"{len(index):04d}.txt"
                target.write_text(text, encoding="utf-8")
                index[repo] = {"path": item["path"], "name": name, "file": target.name}
            index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
            time.sleep(7)  # code search allows 10 requests a minute
    print("files", len(index))


def rules_of(text: str) -> list[CardRule]:
    found: list[CardRule] = []
    for line in text.splitlines():
        match = BULLET.match(line)
        if match and 12 <= len(body := match.group(1).strip()) <= 400:
            found.append(CardRule(f"r{len(found)}", body, "rule", "*", "", f"{len(found):05d}"))
    return found


def count(folder: Path) -> None:
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    types: Counter[str] = Counter()
    for entry in index.values():
        rules = rules_of((folder / "files" / entry["file"]).read_text(encoding="utf-8"))
        if len(rules) >= MIN_RULES:
            types[classify(rules).archetype.key] += 1
    total = sum(types.values())
    for key, found in types.most_common():
        print(f"  {found / total:6.1%}  {ARCHETYPES[key].title}")
    shares = "\n".join(f'    "{key}": {types.get(key, 0) / total:.4f},' for key in ARCHETYPES)
    month = datetime.now(UTC).strftime("%B %Y")
    TARGET.write_text(f'''"""How rare each AI type is: its share of public AI rule files on GitHub.

Written by ``scripts/type_rarity.py count``; do not edit by hand. {total} files
(CLAUDE.md, AGENTS.md, .cursorrules, copilot-instructions.md, GEMINI.md), one
per repository, each with at least {MIN_RULES} bullet rules, classified by
``varyz.card.archetype``. A type no file had shows as "<1%".
"""

from __future__ import annotations

FILES = {total}
BASIS = "Share of {total:,} public AI rule files on GitHub, {month}."
SHARE: dict[str, float] = {{
{shares}
}}


def share_text(key: str) -> str:
    """The share as the card prints it: a whole percent, "<1%" below one."""
    percent = SHARE.get(key, 0.0) * 100
    return "<1%" if percent < 1 else f"{{percent:.0f}}%"


__all__ = ["BASIS", "FILES", "SHARE", "share_text"]
''', encoding="utf-8", newline="\n")
    print("wrote", TARGET, total, "files")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"fetch", "count"}:
        sys.exit(__doc__)
    (fetch if sys.argv[1] == "fetch" else count)(Path(sys.argv[2]))
