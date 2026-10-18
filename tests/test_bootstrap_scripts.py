import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import typer

from varyz import cli

ROOT = Path(__file__).resolve().parents[1]
PS_INSTALLER = ROOT / "scripts" / "install.ps1"
SH_INSTALLER = ROOT / "scripts" / "install.sh"
MAC_LAUNCHER = ROOT / "scripts" / "Install Varyz.command"
WINDOWS_LAUNCHER = ROOT / "scripts" / "Install Varyz.cmd"
PACKAGE_NAME = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["name"]

# `varyz setup`, `& $varyz setup`, `"$varyz" setup` and a `varyz setup` quoted
# inside a message are all the same promise to the reader, so the shell's ways
# of spelling the launcher are flattened before anything is matched.
_LAUNCHER = re.compile(r'(?:"\$varyz"|&\s+\$varyz|\$varyz|\bvaryz)')
# The command is whatever words come before the first flag, which is how the
# two-word groups (`local-model install`) stay one name.
_INVOCATION = re.compile(r"varyz ((?:[a-z][a-z-]* )+?)(--[a-z-]+(?:[^\n]*))")


def _commands_named(script: str) -> list[tuple[str, list[str]]]:
    """Every varyz command the script runs or tells the reader to run."""
    flat = _LAUNCHER.sub("varyz", script)
    named = []
    for words, tail in _INVOCATION.findall(flat):
        # A flag that ends a quoted message carries the closing quote.
        flags = [
            re.match(r"--[a-z-]+", token).group(0)  # type: ignore[union-attr]
            for token in tail.split()
            if token.startswith("--")
        ]
        named.append((words.strip(), flags))
    return named


def _resolve(name: str):
    """The live command that name refers to, or a failure saying it is gone."""
    command = typer.main.get_command(cli.app)
    for word in name.split():
        assert word in command.commands, f"`varyz {name}` is not a command"
        command = command.commands[word]
    return command


def test_installers_only_name_commands_and_flags_that_exist():
    # An installer is the one script nobody runs from a checkout, so a command
    # renamed underneath it fails for a stranger on their first minute and for
    # nobody else. Resolve every name against the CLI instead of matching words.
    for script in (PS_INSTALLER, SH_INSTALLER):
        named = _commands_named(script.read_text(encoding="utf-8"))
        assert named, f"{script.name} runs no varyz command at all"
        for name, flags in named:
            command = _resolve(name)
            options = {opt for param in command.params for opt in param.opts}
            options |= {opt for param in command.params for opt in param.secondary_opts}
            for flag in flags:
                assert flag in options, f"`varyz {name}` has no {flag}"


def test_installers_hand_over_the_whole_first_run():
    powershell = PS_INSTALLER.read_text(encoding="utf-8")
    shell = SH_INSTALLER.read_text(encoding="utf-8")

    for script in (powershell, shell):
        assert "tool install" in script
        # `setup` is the first run: archive, daemon, agents and
        # data sources. `init` + `install` stopped at the archive and the MCP
        # entry, which left somebody who used the published one-liner with no
        # daemon, no rules, no connected source and no word on any of it.
        commands = {name for name, _ in _commands_named(script)}
        assert "setup" in commands
        assert not commands & {"bootstrap", "init", "install"}
        assert "--yes" in script
    assert "DryRun" in powershell
    assert "--dry-run" in shell
    assert "astral.sh/uv/install.ps1" in powershell
    assert "astral.sh/uv/install.sh" in shell
    assert "</dev/tty" in shell  # curl | sh still has a safe confirmation channel


def test_installers_update_an_installed_varyz_and_tell_a_pause_from_a_failure():
    # Run again over an installed Varyz, `uv tool install` only said so; an
    # update backs the database up and refreshes the agents. A setup paused in
    # a download (exit 130) has said how to carry on and is no failure.
    for script in (PS_INSTALLER, SH_INSTALLER):
        text = script.read_text(encoding="utf-8")
        assert ("update apply", ["--config"]) in _commands_named(text), script.name
        assert "130" in text, script.name
    assert cli.SETUP_PAUSED == 130


REPOSITORY = "git+https://github.com/egrm07/varyz"


def test_installers_install_this_folder_or_else_github():
    # Varyz is not on a package index: the installers take the source folder
    # they sit in, and GitHub when run from anywhere else. The folder is known
    # by the distribution's name in its pyproject, not by any folder name.
    powershell = PS_INSTALLER.read_text(encoding="utf-8")
    shell = SH_INSTALLER.read_text(encoding="utf-8")
    for script in (powershell, shell):
        assert REPOSITORY in script
        assert f'name = "{PACKAGE_NAME}"' in script
        assert "needs git" in script
    assert "$env:VARYZ_LAUNCHER" in powershell  # the .cmd copy has no $PSScriptRoot


def _shell_dry_run(script: Path, tmp_path: Path, **env: str) -> str:
    executable = shutil.which("sh")
    if executable is None:
        pytest.skip("a POSIX shell is not installed")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {**os.environ, "HOME": str(home)}
    environment.pop("VARYZ_PACKAGE_SOURCE", None)
    environment.update(env)
    result = subprocess.run(
        [executable, str(script), "--dry-run", "--yes"],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_posix_installer_in_a_checkout_installs_the_checkout(tmp_path):
    out = _shell_dry_run(SH_INSTALLER, tmp_path)
    assert f"package: {ROOT}" in out
    assert f"Would run: uv tool install {ROOT}" in out


def test_posix_installer_elsewhere_installs_from_github(tmp_path):
    lone = tmp_path / "Downloads" / "install.sh"
    lone.parent.mkdir()
    shutil.copy(SH_INSTALLER, lone)
    assert f"package: {REPOSITORY}" in _shell_dry_run(lone, tmp_path)
    # Named outright, the source is the one used.
    assert "package: /some/folder" in _shell_dry_run(
        lone, tmp_path, VARYZ_PACKAGE_SOURCE="/some/folder"
    )


def test_installers_ask_unless_told_yes_and_never_hang():
    # setup asks before each agent and data source. The owner's --yes is the
    # only thing that answers for them; without it the questions go to the
    # terminal, and where there is none the installer says so and stops
    # rather than waiting on a prompt nobody can see.
    for script in (PS_INSTALLER, SH_INSTALLER):
        setups = [
            flags for name, flags in _commands_named(script.read_text(encoding="utf-8"))
            if name == "setup"
        ]
        assert any("--yes" in flags for flags in setups), script.name
        assert any("--yes" not in flags for flags in setups), script.name
    shell = SH_INSTALLER.read_text(encoding="utf-8")
    assert "(exec </dev/tty) 2>/dev/null" in shell
    assert "Re-run with --yes" in shell
    assert "or with --web" in shell


def test_windows_installer_puts_the_answer_checker_on_an_nvidia_card():
    # DirectML read agent questions 12x faster on an RTX 5070 with the same
    # scores. It is the same `onnxruntime` module as the plain build, so the
    # plain build is overridden away on Windows rather than installed beside it.
    powershell = PS_INSTALLER.read_text(encoding="utf-8")
    shell = SH_INSTALLER.read_text(encoding="utf-8")
    assert "Get-Command nvidia-smi" in powershell
    assert "--with onnxruntime-directml --overrides $overrides" in powershell
    assert "onnxruntime ; sys_platform != 'win32'" in powershell
    installs = [line.strip() for line in powershell.splitlines() if "tool install $Package" in line]
    assert "& $uvPath tool install $PackageSource" in installs  # no card: as before
    assert "directml" not in shell


def test_published_installers_are_the_checked_in_ones():
    # The prelaunch site deliberately omits installer endpoints. Once downloads
    # are enabled, both scripts must be present and match their canonical source.
    if not (ROOT / "website").is_dir():
        pytest.skip("website/ is not in this checkout")
    installers = [ROOT / "website" / name for name in ("install.ps1", "install.sh")]
    if not any(path.exists() for path in installers):
        download = (ROOT / "website" / "download.html").read_text(encoding="utf-8")
        assert "Downloads are not available yet" in download
        assert 'class="download-btn"' not in download
        return
    for name in ("install.ps1", "install.sh"):
        published = (ROOT / "website" / name).read_bytes()
        assert published == (ROOT / "scripts" / name).read_bytes(), name


def test_powershell_dry_run_has_no_effects(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".varyz" / "config.toml"
    env = {**os.environ, "USERPROFILE": str(home), "VARYZ_CONFIG": str(config)}

    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS_INSTALLER),
            "-DryRun",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "no commands will run and no files will change" in result.stdout
    assert not config.exists()


def test_posix_shell_is_valid_and_dry_run_has_no_effects(tmp_path):
    executable = shutil.which("sh")
    if executable is None:
        pytest.skip("a POSIX shell is not installed")
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".varyz" / "config.toml"
    env = {**os.environ, "HOME": str(home), "VARYZ_CONFIG": str(config)}

    syntax = subprocess.run(
        [executable, "-n", str(SH_INSTALLER)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    result = subprocess.run(
        [executable, str(SH_INSTALLER), "--dry-run"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "no commands will run and no files will change" in result.stdout
    assert not config.exists()


def test_macos_launcher_is_the_shell_installer_and_takes_the_page():
    # Double-clicked, a .command opens in Terminal with its own path as $0;
    # that name is what switches the installer to the browser page, so the
    # launcher stays a plain copy with no second set of install steps to drift.
    assert MAC_LAUNCHER.read_bytes() == SH_INSTALLER.read_bytes()
    if os.name != "nt":
        assert os.access(MAC_LAUNCHER, os.X_OK), "Finder only runs an executable .command"
    shell = SH_INSTALLER.read_text(encoding="utf-8")
    assert 'case "$0" in *.command) web=1 ;; esac' in shell


def test_windows_launcher_runs_the_powershell_installer_with_web():
    launcher = WINDOWS_LAUNCHER.read_bytes().decode("utf-8")
    assert "\r\n" in launcher  # cmd.exe misreads a batch file with bare LF endings
    lines = launcher.replace("\r\n", "\n").split("\n")
    skip = int(re.search(r"Select-Object -Skip (\d+)", launcher).group(1))  # type: ignore[union-attr]
    header, body = lines[:skip], "\n".join(lines[skip:])
    assert header[0] == "@echo off"
    assert header[-1] == "exit /b"  # cmd.exe never reads the PowerShell below
    assert any(") -Web\"" in line for line in header)
    installer = PS_INSTALLER.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert body == installer


@pytest.mark.parametrize("flags", [["--dry-run"], ["--dry-run", "--web"]])
def test_posix_launcher_dry_run_names_the_page(tmp_path, flags):
    executable = shutil.which("sh")
    if executable is None:
        pytest.skip("a POSIX shell is not installed")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    env.pop("VARYZ_CONFIG", None)
    env.pop("VARYZ_BOOTSTRAP_APPROVE", None)
    script = MAC_LAUNCHER if flags == ["--dry-run"] else SH_INSTALLER

    result = subprocess.run(
        [executable, str(script), *flags],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--web" in result.stdout.splitlines()[-2]
    assert not (home / ".varyz").exists()


def test_posix_installer_refuses_web_with_yes(tmp_path):
    executable = shutil.which("sh")
    if executable is None:
        pytest.skip("a POSIX shell is not installed")

    result = subprocess.run(
        [executable, str(SH_INSTALLER), "--web", "--yes", "--dry-run"],
        cwd=tmp_path, text=True, capture_output=True, check=False, timeout=30,
    )

    assert result.returncode == 2
    assert "drop --yes" in result.stderr
