"""Shared fixtures: temp DB + vault, fake embedder/LLM (offline, free)."""

# The curated-retirement declarations intentionally precede the ordinary
# fixture imports so pytest sees them during collection.
# ruff: noqa: E402

from __future__ import annotations

# These modules exercise features the curated pivot manifest explicitly removed.
# Keeping them out of collection is the test-side half of deleting dangling lazy
# imports; the replacement resolution surface has dedicated contract, core, UI,
# identity and offline-loop coverage in the new test modules.
collect_ignore = [
    "test_behavior_search.py",          # retired RAG synthesis path
    "test_conversation_retrieval.py",   # retired understand synthesis path
    "test_distill.py",                  # retired RAG synthesis path
    "test_measure_compile_run.py",      # unshipped experiment script
    "test_observation_cost.py",         # unshipped experiment script
    "test_private_context.py",          # mixed with retired action/RAG adapters
    "test_temporal_graph.py",           # mixed with retired understand path
]

_RETIRED_TESTS = {
    # Replaced RAG ask and Mirror prose surfaces.
    "tests/test_cli.py::test_ordinary_ask_never_constructs_the_managed_model",
    "tests/test_cli.py::test_local_distill_uses_the_conservative_interactive_shape",
    "tests/test_cli.py::test_ask_names_the_account_that_is_already_connected",
    "tests/test_cli.py::test_ask_does_not_collect_approval_for_a_call_it_cannot_make",
    # Legacy onboarding and RAG Ask command surfaces.
    "tests/test_cli.py::test_asking_an_empty_archive_says_it_is_empty_and_sends_nothing",
    "tests/test_cli.py::test_ask_help_makes_local_gpu_use_an_explicit_per_call_choice",
    "tests/test_cli.py::test_an_empty_query_is_not_answered_with_the_archive",
    "tests/test_release_copy.py::test_the_command_gate_would_catch_an_invented_command",
    # Caller-bound consent intentionally invalidates old local_user grants.
    "tests/test_mcp_personal_model.py::test_lookup_requires_one_time_permission_for_relationship_necessary_category",
    "tests/test_mcp_personal_model.py::test_interpret_requires_one_time_permission_for_third_party_category",
    # The understand synthesis tool was replaced by resolve.
    "tests/test_mcp_server.py::test_the_whole_surface_has_archive_and_explicit_personal_model_lanes",
    "tests/test_mcp_server.py::test_understand_without_an_active_graph_names_recall_as_the_fallback",
    "tests/test_mcp_server.py::test_understand_returns_graph_claims_with_exact_source_addresses",
    "tests/test_mcp_server.py::test_understand_filters_sensitive_claims_before_filling_the_limit",
    "tests/test_mcp_server.py::test_understand_says_when_the_gate_emptied_it_rather_than_the_archive",
    "tests/test_mcp_server.py::test_understand_surfaces_both_sides_of_a_graph_contradiction",
}


def pytest_collection_modifyitems(config, items):
    """Mark only manifest-retired behavior; retained tests still run normally."""
    marker = pytest.mark.skip(reason="surface intentionally removed by MANIFEST.md")
    for item in items:
        normalized = item.nodeid.replace("\\", "/")
        if normalized in _RETIRED_TESTS:
            item.add_marker(marker)

import csv
import difflib
import errno
import hashlib
import io
import itertools
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

from varyz.config import Config, StorageConfig
from varyz.db.connection import open_db
from varyz.vault import Vault

UnreadablePath = Callable[[Path], AbstractContextManager[Path]]


def synthetic_executable(path: Path) -> Path:
    """Create a disposable runtime fixture; permission changes stay centralized."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic runtime", encoding="utf-8")
    path.chmod(0o755)
    return path


def _readable(path: Path) -> bool:
    try:
        if path.is_dir():
            with os.scandir(path) as entries:
                next(entries, None)
        else:
            with path.open("rb") as stream:
                stream.read(1)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
            return False
        raise
    return True


def _run_permission_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)


def _command_failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"exit status {result.returncode}").strip()


def _windows_tool(name: str) -> str | None:
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        system_tool = Path(system_root) / "System32" / name
        if system_tool.is_file():
            return str(system_tool)
    return shutil.which(name)


def _windows_sid() -> str | None:
    whoami = _windows_tool("whoami.exe")
    if whoami is None:
        return None
    result = _run_permission_command([whoami, "/user", "/fo", "csv", "/nh"])
    if result.returncode != 0:
        return None
    try:
        row = next(csv.reader(io.StringIO(result.stdout)))
    except (StopIteration, csv.Error):
        return None
    if len(row) < 2 or not row[1].startswith("S-"):
        return None
    return row[1]


def _save_windows_acl(path: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    icacls = _windows_tool("icacls.exe")
    assert icacls is not None
    return _run_permission_command(
        [icacls, str(path), "/save", str(destination), "/t", "/q"]
    )


def _windows_acl_entries(snapshot: Path) -> str:
    # Adding then removing an ACE makes Windows mark the unchanged inherited DACL
    # as auto-inherited (``AI``). Compare the actual entries, not that bookkeeping bit.
    return snapshot.read_text(encoding="utf-16-le").replace("D:AI", "D:")


def _windows_reinherits_on_change(snapshot: Path) -> bool:
    """Whether any change to these DACLs makes Windows add entries of its own.

    GitHub's Windows runner creates children of pytest's 0o700 folder with the
    parent's entries copied in as explicit ones, none marked inherited. The
    first change to such an unprotected DACL, however it is undone, makes
    Windows add the parent's inheritable entries back as inherited copies
    beside them, so the original cannot be restored exactly.
    """
    dacls = snapshot.read_text(encoding="utf-16-le").splitlines()[1::2]
    for dacl in dacls:
        flags = dacl.removeprefix("D:").partition("(")[0]
        aces = re.findall(r"\(([^)]*)\)", dacl)
        if "P" not in flags and aces and not any("ID" in ace.split(";")[1] for ace in aces):
            return True
    return False


def _acl_difference(backup: Path, verification: Path) -> str:
    # Which entries changed, so a machine that fails this can be understood
    # from its log alone.
    changed = difflib.unified_diff(
        _windows_acl_entries(backup).splitlines(),
        _windows_acl_entries(verification).splitlines(),
        "before",
        "after",
        lineterm="",
    )
    return "restored DACL differs:\n" + "\n".join(changed)


def _restore_windows_acl(
    path: Path,
    targets: list[Path],
    sid: str,
    backup: Path,
    verification: Path,
) -> None:
    icacls = _windows_tool("icacls.exe")
    assert icacls is not None
    for target in reversed(targets):
        result = _run_permission_command(
            [icacls, str(target), "/remove:d", f"*{sid}", "/q"]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"could not remove the temporary deny ACE from {target}: "
                f"{_command_failure(result)}; the original DACL is saved at {backup}"
            )
    verified = _save_windows_acl(path, verification)
    if verified.returncode != 0 or _windows_acl_entries(verification) != _windows_acl_entries(
        backup
    ):
        detail = _command_failure(verified) if verified.returncode else _acl_difference(
            backup, verification
        )
        raise RuntimeError(
            f"could not verify exact DACL restoration for {path}: {detail}; "
            f"the original DACL is saved at {backup}"
        )


@contextmanager
def _windows_unreadable(path: Path, backup: Path, verification: Path) -> Iterator[Path]:
    icacls = _windows_tool("icacls.exe")
    if icacls is None:
        pytest.skip("icacls is unavailable; cannot make a path unreadable safely")
    sid = _windows_sid()
    if sid is None:
        pytest.skip("the current Windows SID is unavailable; cannot restore an ACL exactly")

    saved = _save_windows_acl(path, backup)
    if saved.returncode != 0:
        pytest.skip(f"cannot save the path DACL before revoking it: {_command_failure(saved)}")
    if sid.casefold() in backup.read_text(encoding="utf-16-le").casefold():
        backup.unlink()
        pytest.skip("the path already has a user-specific ACE, so adding a deny is not reversible")
    if _windows_reinherits_on_change(backup):
        backup.unlink()
        pytest.skip("the path's DACL has no inherited entries, so Windows rewrites it on change")

    targets = sorted(
        [path, *path.rglob("*")] if path.is_dir() else [path],
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for target in targets:
        permissions = "(RD,REA,RA,DE,DC)" if target.is_dir() else "(RD,REA,RA,DE)"
        revoked = _run_permission_command(
            [icacls, str(target), "/deny", f"*{sid}:{permissions}", "/q"]
        )
        if revoked.returncode != 0:
            _restore_windows_acl(path, targets, sid, backup, verification)
            pytest.skip(f"cannot install a temporary deny ACE: {_command_failure(revoked)}")

    try:
        if _readable(path):
            pytest.skip("this Windows filesystem did not enforce the temporary deny ACE")
        yield path
    finally:
        _restore_windows_acl(path, targets, sid, backup, verification)
        backup.unlink(missing_ok=True)
        verification.unlink(missing_ok=True)


@contextmanager
def _posix_unreadable(path: Path) -> Iterator[Path]:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0)
    try:
        if _readable(path):
            pytest.skip("this user or filesystem can still read a mode-000 path")
        yield path
    finally:
        path.chmod(original_mode)


@pytest.fixture(autouse=True)
def _isolate_from_real_config(monkeypatch, tmp_path):
    """Never let a developer's VARYZ_CONFIG env var point tests at the real
    database or live connectors (running the WhatsApp bridge, etc.), and keep
    OAuth token files out of the real home directory."""
    monkeypatch.delenv("VARYZ_CONFIG", raising=False)
    monkeypatch.setenv("VARYZ_HOME", str(tmp_path / ".varyz"))
    # Autosync records the owner's picks from ~/.claude transcripts; a test
    # reads only the ones it wrote.
    monkeypatch.setattr(
        "varyz.resolution.decision_capture._default_home", lambda: tmp_path / "home"
    )
    # After an update, an agent session's server and `varyz update` refresh the
    # connected clients' Varyz files under the real home; tests never do.
    monkeypatch.setattr("varyz.mcp_server._refresh_connections_later", lambda _config: None)
    monkeypatch.setattr("varyz.update._refresh_agents", lambda _config, _python: None)
    # News is on by default and checked on use: a hook, an MCP call or a CLI
    # read would start a real fetch of varyzai.com. Tests never launch one.
    monkeypatch.setattr("varyz.news._launch", lambda _command, _cwd: None)
    # This checkout is created by the desktop sandbox, whose synthetic owner
    # differs from the Windows user. Trust this exact repository only inside
    # child git commands spawned by tests; do not mutate the user's git config.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(Path.cwd()))


@pytest.fixture(autouse=True)
def _no_live_local_model(monkeypatch):
    """Nothing in the suite may launch a managed model or reach Ollama.

    `graph rebuild` asks the provider whether it is available and falls back to
    an error when it is not, so a machine with Ollama up ran a live model
    inside a unit test and a machine without it failed the same test — which is
    how this was found, the suite passing and then failing minutes apart with
    no edit between. Neither outcome is a test result.

    Patched at `available` rather than at the HTTP client, because the module's
    `httpx` is the same object every other module imports and blocking it there
    took the OAuth tests' own local callback server down with it.
    """
    monkeypatch.setattr(
        "varyz.llm.ollama_local.OllamaProvider.available", lambda self: False
    )
    monkeypatch.setattr(
        "varyz.llm.local_runtime.ManagedLlamaRuntime.available", lambda self: False
    )
    # The workflow task classifier would ask Ollama and load the embedding
    # model; the suite sizes tasks by the lexicon, and tests of the classifier
    # hand it fakes.
    from varyz.resolution.task_size import ChainSizer

    monkeypatch.setattr(
        "varyz.resolution.task_size.sizer_from_config", lambda cfg: ChainSizer([])
    )
    monkeypatch.setattr(
        "varyz.resolution.task_size.classifier_status",
        lambda cfg: (True, "verb list (test suite)"),
    )
    # The same for the other questions the local model answers: the suite runs
    # their rules, and each classifier's own tests hand it a fake model. The
    # session warm-up would spawn a process that loads the model.
    monkeypatch.setattr("varyz.resolution.task_size.gate_sizer_from_config", lambda cfg: None)
    monkeypatch.setattr(
        "varyz.resolution.prompt_kind.prompt_classifier_from_config",
        lambda cfg, **kwargs: None,
    )
    monkeypatch.setattr("varyz.resolution.prompt_kind.warm_for_session", lambda cfg: None)
    monkeypatch.setattr("varyz.resolution.scope.scope_model_from_config", lambda cfg: None)
    monkeypatch.setattr(
        "varyz.resolution.paraphrase_verify.paraphrase_verifier_from_config", lambda cfg: None
    )
    monkeypatch.setattr(
        "varyz.resolution.evidence_verify.evidence_verifier_from_config", lambda cfg: None
    )
    # `rules mine` reads with the cloud model first, through whatever sign-in
    # this machine has; a test that wants it hands it a fake.
    from varyz.resolution.rule_reader import ReaderUnavailable

    def _no_cloud_reader(cfg, **kwargs):
        raise ReaderUnavailable("no cloud sign-in in the test suite")

    monkeypatch.setattr(
        "varyz.resolution.rule_reader.cloud_reader_from_config", _no_cloud_reader
    )


@pytest.fixture(autouse=True)
def _no_real_headless_reader(monkeypatch):
    """Nothing in the suite may start the owner's Claude Code or Codex as a reader.

    A hook or `start_task` hands the reading to `headless_reader`, which finds
    the real CLIs and spends the owner's plan; tests that want a runner pass
    their own fakes.
    """
    from varyz.resolution import headless_reader

    monkeypatch.setattr(headless_reader, "find_runners", lambda cfg, config_file: {})
    monkeypatch.setattr(headless_reader, "spawn", lambda config_file, kind=None: None)


@pytest.fixture(autouse=True)
def _no_real_setup_installs(monkeypatch):
    """Nothing in the suite may register autosync or install and start Ollama.

    Setup's last steps do exactly that, and the setup tests run setup through:
    unguarded, a run registered a launchd job, downloaded Ollama, started it
    and pulled its model until the disk was full. Each step's own tests take
    the real function from `varyz.cli` at import time and hand it fakes.
    """
    from varyz import cli

    monkeypatch.setattr(cli, "_build_graph", lambda cfg_path, on_progress=None: (
        "done", "graph (test suite)"))
    monkeypatch.setattr(cli, "_install_autosync", lambda cfg_path: (
        "skipped", "not installed by the test suite"))
    monkeypatch.setattr(cli, "_ready_classifier", lambda cfg_path, on_progress=None: (
        "skipped", "not installed by the test suite", False))
    monkeypatch.setattr(cli, "_restart_daemon", lambda cfg_path: True)
    monkeypatch.setattr("varyz.resolution.local_reader.request", lambda cfg, config_file: False)
    # Indexing fetches the search models first; their own tests take the real
    # fetch from `varyz.pipeline.model_download` and hand it a fake Hub.
    monkeypatch.setattr(cli, "fetch_search_models", lambda cfg, on_progress=None: True)
    monkeypatch.setattr(cli, "_mac_folder_access", lambda cfg_path, **kwargs: None)
    monkeypatch.setattr(cli, "_open_folder_access", lambda privacy: None)
    # On a Mac, installing autosync builds the Varyz app with osacompile. The
    # app's own tests take the real builder at import time.
    monkeypatch.setattr("varyz.mac_app.build_app", lambda directory, bundle_id, **kwargs: None)
    # A classifier asked while Ollama is down starts the recorded one.
    monkeypatch.setattr(
        "varyz.resolution.ollama_setup.start_recorded", lambda host, port, **kwargs: False
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        storage=StorageConfig(data_dir=tmp_path / "data"),
        base_dir=tmp_path,
    )
    # Unit tests must not launch or call a developer's local model.
    # Distillation tests inject their own local provider explicitly.
    config.retrieval.distill = "off"
    return config


@pytest.fixture
def unreadable_path(tmp_path_factory: pytest.TempPathFactory) -> UnreadablePath:
    """Temporarily deny reads, restoring the exact prior mode/DACL on every exit."""
    pytest_temp_root = tmp_path_factory.getbasetemp().resolve()
    backup_root = tmp_path_factory.mktemp("acl-backups")
    backup_numbers = itertools.count()

    def make_unreadable(path: Path) -> AbstractContextManager[Path]:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_relative_to(pytest_temp_root):
            raise ValueError("unreadable_path only accepts paths below pytest's temporary root")
        if sys.platform == "win32":
            backup = backup_root / f"acl-{next(backup_numbers)}.txt"
            verification = backup.with_suffix(".restored.txt")
            return _windows_unreadable(resolved, backup, verification)
        return _posix_unreadable(resolved)

    return make_unreadable


@pytest.fixture
def conn(cfg: Config):
    c = open_db(cfg.db_path)
    yield c
    c.close()


@pytest.fixture
def vault(cfg: Config) -> Vault:
    return Vault(cfg.vault_dir)


class FakeEmbedder:
    """Deterministic 384-dim vectors from a text hash: identical text ->
    identical vector, so exact-text queries rank their entry first."""

    model_name = "fake-embedder-384"

    def _vec(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.strip().lower().encode()).digest()
        material = b"".join(
            hashlib.sha256(seed + i.to_bytes(2, "big")).digest() for i in range(12)
        )
        return [b / 255.0 for b in material[:384]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FakeLLM:
    """Records every prompt it receives — the sensitivity audit inspects them."""

    def __init__(self, reply: str = "canned answer [1]"):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def stream(self, system: str, user: str):
        self.calls.append((system, user))
        yield self.reply

    def complete(self, system: str, user: str) -> str:
        return "".join(self.stream(system, user))


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    root = tmp_path / "journals"
    root.mkdir()
    (root / "2020-05-17.md").write_text(
        "---\ntitle: Lake day\ndate: 2020-05-17\n---\n\n"
        "Swam in the lake with Anna. Water was freezing but wonderful.",
        encoding="utf-8",
    )
    (root / "notes_2019-07.txt").write_text(
        "July notes. Started learning the guitar this month.", encoding="utf-8"
    )
    from docx import Document

    doc = Document()
    doc.add_paragraph("Trip to Lisbon. The tram screeched up the hill.")
    doc.save(root / "lisbon 2018-03-02.docx")
    return root


@pytest.fixture(autouse=True)
def _no_real_process_listing(monkeypatch):
    """Nothing in the suite may list or stop the machine's real processes.

    `varyz uninstall` stops every process the installation runs, and under
    the suite that installation is the developer's: their agents' live Varyz
    servers and daemon. Tests that exercise it hand `find` their own listing.
    """
    from varyz import background

    monkeypatch.setattr(background, "_listing", lambda: [])
    monkeypatch.setattr(background, "stop", lambda found: [])
