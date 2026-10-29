"""The self-updater, against a real git remote and clone in a temporary folder."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from varyz import update, update_worker
from varyz.config import Config
from varyz.domain import local_day, utc_now

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_javascript_only_change_requires_package_refresh(tmp_path, monkeypatch):
    source = tmp_path / "project" / "src" / "varyz"
    source.mkdir(parents=True)
    (source.parent.parent / "pyproject.toml").write_text("[project]\nname='varyzai'\n",
                                                       encoding="utf-8")
    installed = tmp_path / "installed"
    installed.mkdir()
    for folder in (source, installed):
        (folder / "app.js").write_text("first", encoding="utf-8")
    monkeypatch.setattr(update, "_installed_package", lambda: installed)
    assert not update._folder_differs(source.parent.parent)
    (source / "app.js").write_text("second", encoding="utf-8")
    assert update._folder_differs(source.parent.parent)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The developer's own git config (signing, hooks, default branch) stays out.
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.invalid")


class Repos:
    def __init__(self, tmp_path: Path) -> None:
        self.remote = tmp_path / "remote.git"
        self.upstream = tmp_path / "upstream"
        self.clone = tmp_path / "clone"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.remote))
        _git(tmp_path, "clone", "-q", str(self.remote), str(self.upstream))
        _git(self.upstream, "checkout", "-b", "main")
        self.publish({"pyproject.toml": "[project]\nname = 'x'\n", "src/varyz/a.py": "A = 1\n"})
        _git(tmp_path, "clone", "-q", str(self.remote), str(self.clone))

    def publish(self, files: dict[str, str], message: str = "change") -> str:
        for name, text in files.items():
            path = self.upstream / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-q", "-m", message)
        _git(self.upstream, "push", "-q", "origin", "main")
        return _git(self.upstream, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.clone, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


class Steps:
    def __init__(self, verify_error: str | None = None) -> None:
        self.synced: list[Path] = []
        self.verified: list[Path] = []
        self.verify_error = verify_error

    def sync(self, root: Path) -> str | None:
        self.synced.append(root)
        return None

    def verify(self, root: Path) -> str | None:
        self.verified.append(root)
        return self.verify_error


def _run(
    cfg: Config, repos: Repos, steps: Steps | None = None, **kwargs: Any
) -> update.UpdateResult:
    steps = steps or Steps()
    return update.run(cfg, root=repos.clone, sync=steps.sync, verify=steps.verify, **kwargs)


def test_default_mode_is_notify():
    assert Config().update.mode == "notify"


def test_off_mode_and_an_automatic_trigger_does_nothing(cfg, repos):
    cfg.update.mode = "off"
    before = repos.head()
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "off"
    assert repos.head() == before
    assert not update.is_due(cfg)


def test_an_up_to_date_checkout_says_so(cfg, repos):
    cfg.update.mode = "auto"
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "up_to_date"
    assert result.current == result.target


def test_notify_reports_an_update_and_leaves_the_checkout(cfg, repos):
    cfg.update.mode = "notify"
    before = repos.head()
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "available"
    assert result.commits == 1
    assert repos.head() == before


def test_auto_fast_forwards_and_checks_the_new_code(cfg, repos):
    cfg.update.mode = "auto"
    steps = Steps()
    target = repos.publish({"src/varyz/a.py": "A = 2\n"})
    result = _run(cfg, repos, steps, trigger="on-use")
    assert result.status == "applied", result.detail
    assert repos.head() == target
    assert steps.verified == [repos.clone]
    assert steps.synced == []  # no dependency change, no sync
    assert (repos.clone / "src/varyz/a.py").read_text(encoding="utf-8") == "A = 2\n"


def test_new_code_that_fails_its_check_is_rolled_back(cfg, repos):
    cfg.update.mode = "auto"
    before = repos.head()
    repos.publish({"src/varyz/a.py": "A = (\n"})
    result = _run(cfg, repos, Steps(verify_error="import check: SyntaxError"), trigger="on-use")
    assert result.status == "failed"
    assert "returned to" in result.detail
    assert repos.head() == before
    assert (repos.clone / "src/varyz/a.py").read_text(encoding="utf-8") == "A = 1\n"


def test_local_changes_are_never_overwritten(cfg, repos):
    cfg.update.mode = "auto"
    before = repos.head()
    (repos.clone / "src/varyz/a.py").write_text("A = 'mine'\n", encoding="utf-8")
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "skipped"
    assert "local changes" in result.detail
    assert repos.head() == before
    assert (repos.clone / "src/varyz/a.py").read_text(encoding="utf-8") == "A = 'mine'\n"


def test_local_commits_are_kept_and_a_diverged_branch_is_skipped(cfg, repos):
    cfg.update.mode = "auto"
    (repos.clone / "notes.txt").write_text("mine\n", encoding="utf-8")
    _git(repos.clone, "add", "notes.txt")
    _git(repos.clone, "commit", "-q", "-m", "local")
    ahead = repos.head()
    assert _run(cfg, repos, trigger="on-use").status == "up_to_date"

    repos.publish({"src/varyz/a.py": "A = 2\n"})
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "skipped"
    assert "diverged" in result.detail
    assert repos.head() == ahead


def test_a_branch_without_upstream_is_skipped(cfg, repos):
    cfg.update.mode = "auto"
    _git(repos.clone, "checkout", "-q", "-b", "experiment")
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "skipped"
    assert "tracks no remote branch" in result.detail


class Installed:
    """An installed Varyz: what uv would do, recorded instead of done."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, source: dict[str, Any] | None,
                 package: Path | None = None) -> None:
        self.reinstalled = 0
        self.refreshed: list[Path | None] = []
        self.reinstall_error: str | None = None
        monkeypatch.setattr(update, "checkout_root", lambda: None)
        monkeypatch.setattr(update, "install_source", lambda: source)
        monkeypatch.setattr(update, "_starts", lambda: None)
        monkeypatch.setattr(update, "_reinstall", self._reinstall)
        monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: False)
        monkeypatch.setattr(
            update, "_refresh_agents", lambda config, _python: self.refreshed.append(config)
        )
        if package is not None:
            monkeypatch.setattr(update, "_installed_package", lambda: package)

    def _reinstall(self) -> str | None:
        self.reinstalled += 1
        return self.reinstall_error


def _source_folder(root: Path, text: str) -> Path:
    (root / "src" / "varyz" / "db" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "src" / "varyz" / "a.py").write_text(text, encoding="utf-8")
    (root / "src" / "varyz" / "held_back.py").write_text("X = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "varyzai"\n[tool.uv.build-backend]\n'
        'source-exclude = ["src/varyz/held_back.py"]\n',
        encoding="utf-8",
    )
    return root


def _package(root: Path, text: str) -> Path:
    (root / "db" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text(text, encoding="utf-8")
    return root


def test_varyz_not_installed_with_uv_is_left_alone(cfg, monkeypatch):
    Installed(monkeypatch, None)
    result = update.run(cfg, apply=True)
    assert result.status == "skipped"
    assert "not installed with uv" in result.detail


def test_varyz_installed_from_github_is_reinstalled_when_github_moves_on(
    cfg, monkeypatch, tmp_path
):
    installed = Installed(monkeypatch, {"name": "varyzai", "git": update.REPOSITORY})
    monkeypatch.setattr(update, "_installed_commit", lambda: "a" * 40)
    monkeypatch.setattr(update, "_remote_commit", lambda url, ref: "a" * 40)
    assert update.run(cfg, apply=True).status == "up_to_date"
    assert installed.reinstalled == 0

    monkeypatch.setattr(update, "_remote_commit", lambda url, ref: "b" * 40)
    monkeypatch.setattr(update, "_backup_database", lambda cfg: (None, None))
    checked = update.run(cfg, apply=False)
    assert checked.status == "available" and "varyz update apply" in checked.detail
    assert installed.reinstalled == 0

    config = tmp_path / "config.toml"
    applied = update.run(cfg, apply=True, config_path=config)
    assert applied.status == "applied", applied.detail
    assert installed.reinstalled == 1
    assert installed.refreshed == [config]  # nobody runs varyz connect afterwards


def test_an_unreachable_github_is_a_failure_not_an_update(cfg, monkeypatch):
    installed = Installed(monkeypatch, {"name": "varyzai", "git": update.REPOSITORY})
    monkeypatch.setattr(update, "_installed_commit", lambda: "a" * 40)
    monkeypatch.setattr(update, "_remote_commit", lambda url, ref: None)
    result = update.run(cfg, apply=True)
    assert result.status == "failed"
    assert installed.reinstalled == 0


def test_varyz_installed_from_a_folder_follows_the_folder(cfg, monkeypatch, tmp_path):
    folder = _source_folder(tmp_path / "source", "A = 1\n")
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    # The module the build leaves out is not a difference.
    result = update.run(cfg, apply=True)
    assert result.status == "up_to_date", result.detail
    assert installed.reinstalled == 0

    (folder / "src" / "varyz" / "a.py").write_text("A = 2\n", encoding="utf-8")
    result = update.run(cfg, apply=True)
    assert result.status == "applied", result.detail
    assert installed.reinstalled == 1
    assert installed.refreshed


def test_a_failed_reinstall_is_reported_and_refreshes_nothing(cfg, monkeypatch, tmp_path):
    folder = _source_folder(tmp_path / "source", "A = 2\n")
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    installed.reinstall_error = "uv: the file is in use"
    result = update.run(cfg, apply=True)
    assert result.status == "failed"
    assert "the file is in use" in result.detail
    assert not installed.refreshed


def test_a_new_migration_in_the_folder_backs_the_database_up_first(
    cfg, monkeypatch, tmp_path
):
    folder = _source_folder(tmp_path / "source", "A = 1\n")
    (folder / "src" / "varyz" / "db" / "migrations" / "0099_new.sql").write_text(
        "SELECT 1;\n", encoding="utf-8"
    )
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    _database(cfg)
    result = update.run(cfg, apply=True)
    assert result.status == "applied", result.detail
    assert result.migrations and result.backup
    assert installed.reinstalled == 1


def test_an_automatic_update_never_installs_uncommitted_work(cfg, monkeypatch, repos, tmp_path):
    cfg.update.mode = "auto"
    folder = repos.clone
    _source_folder(folder, "A = 1\n")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "tree")
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    (folder / "src" / "varyz" / "a.py").write_text("A = 'half done'\n", encoding="utf-8")

    result = update.run(cfg, trigger="on-use")
    assert result.status == "skipped"
    assert "not committed" in result.detail
    assert installed.reinstalled == 0
    # Asked for, it installs what is in the folder.
    assert update.run(cfg, apply=True).status == "applied"
    assert installed.reinstalled == 1


def test_on_windows_an_automatic_reinstall_waits_for_the_sessions(cfg, monkeypatch, tmp_path):
    """Without a Python outside the environment to install from, nothing can."""
    cfg.update.mode = "auto"
    folder = _source_folder(tmp_path / "source", "A = 2\n")
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: True)
    monkeypatch.setattr(update_worker, "base_python", lambda _environment: None)
    result = update.run(cfg, trigger="on-use")
    assert result.status == "waiting"
    assert "varyz update apply" in result.detail
    assert installed.reinstalled == 0


def _config_file(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return config


def test_on_windows_the_install_runs_in_the_background_and_agents_stay_open(
    cfg, monkeypatch, tmp_path
):
    folder = _source_folder(tmp_path / "source", "A = 2\n")
    package = _package(tmp_path / "installed", "A = 1\n")
    installed = Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: True)
    monkeypatch.setattr(update, "_uv", lambda: "uv")
    python = tmp_path / "base" / "pythonw.exe"
    monkeypatch.setattr(update_worker, "base_python", lambda _environment: python)
    launched: list[list[str]] = []
    monkeypatch.setattr(update, "_launch", lambda command, _cwd: launched.append(command))
    result = update.run(cfg, apply=True, config_path=_config_file(tmp_path))
    assert result.status == "started", result.detail
    assert "Nothing needs restarting" in result.detail
    assert installed.reinstalled == 0 and installed.refreshed == []
    assert launched and launched[0][:3] == [str(python), "-I", "-B"]
    job = json.loads(Path(launched[0][4]).read_text(encoding="utf-8"))
    assert job["command"] == ["uv", "tool", "upgrade", "varyzai", "--reinstall"]
    assert update_worker.updating(cfg.data_dir)
    # A second apply meanwhile does not start another install.
    assert update.run(cfg, apply=True).status == "busy"


def test_finishing_a_background_install_records_it_and_releases_the_agents(
    cfg, monkeypatch, tmp_path
):
    config = _config_file(tmp_path)
    monkeypatch.setattr(update, "load_config", lambda _path: cfg)
    monkeypatch.setattr(update, "_starts", lambda: None)
    brought: list[bool] = []
    monkeypatch.setattr(update, "_bring_back", lambda _path, *, refresh: brought.append(refresh))
    job = {"id": "job", "config": str(config), "brings": "Varyz 0.2.0",
           "found": {"current": "0.1.5", "target": "0.2.0"}, "backup": None}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    update_worker.mark(cfg.data_dir, "job")
    assert update.main(["--finish", str(path)]) == 0
    state = update.load_state(cfg)
    assert state["status"] == "applied" and state["trigger"] == update_worker.TRIGGER
    assert not update_worker.updating(cfg.data_dir)
    assert update_worker.installed(cfg.data_dir) == state["checked_at"]
    assert brought == [True]
    notice = update.agent_notice(cfg)
    assert notice is not None and "0.2.0" in notice and "nothing needs restarting" in notice
    assert update.agent_notice(cfg) is None


def test_a_background_install_that_could_not_run_is_told_once(cfg, monkeypatch, tmp_path):
    config = _config_file(tmp_path)
    monkeypatch.setattr(update, "load_config", lambda _path: cfg)
    monkeypatch.setattr(update, "_bring_back", lambda _path, *, refresh: None)
    job = {"id": "job", "config": str(config), "brings": "Varyz 0.2.0", "found": {},
           "outcome": {"status": "waiting", "detail": update_worker.OLD_SESSIONS}}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    assert update.finish(path) == 0
    assert update.load_state(cfg)["status"] == "waiting"
    assert update_worker.installed(cfg.data_dir) == ""  # nothing new: no agent restarts
    notice = update.agent_notice(cfg)
    assert notice is not None and "Close them" in notice
    assert update.agent_notice(cfg) is None


def test_elsewhere_the_agents_pause_while_varyz_reinstalls_and_then_restart_onto_it(
    cfg, monkeypatch, tmp_path
):
    folder = _source_folder(tmp_path / "source", "A = 2\n")
    package = _package(tmp_path / "installed", "A = 1\n")
    Installed(monkeypatch, {"name": "varyzai", "directory": str(folder)}, package)
    during: list[bool] = []
    monkeypatch.setattr(update, "_reinstall",
                        lambda: during.append(update_worker.updating(cfg.data_dir)))
    before = update_worker.installed(cfg.data_dir)
    result = update.run(cfg, apply=True)
    assert result.status == "applied" and "nothing needs restarting" in result.detail
    assert during == [True]
    assert not update_worker.updating(cfg.data_dir)
    assert update_worker.installed(cfg.data_dir) != before


def _database(cfg: Config) -> None:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("CREATE TABLE kept (value TEXT)")
        conn.execute("INSERT INTO kept VALUES ('before the migration')")
    conn.close()


def test_a_migration_is_applied_only_after_the_database_is_backed_up(cfg, repos):
    cfg.update.mode = "auto"
    _database(cfg)
    seen: list[bool] = []

    def verify(root: Path) -> str | None:
        seen.append(update.backup_path(cfg).is_file())
        return None

    target = repos.publish({"src/varyz/db/migrations/0099_new.sql": "SELECT 1;\n"})
    result = update.run(cfg, trigger="on-use", root=repos.clone, sync=Steps().sync, verify=verify)
    assert result.status == "applied", result.detail
    assert result.migrations
    assert seen == [True]
    assert repos.head() == target
    with sqlite3.connect(update.backup_path(cfg)) as conn:
        rows = conn.execute("SELECT value FROM kept").fetchall()
    conn.close()
    assert rows == [("before the migration",)]
    assert result.backup == str(update.backup_path(cfg))


def test_a_migration_waits_when_the_backup_does_not_fit(cfg, repos, monkeypatch):
    cfg.update.mode = "auto"
    _database(cfg)
    before = repos.head()
    monkeypatch.setattr(update, "free_bytes", lambda path: 0)
    repos.publish({"src/varyz/db/migrations/0099_new.sql": "SELECT 1;\n"})
    result = _run(cfg, repos, trigger="on-use")
    assert result.status == "skipped"
    assert "backup failed" in result.detail
    assert repos.head() == before
    assert not update.backup_path(cfg).exists()


def test_new_dependencies_are_synced(cfg, repos, monkeypatch):
    cfg.update.mode = "auto"
    monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: False)
    steps = Steps()
    repos.publish({"uv.lock": "version = 1\n"})
    result = _run(cfg, repos, steps, trigger="on-use")
    assert result.status == "applied", result.detail
    assert result.dependencies
    assert steps.synced == [repos.clone]


def test_on_windows_new_dependencies_wait_for_a_manual_apply(cfg, repos, monkeypatch):
    cfg.update.mode = "auto"
    monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: True)
    before = repos.head()
    repos.publish({"uv.lock": "version = 1\n"})
    waiting = _run(cfg, repos, trigger="on-use")
    assert waiting.status == "waiting"
    assert "varyz update apply" in waiting.detail
    assert repos.head() == before

    steps = Steps()
    applied = _run(cfg, repos, steps, apply=True)  # manual
    assert applied.status == "applied", applied.detail
    assert steps.synced == [repos.clone]


def test_either_kill_switch_stops_updates(cfg, repos):
    cfg.update.mode = "auto"
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    before = repos.head()
    update.pause(cfg)
    assert _run(cfg, repos, trigger="on-use").status == "paused"
    assert _run(cfg, repos, apply=True).status == "paused"
    assert not update.is_due(cfg)
    update.resume(cfg)
    (cfg.data_dir / "autosync" / "PAUSED").write_text("", encoding="utf-8")
    assert _run(cfg, repos, trigger="on-use").status == "paused"
    assert repos.head() == before


def test_a_dry_run_fetches_changes_and_records_nothing(cfg, repos):
    cfg.update.mode = "auto"
    before = repos.head()
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    # Nothing fetched yet, so the dry run sees what the last fetch saw.
    assert _run(cfg, repos, apply=True, dry_run=True).status == "up_to_date"
    _git(repos.clone, "fetch", "-q")
    result = _run(cfg, repos, apply=True, dry_run=True)
    assert result.status == "dry_run"
    assert result.detail.startswith("Would apply: 1 new commit")
    assert repos.head() == before
    assert not update.state_path(cfg).exists()
    assert not update.log_path(cfg).exists()


def test_every_attempt_is_appended_to_the_log(cfg, repos):
    cfg.update.mode = "auto"
    _run(cfg, repos, trigger="on-use")
    repos.publish({"src/varyz/a.py": "A = 2\n"})
    _run(cfg, repos, apply=False)
    lines = update.log_path(cfg).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["up_to_date", "available"]
    assert update.load_state(cfg)["status"] == "available"


def test_the_check_runs_once_a_day_on_first_use(cfg, monkeypatch):
    cfg.update.mode = "auto"
    launched: list[list[str]] = []
    monkeypatch.setattr(update, "_launch", lambda command, _cwd: launched.append(command))
    config_path = cfg.base_dir / "config.toml"
    config_path.write_text("", encoding="utf-8")

    assert update.is_due(cfg)
    assert update.maybe_check_on_use(cfg, config_path) == "started"
    assert update.maybe_check_on_use(cfg, config_path) == "not_due"
    assert len(launched) == 1
    assert launched[0][1:3] == ["-m", "varyz.update"]

    yesterday = local_day(utc_now() - timedelta(days=1))
    update._write_state(cfg, {"attempted_on": yesterday})
    assert update.is_due(cfg)


def test_doctor_reports_the_update_mode(cfg):
    cfg.update.mode = "off"
    ok, detail = update.doctor_detail(cfg)
    assert ok and detail.startswith("off")
    cfg.update.mode = "auto"
    update._write_state(cfg, {"status": "failed", "checked_at": "t", "detail": "broke"})
    ok, detail = update.doctor_detail(cfg)
    assert not ok and "broke" in detail


def test_an_update_a_notify_check_found_reaches_the_owner_once(cfg):
    found = {
        "status": "available",
        "detail": "2 commits on origin/main: varyz update apply",
        "trigger": "on-use",
        "target": "abc123",
    }
    update._write_state(cfg, found)
    notice = update.agent_notice(cfg)
    assert notice is not None
    assert "2 commits on origin/main" in notice
    assert "varyz update apply" in notice
    assert update.agent_notice(cfg) is None  # told once per version
    update._write_state(cfg, {**found, "target": "def456"})
    assert update.agent_notice(cfg) is not None


def test_no_update_notice_when_nothing_is_new_or_the_owner_checked_by_hand(cfg):
    assert update.agent_notice(cfg) is None
    update._write_state(cfg, {"status": "up_to_date", "trigger": "on-use", "target": "a"})
    assert update.agent_notice(cfg) is None
    update._write_state(cfg, {"status": "available", "trigger": "manual", "target": "a"})
    assert update.agent_notice(cfg) is None
    update._write_state(cfg, {"status": "available", "trigger": "on-use", "target": "a"})
    update.pause(cfg)
    assert update.agent_notice(cfg) is None


RELEASE_SOURCES = [{"name": "varyzai", "path": "release.whl"},
                   {"name": "varyzai", "url": "file:///release.whl"}]
INSTALLER = b"installer bytes"
DOWNLOADS = "https://github.com/egrm07/varyz/releases/download/v0.2.0/"


class Release:
    """GitHub's latest release, the release's checksums and its installer download."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, tag: str = "v0.2.0",
                 digest: str | None = None) -> None:
        self.name = "VaryzSetup-0.2.0-windows-x64.exe"
        self.digest = digest or hashlib.sha256(INSTALLER).hexdigest()
        self.tag = tag
        self.gets: list[str] = []
        self.downloads: list[str] = []
        self.opened: list[Path] = []
        monkeypatch.setattr(update, "installed_version", lambda: "0.1.5")
        monkeypatch.setattr(update, "installer_name",
                            lambda version: f"VaryzSetup-{version}-windows-x64.exe")
        monkeypatch.setattr(update, "_http_get", self.get)
        monkeypatch.setattr(update, "_download", self.download)
        monkeypatch.setattr(update, "_open", self.opened.append)
        # Opened as on a Mac; on Windows the background worker runs it silently.
        monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: False)
        monkeypatch.setattr(update, "_reinstall",
                            lambda: pytest.fail("must not contact a package index"))

    def get(self, url: str) -> bytes:
        self.gets.append(url)
        if url == update.LATEST_RELEASE:
            assets = [{"name": name, "browser_download_url": DOWNLOADS + name}
                      for name in (self.name, update.CHECKSUMS)]
            return json.dumps({"tag_name": self.tag, "assets": assets}).encode()
        assert url == DOWNLOADS + update.CHECKSUMS
        return f"{'0' * 64}  other.whl\n{self.digest}  {self.name}\n".encode()

    def download(self, url: str, target: Path, expected: str) -> None:
        self.downloads.append(url)
        if hashlib.sha256(INSTALLER).hexdigest() != expected:
            raise ValueError("its checksum does not match the release's")
        target.write_bytes(INSTALLER)


def _release_run(cfg: Config, monkeypatch: pytest.MonkeyPatch, *, trigger: str = "manual",
                 apply: bool | None = None, source: dict | None = None) -> update.UpdateResult:
    monkeypatch.setattr(update, "install_source", lambda: source or RELEASE_SOURCES[0])
    monkeypatch.setattr(update, "checkout_root", lambda: None)
    return update.run(cfg, trigger=trigger, apply=apply)


@pytest.mark.parametrize("source", RELEASE_SOURCES)
def test_a_release_install_hears_of_a_new_release(cfg, monkeypatch, source):
    release = Release(monkeypatch)
    result = _release_run(cfg, monkeypatch, trigger="on-use", source=source)
    assert result.status == "available"
    assert (result.current, result.target) == ("0.1.5", "0.2.0")
    assert "Varyz 0.2.0 is out" in result.detail and "varyz update apply" in result.detail
    assert release.downloads == [] and release.opened == []
    notice = update.agent_notice(cfg)
    assert notice is not None and "0.2.0" in notice
    assert update.agent_notice(cfg) is None


def test_auto_mode_never_downloads_an_installer_by_itself(cfg, monkeypatch):
    cfg.update.mode = "auto"
    release = Release(monkeypatch)
    result = _release_run(cfg, monkeypatch, trigger="on-use")
    assert result.status == "available"
    assert release.downloads == [] and release.opened == []


def test_a_release_install_on_the_latest_release_is_up_to_date(cfg, monkeypatch):
    Release(monkeypatch, tag="v0.1.5")
    assert _release_run(cfg, monkeypatch, apply=False).status == "up_to_date"
    Release(monkeypatch, tag="v0.1.4")
    assert _release_run(cfg, monkeypatch, apply=False).status == "up_to_date"


def test_a_prerelease_tag_is_not_offered(cfg, monkeypatch):
    Release(monkeypatch, tag="v0.2.0-rc1")
    assert _release_run(cfg, monkeypatch, apply=True).status == "failed"


def test_apply_downloads_the_checked_installer_and_opens_it(cfg, monkeypatch):
    release = Release(monkeypatch)
    old = update.installers_path(cfg) / "VaryzSetup-0.1.5-windows-x64.exe"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    result = _release_run(cfg, monkeypatch, apply=True)
    target = update.installers_path(cfg) / release.name
    assert result.status == "opened", result.detail
    assert "Close your AI agents" in result.detail
    assert release.downloads == [DOWNLOADS + release.name]
    assert release.opened == [target]
    assert target.read_bytes() == INSTALLER
    assert not old.exists()
    # A second apply opens the installer already here instead of downloading again.
    _release_run(cfg, monkeypatch, apply=True)
    assert len(release.downloads) == 1 and release.opened == [target, target]


def test_on_windows_the_checked_installer_runs_silently_in_the_background(
    cfg, monkeypatch, tmp_path
):
    release = Release(monkeypatch)
    monkeypatch.setattr(update, "_libraries_locked_while_loaded", lambda: True)
    monkeypatch.setattr(update_worker, "base_python",
                        lambda _environment: tmp_path / "pythonw.exe")
    launched: list[list[str]] = []
    monkeypatch.setattr(update, "_launch", lambda command, _cwd: launched.append(command))
    monkeypatch.setattr(update, "install_source", lambda: RELEASE_SOURCES[0])
    monkeypatch.setattr(update, "checkout_root", lambda: None)
    result = update.run(cfg, apply=True, config_path=_config_file(tmp_path))
    assert result.status == "started", result.detail
    assert release.opened == []
    job = json.loads(Path(launched[0][4]).read_text(encoding="utf-8"))
    target = update.installers_path(cfg) / release.name
    assert job["command"][:3] == [str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES"]


def test_an_installer_that_fails_its_checksum_is_never_opened(cfg, monkeypatch):
    release = Release(monkeypatch, digest="f" * 64)
    result = _release_run(cfg, monkeypatch, apply=True)
    assert result.status == "failed" and "checksum" in result.detail
    assert release.opened == []
    assert not (update.installers_path(cfg) / release.name).exists()


def test_an_installer_outside_the_release_downloads_is_refused(cfg, monkeypatch):
    release = Release(monkeypatch)

    def elsewhere(url: str) -> bytes:
        data = json.loads(release.get(url))
        data["assets"][0]["browser_download_url"] = "https://example.com/VaryzSetup.exe"
        return json.dumps(data).encode()

    monkeypatch.setattr(update, "_http_get", elsewhere)
    result = _release_run(cfg, monkeypatch, apply=True)
    assert result.status == "failed"
    assert release.downloads == [] and release.opened == []


def test_an_unreachable_github_is_a_failure_for_a_release_install(cfg, monkeypatch):
    Release(monkeypatch)

    def offline(_url: str) -> bytes:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(update, "_http_get", offline)
    result = _release_run(cfg, monkeypatch, trigger="on-use")
    assert result.status == "failed" and "GitHub" in result.detail
    assert update.agent_notice(cfg) is None


def test_a_computer_without_an_installer_gets_the_releases_page(cfg, monkeypatch):
    release = Release(monkeypatch)
    monkeypatch.setattr(update, "installer_name", lambda _version: None)
    result = _release_run(cfg, monkeypatch, apply=True)
    assert result.status == "available" and update.RELEASES_PAGE in result.detail
    assert release.downloads == []


@pytest.mark.parametrize(("system", "machine", "name"), [
    ("win32", "AMD64", "VaryzSetup-0.2.0-windows-x64.exe"),
    ("darwin", "arm64", "VaryzSetup-0.2.0-macos-arm64.pkg"),
    ("darwin", "x86_64", "VaryzSetup-0.2.0-macos-x86_64.pkg"),
    ("linux", "x86_64", None),
])
def test_installer_names_match_the_release_assets(monkeypatch, system, machine, name):
    monkeypatch.setattr(update.sys, "platform", system)
    monkeypatch.setattr(update.platform, "machine", lambda: machine)
    assert update.installer_name("0.2.0") == name


def test_release_checksums_are_read_like_sha256sum():
    text = f"{'a' * 64}  VaryzSetup-0.2.0-windows-x64.exe\n{'b' * 64} *x.pkg\nbad line\n"
    assert update._checksums(text) == {"VaryzSetup-0.2.0-windows-x64.exe": "a" * 64}
