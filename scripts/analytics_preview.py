"""Render the real rules review page and its Varyz network popup, on a synthetic archive.

The page, the server and the offer rules are the real ones; only the rules are fixtures,
so saving a review never reaches the daemon. Each viewport gets a fresh archive whose
trial window is over, so the popup is due on its first saved review.
"""

import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from varyz import analytics, onboarding
from varyz.config import Config
from varyz.review_web import ReviewServer, ReviewSession

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "analytics-preview"
VIEWPORTS = (("desktop", 1440, 900), ("mobile", 390, 844))
RULES = [
    {"id": f"rule-{n}", "statement": statement, "status": "pending", "scope": "*",
     "applies_to": "all", "slot": "rule", "source": "model:profile", "created_at": "2026-10-16",
     "quotes": [], "corrections": 0, "messages": 2, "lifted_from": [], "trial": None}
    for n, statement in enumerate((
        "Run short targeted tests, never the full suite.",
        "Ask before anything outward-facing: publishing, sending, spending.",
    ))
]


class FixtureSession(ReviewSession):
    """The real session with fixture rules: offers and answers go through analytics."""

    def items(self) -> dict[str, Any]:
        return {"items": RULES, "counts": {"live": 0, "trial": 0, "pending": len(RULES),
                                           "withdrawn": 0}, "kept": 0, "dropped": 0}

    def decide(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        return {"done": [str(d.get("id")) for d in decisions], "errors": {}}


def audit(cfg: Config) -> list[str]:
    with sqlite3.connect(analytics.state_path(cfg)) as db:
        return [row[0] for row in db.execute("SELECT action FROM audit ORDER BY seq")]


def main() -> None:
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    for name, width, height in VIEWPORTS:
        base = OUT / name
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        cfg = Config(base_dir=base)
        for n in range(onboarding.QUIET_CONVERSATIONS + 1):
            onboarding.note_conversation(cfg, f"preview-{n}", now=time.time() - 2 * 86400)
        server = ReviewServer(FixtureSession(None, None, "preview-device", cfg))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [str(runtime / "bin/node.exe"), str(ROOT / "scripts/analytics_preview.cjs"),
                 server.url, str(OUT), str(runtime / "node_modules/playwright"),
                 name, str(width), str(height)],
                capture_output=True, timeout=120,
            )
        finally:
            server.shutdown()
            server.server_close()
        if result.returncode:
            sys.exit(result.stderr.decode("utf-8", errors="replace"))
        print(result.stdout.decode("utf-8").strip())
        # Escape, then the x: two noes; then a yes. The popup was shown once.
        actions = audit(cfg)
        assert actions == ["offer_shown", "consent_off", "consent_off", "consent_on"], actions
        assert analytics.status(cfg)["consent"] is True
        print(name, "audit", actions)


if __name__ == "__main__":
    main()
