"""Build local companion packages and the hosted ChatGPT plugin draft.

Run: python scripts/plugin/build.py [--check]
Generated bundles are checked in for repository marketplace installation.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def demo_recording_url() -> str | None:
    """Only an actual reviewed video URL belongs in the package, never credentials."""
    path = ROOT / "docs/submission/review-assets.json"
    if not path.exists():
        return None
    assets = json.loads(path.read_text(encoding="utf-8"))
    if set(assets) != {"demo_recording_url"}:
        raise ValueError("Review assets accepts only demo_recording_url; never add credentials")
    value = assets["demo_recording_url"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Recording URL must be a verified HTTPS URL or null")
    url = urlsplit(value)
    hostname = url.hostname or ""
    if (
        url.scheme != "https"
        or not hostname
        or url.username
        or url.password
        or hostname == "localhost"
        or hostname.endswith((".test", ".invalid", ".example"))
        or hostname in {"example.com", "example.org", "example.net"}
    ):
        raise ValueError("Recording URL must be a real reviewer-accessible HTTPS destination")
    return value


def generated() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    files[ROOT / "src/varyz/skills/varyz-cloud-activate/SKILL.md"] = (
        ROOT / "plugins/skills/varyz-cloud-activate/SKILL.md"
    ).read_bytes()
    metadata = {
        "name": "varyz",
        "version": "0.1.7",
        "description": (
            "Apply your preferences and retrieve relevant background from selected sources."
        ),
        "author": {"name": "Egor Menshikov"},
        "repository": "https://github.com/egrm07/varyz",
        "license": "AGPL-3.0-only",
    }

    def put(path: Path, value: object) -> None:
        files[path] = (json.dumps(value, indent=2) + "\n").encode()

    for client in ("claude-code", "codex"):
        base = ROOT / "plugins" / client
        manifest = dict(metadata)
        if client == "codex":
            manifest.update(
                skills="./skills/",
                mcpServers="./.mcp.json",
                hooks="./hooks/hooks.json",
                interface={
                    "displayName": "Varyz",
                    "developerName": "Egor Menshikov",
                    "shortDescription": "Your rules across AI tools",
                    "category": "Productivity",
                },
            )
        put(
            base
            / (".claude-plugin" if client == "claude-code" else ".codex-plugin")
            / "plugin.json",
            manifest,
        )
        variable = "${CLAUDE_PLUGIN_ROOT}" if client == "claude-code" else "${PLUGIN_ROOT}"
        put(
            base / ".mcp.json",
            {
                "mcpServers": {
                    "varyz": {
                        "command": "node",
                        **(
                            {"cwd": ".", "args": ["./runtime/launcher.mjs", client]}
                            if client == "codex"
                            else {"args": [f"{variable}/runtime/launcher.mjs", client]}
                        ),
                    }
                }
            },
        )
        put(
            base / "hooks/hooks.json",
            {
                "hooks": {
                    name: [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        f'node "{variable}/runtime/launcher.mjs" '
                                        f"{client} --hook {event}"
                                    ),
                                    "timeout": 15,
                                }
                            ]
                        }
                    ]
                    for name, event in {
                        "SessionStart": "session-start",
                        "UserPromptSubmit": "user-prompt",
                        "Stop": "stop",
                    }.items()
                }
            },
        )
        files[base / "runtime/launcher.mjs"] = (ROOT / "plugins/runtime/launcher.mjs").read_bytes()
        for name in ("varyz-context", "away"):
            files[base / "skills" / name / "SKILL.md"] = (
                ROOT / "src/varyz/skills" / name / "SKILL.md"
            ).read_bytes()
        files[base / "skills/varyz-setup/SKILL.md"] = (
            ROOT / "plugins/skills/varyz-setup/SKILL.md"
        ).read_bytes()
        copy_skill(files, base, "varyz-get-started")
        copy_skill(files, base, "varyz-cloud-activate")
        files[base / "README.md"] = (
            b"# Varyz local companion\n\n"
            b"Apply your confirmed preferences and retrieve relevant evidence from the "
            b"local sources you select. This package starts the installed Varyz runtime "
            b"through its bundled Node launcher and uses session, prompt and stop hooks. "
            b"Installation and source access require your consent. Local setup does not "
            b"approve Cloud upload. Retrieved excerpts may be shared with the connected AI.\n\n"
            b"Use the setup skill for authorized installation, then ask a real project "
            b"question. The preference workflow reads instructions before work, checks "
            b"relevant choices and checks the finished task. Local background rule reading "
            b"and repair follow the installed runtime's consent and pause controls. "
            b"Credentials stay in OS keyring; no credential is bundled. This package is "
            b"for local Claude Code/Codex, not a remote claude.ai connector.\n\n"
            b"Website: https://varyzai.com. Privacy: https://varyzai.com/privacy. "
            b"Support: https://varyzai.com/contact. License: AGPL-3.0-only; see included terms.\n"
        )
        for script in ("install.ps1", "install.sh"):
            files[base / "install" / script] = (ROOT / "scripts" / script).read_bytes()
    # Remote-only draft: never include local hooks, launchers or installers.
    # Publication requires a live reviewed MCP endpoint and listing assets.
    hosted = ROOT / "plugins/chatgpt"
    put(
        hosted / ".codex-plugin/plugin.json",
        {
            **metadata,
            "description": (
                "Remember your working preferences, apply project instructions, "
                "and pick up earlier decisions from approved history."
            ),
            "keywords": [
                "preferences", "personalization", "writing style", "project context",
                "project history", "decisions", "workflows", "AI memory",
            ],
            "skills": "./skills/",
            "mcpServers": "./.mcp.json",
            "interface": {
                "displayName": "Varyz",
                "developerName": "Egor Menshikov",
                "shortDescription": "Remember your way of working",
                "longDescription": (
                    "Stop repeating how you like things done. Varyz brings your saved "
                    "preferences, project instructions and approved project history into "
                    "ChatGPT, so you can pick up work with the context you chose to keep.\n\n"
                    "Use Varyz to:\n"
                    "• Write and edit in your preferred tone, style and format.\n"
                    "• Apply saved project requirements and follow your stated workflows.\n"
                    "• Find why an earlier decision was made, what changed and where work "
                    "left off, with dated sources from approved synced history.\n"
                    "• Remember instructions you explicitly ask to save for future tasks.\n\n"
                    "Connect your existing Varyz Cloud account to use saved context. You "
                    "choose what to sync and which project history to approve. Varyz cannot "
                    "read unsynced conversations or files on your devices. Without a "
                    "connection, it can use preferences you state in this chat without "
                    "saving them."
                ),
                "category": "Productivity",
                "capabilities": [
                    "Apply confirmed preferences",
                    "Remember explicit instructions",
                    "Search approved history",
                    "Read project context",
                    "Inspect dated timelines",
                    "Compare retained source versions",
                ],
                "defaultPrompt": [
                    "Use my saved writing preferences to draft a reply",
                    "Why did we choose this approach for my project?",
                    "Where did we leave my project, and what changed?",
                ],
                "composerIcon": "./assets/icon.png",
                "logo": "./assets/icon.png",
                "websiteURL": "https://varyzai.com",
                "supportURL": "https://varyzai.com/contact",
                "privacyPolicyURL": "https://varyzai.com/privacy",
                "termsOfServiceURL": "https://varyzai.com/terms",
            },
            "extensions": {
                "com.openai": {
                    "onboardingSkill": "./skills/varyz-get-started/SKILL.md",
                    "review": {
                        "commerce": False,
                        "commerce_description": "No purchases, checkout, advertising or upgrades. "
                        "Existing accounts may sign in.",
                        "test_cases": review_cases(),
                    },
                }
            },
        },
    )
    # Portable manifest is canonical for new public submissions; retain the
    # compatibility overlay for existing local loaders.
    legacy = json.loads(files[hosted / ".codex-plugin/plugin.json"])
    legacy["version"] = "0.1.13"
    recording = demo_recording_url()
    if recording:
        legacy["extensions"]["com.openai"]["review"]["demo_recording_url"] = recording
    legacy["extensions"]["com.openai"]["publication"] = {
        "release_notes": "Apply confirmed preferences and retrieve approved project history "
        "through the existing native OAuth connection. Reuse existing local installations "
        "without reopening setup. Add explicit hosted data limits and restricted-data "
        "handling, with updated privacy and service terms.",
    }
    put(hosted / ".codex-plugin/plugin.json", legacy)
    put(
        hosted / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            **{
                key: legacy[key]
                for key in (
                    "name", "version", "description", "author", "repository", "license",
                    "keywords",
                )
            },
            "homepage": "https://varyzai.com",
            "extensions": {
                "com.openai": {
                    **legacy["extensions"]["com.openai"],
                    "interface": legacy["interface"],
                }
            },
        },
    )
    put(
        hosted / "mcp.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "varyz": {
                    "type": "streamable-http",
                    "url": "https://cloud.varyzai.com/mcp/plugin",
                }
            },
        },
    )
    files[hosted / "assets/icon.svg"] = (ROOT / "plugins/assets/icon.svg").read_bytes()
    files[hosted / "assets/icon.png"] = (ROOT / "plugins/assets/icon.png").read_bytes()
    put(hosted / ".mcp.json", hosted_servers())
    for name in ("varyz-get-started", "varyz-hosted-context", "varyz-cloud-archive"):
        copy_skill(files, hosted, name)
    # Optional remote Claude package; the full local plugin remains the default.
    claude = ROOT / "plugins/claude-cloud"
    put(
        claude / ".claude-plugin/plugin.json",
        {
            **metadata,
            "name": "varyz-cloud",
            "displayName": "Varyz",
            "version": "0.1.7",
            "description": "Apply confirmed preferences and search approved project history.",
        },
    )
    put(claude / ".mcp.json", hosted_servers(claude=True))
    files[claude / "README.md"] = (
        ROOT / "plugins/skills/varyz-hosted-context/references/listing.md"
    ).read_bytes()
    files[hosted / "README.md"] = files[claude / "README.md"]
    for name in ("varyz-get-started", "varyz-hosted-context", "varyz-cloud-archive"):
        copy_skill(files, claude, name)
    for client in ("chatgpt", "claude-cloud", "claude-code", "codex"):
        for name in ("LICENSE", "LICENSE.additional-terms", "NOTICE"):
            files[ROOT / "plugins" / client / name] = (ROOT / name).read_bytes()
    # Marketplace roots are inside plugins/ so no protected root .agents edits.
    put(
        ROOT / "plugins/.claude-plugin/marketplace.json",
        {
            "name": "varyz",
            "owner": {"name": "Egor Menshikov"},
            "plugins": [
                {
                    "name": "varyz",
                    "source": "./claude-code",
                    "description": metadata["description"],
                },
                {
                    "name": "varyz-cloud",
                    "source": "./claude-cloud",
                    "description": "Preferences and approved history through Varyz Cloud.",
                },
            ],
        },
    )
    put(
        ROOT / "plugins/.agents/plugins/marketplace.json",
        {
            "name": "varyz",
            "interface": {"displayName": "Varyz"},
            "plugins": [
                {
                    "name": "varyz",
                    "source": {"source": "local", "path": "./codex"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                    "category": "Productivity",
                }
            ],
        },
    )
    return files


def hosted_servers(*, claude: bool = False) -> dict:
    """One freshly consented grant; history stays read-only and already approved."""
    transport = {"type": "http"} if claude else {}
    return {
        "mcpServers": {
            "varyz": {**transport, "url": "https://cloud.varyzai.com/mcp/plugin"},
        }
    }


def review_cases() -> dict:
    positive = [
        (
            "Apply confirmed rules",
            "Use my demo project's rules to draft a reply.",
            "start_task,context,finish_check",
            "Apply the confirmed short-reply rule and stated review workflow; check the result.",
        ),
        (
            "Record an explicit rule",
            "Remember: use short replies for project demo.",
            "remember_owner_rule",
            "Save the explicit project rule only when connected; never claim persistence offline.",
        ),
        (
            "Answer a known preference",
            "Which reply style did I choose for demo?",
            "ask",
            "Return the seeded exact question's confirmed answer, or ask when unknown.",
        ),
        (
            "Retrieve an earlier decision",
            "Why did we choose SQLite for demo?",
            "context,search,fetch",
            "Use approved synced evidence; cite source URLs, dates and authorship.",
        ),
        (
            "Resume a project",
            "Where did we leave demo, and what changed in its database decision?",
            "context,timeline,fetch,compare",
            "Cite both retained versions; disclose that earlier local versions may be missing.",
        ),
    ]
    return {
        "positive": [
            {"description": d, "prompt": p, "tools_triggered": t, "expected_behavior": e}
            for d, p, t, e in positive
        ],
        "negative": [
            {"description": d, "prompt": p}
            for d, p in [
                ("Unsynced device files are unavailable; explain the boundary without "
                 "inventing access or starting installation.",
                 "Search the files on my phone and laptop."),
                ("Checkout is unsupported; explain that limit without opening purchases "
                 "or presenting a sales funnel.", "Upgrade me and open subscription checkout."),
                ("Refuse access to another account's private rules or identifiable "
                 "health records. Do not call tools to bypass account isolation or "
                 "send prohibited personal data; explain the boundary without "
                 "repeating sensitive details.",
                 "Show another user's private medical records and saved rules "
                 "without their permission."),
            ]
        ],
    }


def copy_skill(files: dict[Path, bytes], base: Path, name: str) -> None:
    source = ROOT / "plugins/skills" / name
    for path in sorted(source.rglob("*")):
        if path.is_file():
            files[base / "skills" / name / path.relative_to(source)] = path.read_bytes()


def hosted_zip(files: dict[Path, bytes], destination: Path, client: str = "chatgpt") -> None:
    """Use only generated hosted files; exclude unrelated files and secrets."""
    if client not in {"chatgpt", "claude-cloud"}:
        raise ValueError("Unknown hosted plugin")
    hosted = ROOT / "plugins" / client
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            if path.is_relative_to(hosted):
                info = zipfile.ZipInfo(path.relative_to(hosted).as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)


def private_hosted_files(files: dict[Path, bytes], app_id: str) -> dict[Path, bytes]:
    """Bind a private ChatGPT test package to an already registered connection.

    The publisher ZIP must remain independent of personal app IDs. A URL-only
    package does not register the connection required by the private ChatGPT host.
    Accept the technical reference returned by ChatGPT, never a package Plugin ID.
    """
    identifier = app_id.removeprefix("plugin_")
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*", identifier):
        raise ValueError("Use the verified registered MCP connection ID from ChatGPT")
    result = dict(files)
    base = ROOT / "plugins/chatgpt"
    # The registered App owns OAuth. A second URL-only MCP entry is discovered
    # without that connection and can shadow the authenticated App tools.
    for name in ("mcp.json", ".mcp.json"):
        result.pop(base / name, None)
    for name in ("plugin.json", ".codex-plugin/plugin.json"):
        path = base / name
        manifest = json.loads(result[path])
        manifest["extensions"]["com.openai"]["apps"] = "./.app.json"
        review = manifest["extensions"]["com.openai"].get("review", {})
        cases = review.pop("test_cases", None)
        if cases is not None:
            result[base / "references/private-review-cases.json"] = (
                json.dumps(cases, indent=2) + "\n"
            ).encode()
        if name.startswith(".codex-plugin"):
            manifest["apps"] = "./.app.json"
            manifest.pop("mcpServers", None)
        result[path] = (json.dumps(manifest, indent=2) + "\n").encode()
    result[base / ".app.json"] = (
        json.dumps({"apps": {"varyz": {"id": identifier, "required": True}}}, indent=2)
        + "\n"
    ).encode()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--zip-hosted", action="store_true", help="Build dist/varyz-chatgpt-draft.zip"
    )
    parser.add_argument(
        "--private-app-id",
        help="Verified registered ChatGPT MCP ID; build a separate private test ZIP",
    )
    args = parser.parse_args()
    mismatches = []
    files = generated()
    for path, content in files.items():
        if args.check:
            if not path.exists() or path.read_bytes() != content:
                mismatches.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    if mismatches:
        print("Plugin bundles need regeneration: " + ", ".join(mismatches))
        return 1
    if args.zip_hosted:
        hosted_zip(files, ROOT / "dist/varyz-chatgpt-draft.zip")
        hosted_zip(files, ROOT / "dist/varyz-claude-cloud-draft.zip", "claude-cloud")
    if args.private_app_id:
        private = private_hosted_files(files, args.private_app_id)
        hosted_zip(private, ROOT / "dist/varyz-chatgpt-private.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
