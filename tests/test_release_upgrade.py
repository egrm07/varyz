"""Real isolated uv installs: migration, failed imports and interrupted commits."""

import shutil
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from varyz import release_upgrade as release


def wheel(folder, version, *, broken=False, migration_failure=False, migrations=()):
    path = folder / f"varyzai-{version}-py3-none-any.whl"
    dist = f"varyzai-{version}.dist-info"
    files = {
        "varyz/__init__.py": "",
        "varyz/cli.py": ("raise RuntimeError('bad release')\n" if broken else "")
        + f"def main():\n print('{version}')\n",
        "varyz/mcp_server.py": "",
        "varyz/autosync_daemon.py": "",
        "varyz/config.py": (
            "import tomllib\ndef load_config(p):\n return tomllib.loads(p.read_text())\n"
        ),
        "varyz/db/__init__.py": "",
        "varyz/db/connection.py": (
            "def migrate(c):\n c.execute('PRAGMA user_version=2'); c.commit()\n"
            + (" raise RuntimeError('failed migration')\n" if migration_failure else "")
        ),
        "sqlite_vec.py": "def load(c):\n pass\n",
        dist + "/METADATA": f"Metadata-Version: 2.1\nName: varyzai\nVersion: {version}\n",
        dist + "/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        dist + "/entry_points.txt": "[console_scripts]\nvaryz = varyz.cli:main\n",
    }
    for migration in migrations:
        files[f"varyz/db/migrations/{migration:04d}_fixture.sql"] = "-- Applied fixture\n"
    files[dist + "/RECORD"] = "".join(f"{name},,\n" for name in files) + dist + "/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(zipfile.ZipInfo(name), data)
    return path


@pytest.fixture
def installed(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for isolated release tests")
    tools = tmp_path / "tools"
    binaries = tmp_path / "bin"
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(binaries))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    # The sandbox denies Windows process inventory. Exercise that adapter below;
    # actual uv installation, environment restoration and SQLite work remain real.
    monkeypatch.setattr(release, "running", lambda *_: False)
    old = wheel(tmp_path, "0.0.1")
    release.execute(
        [uv, "tool", "install", "--offline", "--no-config", "--python", sys.executable, str(old)]
    )
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir="archive"\n')
    data = tmp_path / "archive"
    data.mkdir()
    with closing(sqlite3.connect(data / "varyz.db")) as db, db:
        db.execute("CREATE TABLE records(text)")
        db.execute("INSERT INTO records VALUES('keep this')")
        db.execute("PRAGMA user_version=1")
    return uv, config, tools, binaries, tmp_path


def apply(installed, **kwargs):
    uv, config, tools, binaries, root = installed
    target = wheel(root, "0.0.2", broken=kwargs.pop("broken", False))
    return release.upgrade(uv, target, config, release.digest(target), **kwargs)


def version(installed):
    uv, config, tools, binaries, root = installed
    command = binaries / ("varyz.exe" if sys.platform == "win32" else "varyz")
    return release.execute([str(command)])


def archive(installed):
    with closing(sqlite3.connect(installed[1].parent / "archive/varyz.db")) as db, db:
        return db.execute("PRAGMA user_version").fetchone()[0], db.execute(
            "SELECT text FROM records"
        ).fetchone()[0]


def test_real_uv_upgrade_commits_verified_archive_and_keeps_settings(installed):
    config = installed[1]
    before = config.read_bytes()
    result = apply(installed)
    assert result["status"] == "applied"
    assert version(installed) == "0.0.2"
    assert archive(installed) == (2, "keep this")
    assert config.read_bytes() == before
    folder = Path(result["backup"])
    assert (folder / "environment/uv-receipt.toml").is_file()
    # One pre-upgrade copy is kept for rollback, not two.
    assert not (folder / "archive.db").exists()
    assert (folder / "original.db").is_file()
    with closing(sqlite3.connect(folder / "original.db")) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_new_import_restores_real_uv_environment_and_archive(installed):
    with pytest.raises(ValueError, match="previous installation and archive restored"):
        apply(installed, broken=True)
    assert version(installed) == "0.0.1"
    assert archive(installed) == (1, "keep this")


def test_failed_migration_changes_only_copy_and_rolls_back(installed):
    target = wheel(installed[-1], "0.0.2", migration_failure=True)
    with pytest.raises(ValueError, match="previous installation and archive restored"):
        release.upgrade(installed[0], target, installed[1], release.digest(target))
    assert version(installed) == "0.0.1"
    assert archive(installed) == (1, "keep this")


def test_downgrade_is_refused_before_backup(installed):
    target = wheel(installed[-1], "0.0.0")
    with pytest.raises(ValueError, match="older release"):
        release.upgrade(installed[0], target, installed[1], release.digest(target))
    assert version(installed) == "0.0.1"
    assert not (installed[1].parent / "archive/autosync").exists()


def test_same_release_is_not_reinstalled(installed):
    apply(installed)
    result = apply(installed)
    assert result["status"] == "up_to_date"
    assert len(list((installed[1].parent / "archive/autosync/releases").iterdir())) == 1


def test_schema_downgrade_refuses_before_install(installed):
    with closing(sqlite3.connect(installed[1].parent / "archive/varyz.db")) as db, db:
        db.execute("CREATE TABLE schema_migrations(version)")
        db.execute("INSERT INTO schema_migrations VALUES(9999)")
    with pytest.raises(ValueError, match="migrations are older"):
        apply(installed)
    assert version(installed) == "0.0.1"


def test_recovery_will_not_discard_writes_since_interruption(installed, monkeypatch):
    original = release.write_state

    def stop(folder, state):
        if state["phase"] == "applied":
            raise SystemExit("simulated interruption")
        original(folder, state)

    monkeypatch.setattr(release, "write_state", stop)
    with pytest.raises(SystemExit):
        apply(installed)
    monkeypatch.setattr(release, "write_state", original)
    with closing(sqlite3.connect(installed[1].parent / "archive/varyz.db")) as db, db:
        db.execute("INSERT INTO records VALUES('written later')")
    folder = next((installed[1].parent / "archive/autosync/releases").iterdir())
    with pytest.raises(ValueError, match="Archive changed"):
        release.upgrade(installed[0], Path("unused"), installed[1], "", recovery=folder)
    assert version(installed) == "0.0.2"
    with closing(sqlite3.connect(installed[1].parent / "archive/varyz.db")) as db, db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 2


def test_dry_run_changes_no_environment_or_archive(installed):
    result = apply(installed, dry_run=True)
    assert result["status"] == "dry_run"
    assert version(installed) == "0.0.1"
    assert archive(installed) == (1, "keep this")
    assert not (installed[1].parent / "archive/autosync").exists()


def test_default_tool_python_is_preserved(installed):
    path = installed[2] / "varyzai/uv-receipt.toml"
    path.write_text(
        "\n".join(line for line in path.read_text().splitlines() if not line.startswith("python ="))
    )
    assert apply(installed)["status"] == "applied"
    assert version(installed) == "0.0.2"


def test_recovery_after_interruption_between_database_renames(installed, monkeypatch):
    original = release.rename

    def stop(source, target):
        original(source, target)
        if target.name == "original.db":
            raise SystemExit("simulated interruption")

    monkeypatch.setattr(release, "rename", stop)
    with pytest.raises(SystemExit):
        apply(installed)
    assert not (installed[1].parent / "archive/varyz.db").exists()
    monkeypatch.setattr(release, "rename", original)
    folder = next((installed[1].parent / "archive/autosync/releases").iterdir())
    assert (
        release.upgrade(installed[0], Path("unused"), installed[1], "", recovery=folder)["status"]
        == "rolled_back"
    )
    assert archive(installed) == (1, "keep this")
    assert version(installed) == "0.0.1"


@pytest.mark.parametrize("phase", ["installing", "committing"])
def test_hard_interruption_can_recover_with_missing_current_receipt(installed, monkeypatch, phase):
    original_execute = release.execute
    original_state = release.write_state

    def execute(args):
        result = original_execute(args)
        if phase == "installing" and args[1:3] == ["tool", "install"]:
            raise SystemExit("simulated power loss")
        return result

    def write_state(folder, state):
        if phase == "committing" and state["phase"] == "applied":
            raise SystemExit("simulated power loss")
        original_state(folder, state)

    monkeypatch.setattr(release, "execute", execute)
    monkeypatch.setattr(release, "write_state", write_state)
    with pytest.raises(SystemExit):
        apply(installed)
    monkeypatch.setattr(release, "execute", original_execute)
    monkeypatch.setattr(release, "write_state", original_state)
    folder = next((installed[1].parent / "archive/autosync/releases").iterdir())
    (installed[2] / "varyzai/uv-receipt.toml").unlink()
    result = release.upgrade(installed[0], Path("unused"), installed[1], "", recovery=folder)
    assert result["status"] == "rolled_back"
    assert version(installed) == "0.0.1"
    assert archive(installed) == (1, "keep this")


def test_running_agent_refuses_before_install_and_backups(installed, monkeypatch):
    monkeypatch.setattr(release, "running", lambda *_: True)
    with pytest.raises(ValueError, match="Close Varyz agents"):
        apply(installed)
    assert version(installed) == "0.0.1"
    assert not (installed[1].parent / "archive/autosync/releases").exists()


def test_wrong_checksum_refuses_without_changes(installed):
    uv, config, tools, binaries, root = installed
    target = wheel(root, "0.0.2")
    with pytest.raises(ValueError, match="checksum"):
        release.upgrade(uv, target, config, "0" * 64)
    assert version(installed) == "0.0.1"


def ledger(installed, *, user_version=2):
    path = installed[1].parent / "archive/varyz.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_migrations(version)")
    conn.execute("INSERT INTO schema_migrations VALUES(1)")
    conn.execute(f"PRAGMA user_version={user_version}")
    conn.commit()
    return conn


@pytest.mark.parametrize("broken", [False, True])
def test_program_only_upgrade_preserves_open_archive_and_concurrent_writes(
    installed, monkeypatch, broken
):
    target = wheel(installed[-1], "0.0.2", migrations=(1,), broken=broken)
    original = release.execute
    with closing(ledger(installed)) as reader:

        def execute(args, **kw):
            result = original(args, **kw)
            if args[1:3] == ["tool", "install"]:
                reader.execute("INSERT INTO records VALUES('written during update')")
                reader.commit()
            return result

        monkeypatch.setattr(release, "execute", execute)
        if broken:
            with pytest.raises(ValueError, match="previous installation and archive restored"):
                release.upgrade(
                    installed[0], target, installed[1], release.digest(target), package_only=True
                )
            assert version(installed) == "0.0.1"
        else:
            result = release.upgrade(
                installed[0], target, installed[1], release.digest(target), package_only=True
            )
            assert result["archive_replaced"] is False and version(installed) == "0.0.2"
            assert not (Path(result["backup"]) / "original.db").exists()
        assert reader.execute("SELECT text FROM records ORDER BY rowid").fetchall() == [
            ("keep this",),
            ("written during update",),
        ]


def test_program_only_requires_all_release_migrations_before_install(installed):
    target = wheel(installed[-1], "0.0.2", migrations=(1, 2))
    with (
        closing(ledger(installed)),
        pytest.raises(ValueError, match="every release migration already applied"),
    ):
        release.upgrade(
            installed[0], target, installed[1], release.digest(target), package_only=True
        )
    assert version(installed) == "0.0.1"
    assert not (installed[1].parent / "archive/autosync/releases").exists()


def test_program_only_rejects_schema_change_in_verified_copy(installed):
    target = wheel(installed[-1], "0.0.2", migrations=(1,))
    with closing(ledger(installed, user_version=1)) as reader:
        with pytest.raises(ValueError, match="previous installation and archive restored"):
            release.upgrade(
                installed[0], target, installed[1], release.digest(target), package_only=True
            )
        assert reader.execute("PRAGMA user_version").fetchone()[0] == 1
    assert version(installed) == "0.0.1"


def test_requirement_receipt_preserves_directml_and_markers(tmp_path):
    receipt = {
        "tool": {
            "requirements": [
                {"name": "varyzai", "directory": "old", "extras": ["cloud"]},
                {"name": "onnxruntime-directml", "specifier": ">=1"},
            ],
            "overrides": [{"name": "onnxruntime", "marker": "sys_platform != 'win32'"}],
        }
    }
    args = release.install_arguments(receipt, tmp_path / "release.whl", tmp_path)
    assert args[0].startswith("varyzai[cloud] @ file:")
    assert "onnxruntime-directml>=1" in args
    assert "sys_platform != 'win32'" in (tmp_path / "overrides.txt").read_text()


def test_unknown_receipt_options_are_not_silently_dropped(tmp_path):
    receipt = {"tool": {"requirements": [{"name": "varyzai"}], "options": {"custom": True}}}
    with pytest.raises(ValueError, match="Custom uv tool options"):
        release.install_arguments(receipt, tmp_path / "release.whl", tmp_path)


def test_process_inventory_failure_abstains(monkeypatch, tmp_path):
    def fail(_, **kwargs):
        raise ValueError("process inventory denied")

    monkeypatch.setattr(release, "execute", fail)
    with pytest.raises(ValueError, match="denied"):
        release.running(tmp_path / "tool", [])


def test_upgrade_moves_the_tool_to_the_python_the_release_ships(tmp_path):
    """Not the interpreter recorded in the receipt at first install (0.1.0's)."""
    receipt = {"tool": {"requirements": [{"name": "varyzai"}], "python": "/old/0.1.0/python3"}}
    args = release.install_arguments(receipt, tmp_path / "w.whl", tmp_path)
    assert args[args.index("--python") + 1] == "/old/0.1.0/python3"
    shipped = tmp_path / "0.1.5/runtime/bin/python3"
    shipped.parent.mkdir(parents=True)
    shipped.write_bytes(b"")
    moved = release.use_python(args, shipped)
    assert moved.count("--python") == 1 and moved[-1] == str(shipped)
    with pytest.raises(ValueError, match="nothing was replaced"):
        release.use_python(args, tmp_path / "missing")


def test_a_finished_upgrade_removes_older_backups_but_never_an_interrupted_one(tmp_path):
    """The test Mac held 271 MB of backups from five upgrades."""
    import json

    parent = tmp_path / "autosync/releases"

    def backup(name, phase, *files):
        folder = parent / name
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"phase": phase}), encoding="utf-8")
        for file in files:
            (folder / file).write_bytes(b"x")
        return folder

    old = backup("a", "applied", "original.db", "archive.db")
    undone = backup("b", "rolled_back", "archive.db")
    interrupted = backup("c", "committing", "original.db")
    newest = backup("d", "applied", "original.db", "archive.db", "varyzai-0.1.5.whl")
    assert release.prune_backups(parent, newest) == 2
    assert not old.exists() and not undone.exists() and interrupted.is_dir()
    assert sorted(p.name for p in newest.iterdir()) == [
        "original.db", "state.json", "varyzai-0.1.5.whl"]
