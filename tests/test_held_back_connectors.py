"""Connectors held back from the alpha stay in the tree and out of the package.

docs/SHIPPING.md holds Gmail, Google Calendar and Drive, the calendar-file
import and the personal archive back from the alpha without deleting
them, and with them `varyz enrich` and the managed model's installer. Two
things have to stay true for that: the build leaves every one of their modules
out, and a varyz without those modules still imports, registers the alpha
connectors, and offers no command for a missing module.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import varyz.connectors as connectors

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
EXCLUDED = set(PYPROJECT["tool"]["uv"]["build-backend"]["source-exclude"])
# `varyz enrich` and the managed model's installer are held back with them;
# everything else excluded is a connector.
ENRICH = "src/varyz/pipeline/enrich.py"
LOCAL_INSTALL = "src/varyz/llm/local_install.py"

# Modules only the held-back connectors import; they go with them.
PRIVATE_HELPERS = ("google_auth", "google_gmail")
ALPHA = ["ai_conversations", "documents", "git", "github", "manual"]


def test_the_build_excludes_every_held_back_module_and_nothing_else():
    held = {*connectors.HELD_BACK, *PRIVATE_HELPERS}
    connector_paths = {f"src/varyz/connectors/{name}.py" for name in held}
    assert connector_paths | {ENRICH, LOCAL_INSTALL} == EXCLUDED
    for path in EXCLUDED:
        assert (ROOT / path).exists(), f"{path} is excluded but no longer in the tree"


def test_no_shipped_module_imports_a_held_back_one():
    held = {*connectors.HELD_BACK, *PRIVATE_HELPERS}
    for source in (ROOT / "src" / "varyz").rglob("*.py"):
        if f"src/varyz/connectors/{source.stem}.py" in EXCLUDED:
            continue
        text = source.read_text(encoding="utf-8")
        for name in held:
            # cli.py imports two of them inside commands registered only when
            # is_shipped() finds the module.
            if source.name == "cli.py" and name in {"gmail_imap", "calendar_ics"}:
                continue
            assert f"varyz.connectors.{name} import" not in text, (source, name)
            assert f"varyz.connectors import {name}" not in text, (source, name)


_WITHOUT_HELD_BACK = """
import importlib.abc, json, sys
HELD = {held!r}

class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in HELD:
            raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
        return None

sys.meta_path.insert(0, Absent())
import varyz.connectors as c
import varyz.cli as cli
from varyz.config import GraphConfig
from varyz.llm.local_runtime import compile_command, install_hint
connect = sorted(i.name for i in cli.connect_app.registered_commands if i.name)
top = sorted(i.name or i.callback.__name__ for i in cli.app.registered_commands)
local_model = sorted(i.name for i in cli.local_model_app.registered_commands if i.name)
print(json.dumps({{
    "ids": c.all_connector_ids(), "connect": connect, "top": top,
    "local_model": local_model, "hint": install_hint(),
    "extractor": GraphConfig().extractor, "compile": compile_command(),
}}))
"""


def test_varyz_without_the_held_back_modules_imports_and_registers_the_alpha_set():
    held = sorted(f"varyz.connectors.{n}" for n in (*connectors.HELD_BACK, *PRIVATE_HELPERS))
    held.extend(["varyz.pipeline.enrich", "varyz.llm.local_install"])
    result = subprocess.run(
        [sys.executable, "-c", _WITHOUT_HELD_BACK.format(held=held)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    shown = json.loads(result.stdout.strip().splitlines()[-1])
    assert shown["ids"] == ALPHA
    assert "gmail" not in shown["connect"]
    assert "calendar" not in shown["connect"]
    assert "github" in shown["connect"]
    assert "enrich" not in shown["top"]
    assert "setup" in shown["top"]
    # The alpha never downloads the managed model, and says so rather than
    # naming the command it left out.
    assert shown["local_model"] == ["status"]
    assert shown["hint"] == "this build of varyz does not install it"
    # It builds the graph and the personal model it can build without one.
    assert shown["extractor"] == "deterministic"
    assert shown["compile"] == "varyz model compile --literal-only --run"


def test_the_source_tree_still_registers_the_held_back_connectors():
    ids = set(connectors.all_connector_ids())
    assert {"gmail", "calendar_import", "personal_archive"} <= ids
    assert {"google_calendar", "google_drive"} <= ids
    # varyz does not read chat apps, contacts or browsing history.
    assert not {
        "browser_history", "google_contacts", "instagram", "telegram", "whatsapp"
    } & ids


def test_the_source_tree_still_offers_enrich_and_nothing_shipped_imports_it():
    from varyz import cli

    assert "enrich" in {i.name for i in cli.app.registered_commands}
    for source in (ROOT / "src" / "varyz").rglob("*.py"):
        if source.name in {"cli.py", "enrich.py"}:
            continue
        text = source.read_text(encoding="utf-8")
        assert "varyz.pipeline.enrich import" not in text, source
        assert "varyz.pipeline import enrich" not in text, source


def test_the_source_tree_still_offers_the_model_installer():
    from varyz import cli
    from varyz.config import GraphConfig
    from varyz.llm.local_runtime import install_hint

    assert {"install", "status"} == {i.name for i in cli.local_model_app.registered_commands}
    assert install_hint() == "run: varyz local-model install"
    assert GraphConfig().extractor == "contextual_facets"


# The calls that download the managed model or its runtime.
_INSTALLER_CALLS = {"install", "install_model", "install_runtime"}


def test_nothing_shipped_downloads_the_managed_model():
    for source in (ROOT / "src" / "varyz").rglob("*.py"):
        # local_runtime.py defines the download; local_install.py, which the
        # build leaves out, is the one place that starts it.
        if source.name in {"local_runtime.py", "local_install.py"}:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _INSTALLER_CALLS
            ):
                raise AssertionError(f"{source.name}:{node.lineno} calls .{node.func.attr}()")


@pytest.mark.parametrize("semantic_provider", ["local", "hybrid"])
def test_a_semantic_graph_without_the_installer_is_refused_by_name(
    conn, cfg, monkeypatch, semantic_provider
):
    """The semantic graph is held back with the model, the cloud provider too."""
    from varyz.graph.service import rebuild_graph
    from varyz.llm.local_runtime import LocalModelNotInstalled

    cfg.graph.extractor = "contextual_facets"
    cfg.graph.semantic_provider = semantic_provider
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)

    with pytest.raises(LocalModelNotInstalled) as raised:
        rebuild_graph(conn, cfg)

    assert "local-model install" not in str(raised.value)
    assert "deterministic extractor only" in str(raised.value)
    assert 'extractor = "deterministic"' in str(raised.value)
    assert conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 0


def test_a_build_without_the_installer_builds_the_deterministic_graph_unasked(
    conn, tmp_path, monkeypatch
):
    from varyz.config import Config, StorageConfig
    from varyz.graph.service import rebuild_graph

    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    cfg = Config(storage=StorageConfig(data_dir=tmp_path / "data"), base_dir=tmp_path)

    assert cfg.graph.extractor == "deterministic"
    rebuild_graph(conn, cfg)
    assert conn.execute(
        "SELECT extractor_name FROM graph_runs WHERE state='active'"
    ).fetchone()[0] == "deterministic"
