"""What the package metadata promises is checked like every other claim.

A classifier is read by people deciding whether to install, and by tooling that
never asks whether the promise is kept. `Typing :: Typed` was in this list
without the `py.typed` marker PEP 561 requires and without any type checker
running here, which is a claim in the only place a stranger would look for one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]


def test_the_typed_claim_and_the_marker_stand_or_fall_together():
    claimed = "Typing :: Typed" in PROJECT["classifiers"]
    marked = (ROOT / "src" / "varyz" / "py.typed").exists()
    assert claimed == marked, (
        "Typing :: Typed promises inline types; PEP 561 delivers them through "
        "src/varyz/py.typed. Ship both or neither."
    )


def test_the_licence_files_the_package_carries_are_the_ones_that_exist():
    for name in PROJECT["license-files"]:
        assert (ROOT / name).exists(), name
    assert PROJECT["license"] == "AGPL-3.0-only"


def test_every_advertised_url_points_at_the_project():
    urls = PROJECT["urls"]
    assert set(urls) >= {"Homepage", "Repository", "Issues"}
    assert urls["Homepage"] == "https://varyzai.com"
    for name, url in urls.items():
        if name == "Homepage":
            continue
        assert url.startswith("https://github.com/egrm07/varyz"), f"{name}: {url}"


def test_the_uninstall_command_names_the_distribution_that_was_installed():
    """`uv tool uninstall` takes the distribution name, which is not the command.

    The module, the executable and every path on disk are `varyz`; the
    distribution is `varyzai`, because `varyz` was taken on PyPI. So the
    obvious command is the one that fails -- "error: `varyz` is not installed"
    -- and it was printed by the program itself at the end of `varyz
    uninstall`, and by the README and the agent guide.
    """

    name = PROJECT["name"]
    printed = (ROOT / "src" / "varyz" / "cli.py").read_text(encoding="utf-8")
    assert f"uv tool uninstall {name}" in printed
    for doc in ("README.md", "docs/AGENT_SETUP.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert f"uv tool uninstall {name}" in text, doc
        lines = [line.strip() for line in text.splitlines()]
        assert "uv tool uninstall varyz" not in lines, doc
