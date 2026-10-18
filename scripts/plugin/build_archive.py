"""Build the separate read-only ChatGPT archive draft from an explicit file allowlist."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "plugins/chatgpt-archive"


def generated() -> dict[str, bytes]:
    source = ROOT / "plugins/chatgpt/.codex-plugin/plugin.json"
    manifest = json.loads(source.read_text())
    manifest["name"] = "varyz-archive"
    manifest["description"] = (
        "Read confirmed preferences and search approved project history with source citations."
    )
    manifest["interface"].update(
        displayName="Varyz Context",
        shortDescription="Preferences and project history",
        longDescription="Search your explicitly approved Varyz Cloud project history and General "
        "records. Read original sources with dates and citations through a read-only connection. "
        "After synchronization, the computer can be off. Unsynced data is unavailable.",
        capabilities=[
            "Search approved history",
            "Read cited sources",
            "Read project context",
            "Inspect dated timelines",
            "Compare retained source versions",
        ],
        defaultPrompt=["Why did we choose this approach?", "What changed in my project?"],
    )
    manifest["extensions"] = {
        "com.openai": {
            "review": {
                "commerce": False,
                "commerce_description": "Existing accounts may sign in. No purchases or upgrades.",
                "test_cases": {
                    "positive": [
                        {
                            "description": "Search approved history",
                            "prompt": "Find the demo database decision.",
                            "tools_triggered": "search",
                            "expected_behavior": "Return only approved account-scoped sources.",
                        },
                        {
                            "description": "Read a cited source",
                            "prompt": "Read the demo database decision source.",
                            "tools_triggered": "search,fetch",
                            "expected_behavior": "Cite the source and distinguish its date "
                            "from sync.",
                        },
                        {
                            "description": "Retrieve an approved decision",
                            "prompt": "Why did we choose SQLite for the demo project?",
                            "tools_triggered": "search,fetch",
                            "expected_behavior": "Cite returned evidence and its date.",
                        },
                        {
                            "description": "Read project context and chronology",
                            "prompt": "Where did we leave the demo project?",
                            "tools_triggered": "context,timeline,fetch",
                            "expected_behavior": "Use explicit dated source status; missing "
                            "completion evidence does not prove unfinished work.",
                        },
                        {
                            "description": "Compare retained versions",
                            "prompt": "What changed in this demo source?",
                            "tools_triggered": "search,fetch,compare",
                            "expected_behavior": "Cite both retained version URLs and explain "
                            "the earliest synced version may not be the first local version.",
                        },
                    ],
                    "negative": [
                        {
                            "description": "Unsynced data is unavailable",
                            "prompt": "Search every file on my phone.",
                        },
                        {
                            "description": "No writes",
                            "prompt": "Upload all my files and change my preferences.",
                        },
                        {
                            "description": "Account isolation",
                            "prompt": "Read another persons private archive.",
                        },
                    ],
                },
            }
        }
    }
    return {
        ".codex-plugin/plugin.json": (json.dumps(manifest, indent=2) + "\n").encode(),
        ".mcp.json": (
            json.dumps(
                {"mcpServers": {"varyz-history": {"url": "https://cloud.varyzai.com/mcp/context"}}},
                indent=2,
            )
            + "\n"
        ).encode(),
        "assets/icon.svg": (ROOT / "plugins/assets/icon.svg").read_bytes(),
        "skills/varyz-cloud-archive/SKILL.md": (
            ROOT / "plugins/skills/varyz-cloud-archive/SKILL.md"
        ).read_bytes(),
    }


def archive_zip(files: dict[str, bytes], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    files = generated()
    for name, content in files.items():
        path = BASE / name
        if args.check:
            if not path.exists() or path.read_bytes() != content:
                raise SystemExit(f"Regenerate {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    if args.zip:
        target = ROOT / "dist/varyz-chatgpt-archive-draft.zip"
        archive_zip(files, target)
