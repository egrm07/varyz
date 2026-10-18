"""Build a review handoff from explicit generated-file allowlists (no credentials).

This prepares artifacts; it never declares unrun native/platform checks successful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "dist/submission-current"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--legal-dir", type=Path,
        help="Optional policy HTML folder; private website sources are never implicit inputs",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Use a new output folder; retained review candidates are immutable")
    legal = []
    if args.legal_dir is not None:
        legal = [args.legal_dir.resolve() / name for name in ("privacy.html", "terms.html")]
        if not all(path.is_file() for path in legal):
            raise SystemExit("The legal folder must contain privacy.html and terms.html")
    builder = runpy.run_path(str(ROOT / "scripts/plugin/build.py"))
    files = builder["generated"]()
    output.mkdir(parents=True)
    for client in ("chatgpt", "claude-cloud"):
        base = ROOT / "plugins" / client
        selected = {
            path.relative_to(base).as_posix(): content
            for path, content in files.items()
            if path.is_relative_to(base)
        }
        manifest_name = "plugin.json" if client == "chatgpt" else ".claude-plugin/plugin.json"
        version = json.loads(selected[manifest_name])["version"]
        destination = output / f"varyz-{client}-{version}-review.zip"
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(selected.items()):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        # A repository-folder candidate for Claude's directory, without the private
        # application checkout, models, wheels, tests, environment or personal data.
        if client == "claude-cloud":
            for name, content in selected.items():
                target = output / "claude-repository" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    for source in (
        "docs/PLUGIN_SUBMISSION.md",
        "docs/submission/REVIEWER_GUIDE.md",
        "docs/submission/WALKTHROUGH.md",
    ):
        path = ROOT / source
        if path.is_file():  # PLUGIN_SUBMISSION.md is a local, unpublished handoff
            (output / path.name).write_bytes(path.read_bytes())
    # Optional deployment inputs stay separate from the distributable plugin ZIPs.
    for source in legal:
        target = output / "legal-pages" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    checksums = {
        path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "checksums.json"
    }
    (output / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n")
    print(f"Review handoff prepared in {output}; submission gates in PLUGIN_SUBMISSION.md")
    policy_status = "Policy HTML included" if legal else "Policy HTML omitted; review live URLs"
    print(policy_status)


if __name__ == "__main__":
    main()
