"""Validate and package the separate guest connector without credentials."""

import json
import shutil
import zipfile
from pathlib import Path

from varyz.cloud.sharing_models import TOOLS

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins/varyz-shared"


def main():
    manifest = json.loads((PACKAGE / "plugin.json").read_text())
    assert manifest["name"] == PACKAGE.name
    assert manifest["version"] == "0.1.1"
    interface = manifest["extensions"]["com.openai"]["interface"]
    assert len(interface["shortDescription"]) <= 30
    assert not set(manifest) & {"skills", "mcpServers", "apps", "interface"}
    mcp = json.loads((PACKAGE / "mcp.json").read_text())
    assert mcp["mcpServers"] == {
        "varyz-shared": {
            "type": "streamable-http",
            "url": "https://cloud.varyzai.com/mcp/shared",
        }
    }
    skill = (PACKAGE / "skills/shared-project/SKILL.md").read_text()
    assert all("`" + name + "`" in skill for name in TOOLS)
    assert skill.startswith("---\nname: shared-project\n")
    for name in ("LICENSE", "NOTICE"):
        source = ROOT / name
        if source.is_file():
            shutil.copyfile(source, PACKAGE / name)
    compat = PACKAGE / ".codex-plugin"
    compat.mkdir(exist_ok=True)
    overlay = {k: manifest[k] for k in ("name", "version", "description", "author", "license")}
    overlay.update({"skills": "./skills/", "mcpServers": "./.mcp.json", "interface": interface})
    (compat / "plugin.json").write_text(json.dumps(overlay, indent=2) + "\n")
    (PACKAGE / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
    archive = ROOT / (".tmp/varyz-shared-" + manifest["version"] + ".zip")
    archive.parent.mkdir(exist_ok=True)
    paths = sorted(path for path in PACKAGE.rglob("*") if path.is_file())
    assert all(not p.is_symlink() and p.stat().st_size < 5 * 1024 * 1024 for p in paths)
    assert all(p.suffix not in (".sqlite", ".env", ".key", ".pyc") for p in paths)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in paths:
            output.write(path, path.relative_to(PACKAGE.parent).as_posix())
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
        assert set(p.split("/")[0] for p in output.namelist()) == {manifest["name"]}
    print(
        json.dumps(
            {
                "archive": str(archive),
                "files": len(paths),
                "tools": sorted(TOOLS),
                "validated": True,
            }
        )
    )


if __name__ == "__main__":
    main()
