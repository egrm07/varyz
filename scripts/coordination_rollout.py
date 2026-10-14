"""Prepare/apply only task-coordination edits, preserving unrelated runtime changes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(text: str, start: str, end: str) -> str:
    if text.count(start) != 1:
        raise ValueError(f"Ambiguous coordination anchor: {start!r}")
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def payload(surface: str) -> dict:
    replacements: dict[str, list[list[str]]] = {}

    def pair(file: str, before: str, after: str) -> None:
        replacements.setdefault(file, []).append([before, after])

    def text(file: str) -> str:
        return (ROOT / "src/varyz" / file).read_text(encoding="utf-8")

    additions = {"coordination.py": text("coordination.py")}
    if surface == "local":
        file = "mcp_server.py"
        source = text(file)
        anchor = "from varyz.config import Config, ConfigError, find_config_file, load_config\n"
        pair(file, anchor, anchor +
             "from varyz.coordination import ROUTE_DESCRIPTION, ROUTE_SCHEMA, WORKFLOW_TOOLS\n")
        anchor = ('TOOLS[:] = [tool for tool in TOOLS '
                  'if tool["name"] not in {"advise", "resolve"}]\n')
        block = section(source, "TOOLS.append({\n", "\n\n# The owner's turns")
        pair(file, anchor, anchor + "\n" + block + "\n")
        anchor = "    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:\n"
        pair(file, anchor, section(source, anchor, '        if name == "cloud_offer":'))
        block = section(source, "    notices = (\n", "    if server._promotion_available():")
        pair(file, '    notices = ""\n', block)
        anchor = '    if server._promotion_available():\n        notices = (\n'
        pair(file, anchor, anchor.replace("notices =", "notices +="))

        file = "resolution/briefing.py"
        source = text(file)
        for message in ("what the user asked you to do", "what you were asked to do"):
            anchor = f'        raise ValueError("task is required: {message}")\n'
            pair(file, anchor, section(source, anchor, "    principles ="))
        anchor = ('    """The briefing as compact text, for hooks that inject it '
                  'into the agent\'s context."""\n')
        pair(file, anchor, section(source, anchor, "    lines ="))
        file = "hooks.py"
        anchor = "        # The reader's ask rides on the briefing every session gets, not only\n"
        pair(file, anchor, '        if briefing.get("status") == "delegated":\n'
             "            return None\n" + anchor)
        additions["db/migrations/0045_task_routes.sql"] = text("db/migrations/0045_task_routes.sql")
        file = "skills/varyz-context/SKILL.md"
        anchor = "# Varyz context\n"
        pair(file, anchor, section(text(file), anchor, "You plan,"))
    elif surface == "cloud":
        file = "cloud/app.py"
        source = text(file)
        anchor = "from varyz.resolution.questioning import QUESTION_POLICY\n"
        pair(file, anchor,
             "from varyz.coordination import ROUTE_DESCRIPTION, ROUTE_SCHEMA, "
             "WORKFLOW_TOOLS, guard, route\n"
             + anchor)
        anchor = "TOOL_NAMES = (\n"
        pair(file, anchor, anchor + '    "route_task",\n')
        anchor = ('                    "before reporting. '
                  'Cloud has no direct local archive access."\n')
        pair(file, anchor, section(source, anchor, "                    + ("))
        anchor = ('        if method == "tools/list":\n'
                  '            for tool in result["tools"]:\n'
                  '                name = tool["name"]\n')
        pair(file, anchor, section(source, '        if method == "tools/list":\n',
                                   '                tool["title"] ='))
        anchor = "    def call_tool(owner, name, args):\n"
        pair(file, anchor, section(source, anchor, "        if name not in TOOL_NAMES:"))
        anchor = '            db.execute("DELETE FROM accounts WHERE id=?", (owner["id"],))\n'
        pair(file, anchor,
             '            db.execute("DELETE FROM varyz_task_routes WHERE namespace=?", '
             '(owner["id"],))\n'
             + anchor)
        file = "cloud/store.py"
        anchor = "            CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY,\n"
        pair(file, anchor, section(text(file),
             "            CREATE TABLE IF NOT EXISTS varyz_task_routes", anchor) + anchor)
        file = "cloud/postgres.py"
        anchor = "CREATE TABLE IF NOT EXISTS changes(\n"
        pair(file, anchor, section(text(file),
             "CREATE TABLE IF NOT EXISTS varyz_task_routes", anchor) + anchor)
    else:
        raise ValueError("Unknown coordination surface")
    return {"surface": surface, "replacements": replacements, "additions": additions}


def apply_to(package: Path, data: dict, backup: Path) -> int:
    """Validate every anchor first; back up bytes before modifying any file."""
    package = package.resolve(strict=True)
    planned: dict[Path, str] = {}
    for relative, pairs in data["replacements"].items():
        path = package / relative
        original = path.read_text(encoding="utf-8")
        changed = original
        for before, after in pairs:
            if after in changed:
                continue
            if changed.count(before) != 1:
                raise ValueError(f"Installed coordination anchor changed: {relative}")
            changed = changed.replace(before, after, 1)
        if changed != original:
            planned[path] = changed
    for relative, content in data["additions"].items():
        path = package / relative
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        if path.exists():
            raise ValueError(f"A different coordination addition already exists: {relative}")
        planned[path] = content
    for path, content in planned.items():
        if not path.resolve().is_relative_to(package):
            raise ValueError("Patch escapes the installed package")
        if path.suffix == ".py":
            compile(content, str(path), "exec")
    if not planned:
        return 0
    backup.mkdir(parents=True, exist_ok=False)
    originals = {path: path.read_bytes() if path.exists() else None for path in planned}
    for path, content in originals.items():
        if content is not None:
            saved = backup / path.relative_to(package)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(content)
    (backup / "manifest.json").write_text(json.dumps({
        "package": str(package), "added": [str(path.relative_to(package))
                                            for path, value in originals.items() if value is None],
    }), encoding="utf-8")
    try:
        # Publish dependency additions before modules that import them.
        planned = dict(sorted(planned.items(), key=lambda item: (
            item[0].relative_to(package).as_posix() not in data["additions"], str(item[0]))))
        for path, content in planned.items():
            if (path.read_bytes() if path.exists() else None) != originals[path]:
                raise ValueError("Installed files changed during patch preparation")
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(path.name + ".coordination-new")
            staging.write_text(content, encoding="utf-8")
            os.replace(staging, path)
    except BaseException:
        for path, content in originals.items():
            if content is not None:
                path.write_bytes(content)
            elif path.exists():
                path.unlink()
        raise
    return len(planned)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=["local", "cloud"], required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = payload(args.surface)
    if args.output:
        args.output.write_text(json.dumps(data), encoding="utf-8")
    if args.package:
        if not args.backup:
            parser.error("--backup is required to apply")
        count = apply_to(args.package, data, args.backup)
        print(json.dumps({"patched_files": count, "backup": str(args.backup)}))
    else:
        print(json.dumps({"surface": args.surface, "prepared": True}))


if __name__ == "__main__":
    main()
