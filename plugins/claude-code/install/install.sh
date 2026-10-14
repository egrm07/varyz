#!/bin/sh
# Install Varyz, then hand the first run to `varyz setup`: the archive, its
# daemon, your agents and your local data sources, asking
# before each agent and source. Setup asks on a page in the browser, where
# agents and sources are ticked, or in the terminal where no browser can open
# (read from the terminal even under `curl | sh`). --yes answers every one of
# those questions with yes; --web keeps to the page even without a browser
# found. A copy of this file named `Install Varyz.command` is the macOS
# double-click installer, and always takes the browser page.
# Varyz is installed from the source folder this file sits in (a clone or an
# unpacked download of the repository), or else from GitHub, which needs git
# and, while the repository is private, a git that is signed in to GitHub.
# Usage:
#   sh scripts/install.sh                  # from this folder
#   sh scripts/install.sh --package <path or git+https://...>
set -eu

repository=git+https://github.com/egrm07/varyz
if [ -n "${VARYZ_PACKAGE_SOURCE:-}" ]; then
    package_source=$VARYZ_PACKAGE_SOURCE
elif [ -f "$0" ] && source_tree=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) \
    && grep -qx 'name = "varyzai"' "$source_tree/pyproject.toml" 2>/dev/null; then
    package_source=$source_tree
    # Git Bash on Windows spells the folder /c/Users/...; uv is a Windows
    # program, so it gets (and the plan shows) the path Windows knows.
    if command -v cygpath >/dev/null 2>&1; then
        package_source=$(cygpath -w "$package_source")
    fi
else
    package_source=$repository
fi
config_path=${VARYZ_CONFIG:-${VARYS_CONFIG:-}}
config_given=0
[ -n "$config_path" ] && config_given=1
approve=${VARYZ_BOOTSTRAP_APPROVE:-0}
release_upgrade_script=${VARYZ_RELEASE_UPGRADE_SCRIPT:-}
release_sha256=${VARYZ_RELEASE_SHA256:-}
release_recovery=""
dry_run=${VARYZ_BOOTSTRAP_DRY_RUN:-0}
web=0
case "$0" in *.command) web=1 ;; esac

while [ "$#" -gt 0 ]; do
    case "$1" in
        --package)
            [ "$#" -ge 2 ] || { echo "--package requires a value" >&2; exit 2; }
            package_source=$2
            shift 2
            ;;
        --config)
            [ "$#" -ge 2 ] || { echo "--config requires a value" >&2; exit 2; }
            config_path=$2
            config_given=1
            shift 2
            ;;
        --recover)
            [ "$#" -ge 2 ] || { echo "--recover requires a backup folder" >&2; exit 2; }
            release_recovery=$2
            shift 2
            ;;
        --yes|-y) approve=1; shift ;;
        --web) web=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$approve" = 1 ] && [ "$web" = 1 ]; then
    echo "--web asks on the page; drop --yes" >&2
    exit 2
fi

if [ "$config_given" = 0 ]; then
    config_path=$HOME/.varyz/config.toml
    # Before 2026-09-24 Varyz was called Varys and kept its default archive in
    # ~/.varys. Re-running the installer there must not start a second, empty one.
    if [ ! -e "$config_path" ] && [ -f "$HOME/.varys/config.toml" ]; then
        config_path=$HOME/.varys/config.toml
    fi
    if command -v cygpath >/dev/null 2>&1; then
        config_path=$(cygpath -w "$config_path")
    fi
fi

case "$config_path" in
    /*) ;;
    # A Windows path, as Git Bash sees VARYZ_CONFIG set by PowerShell.
    [A-Za-z]:\\*|[A-Za-z]:/*) ;;
    *) config_path=$PWD/$config_path ;;
esac

echo "Varyz local-first installer"
echo "  package: $package_source"
echo "  config:  $config_path"

if [ "$dry_run" = 1 ]; then
    echo "Dry run; no commands will run and no files will change."
    echo "Would ensure uv is installed."
    if [ -n "$release_upgrade_script" ]; then
        echo "Would back up and upgrade an existing uv installation from this release wheel; verify before committing; roll back on failure. Close Varyz agents and stop its daemon first."
    else
        echo "Would run, when Varyz and its config are already installed: varyz update apply --config $config_path"
    fi
    echo "Would run: uv tool install $package_source"
    if [ "$approve" = 1 ]; then
        echo "Would run: varyz setup --config $config_path --yes"
    elif [ "$web" = 1 ]; then
        echo "Would run: varyz setup --config $config_path --web"
    else
        echo "Would run: varyz setup --config $config_path"
    fi
    echo "Setup creates the archive, starts its daemon, connects the agents it finds (Claude Code, Codex, Cursor) and offers your local data sources, asking before each unless --yes. It ends by listing every data source and what it holds."
    exit 0
fi

case "$package_source" in
    git+*)
        command -v git >/dev/null 2>&1 || {
            echo "Installing Varyz from GitHub needs git. Install git (on a Mac: xcode-select --install), or run this installer from a downloaded copy of Varyz." >&2
            exit 1
        }
        ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "uv was not found; installing it from Astral's official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    if [ -x "$HOME/.local/bin/uv" ]; then
        uv=$HOME/.local/bin/uv
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        uv=$HOME/.cargo/bin/uv
    else
        uv=$(command -v uv || true)
        [ -n "$uv" ] || { echo "uv installed but its executable was not found" >&2; exit 1; }
    fi
else
    uv=$(command -v uv)
fi

tool_bin=$("$uv" tool dir --bin)
if [ -n "$release_recovery" ]; then
    [ -n "$release_upgrade_script" ] || { echo "Use the downloaded release installer for recovery." >&2; exit 1; }
    "$uv" run --no-project --isolated --python 3.12 "$release_upgrade_script" --uv "$uv" --recover "$release_recovery" --config "$config_path" || {
        echo "Recovery could not complete; the backup was kept." >&2
        exit 1
    }
    exit 0
fi
varyz=$tool_bin/varyz
if [ -x "$varyz" ] && [ -f "$config_path" ]; then
    if [ -n "$release_upgrade_script" ]; then
        "$uv" run --no-project --isolated --python 3.12 "$release_upgrade_script" --uv "$uv" --wheel "$package_source" --sha256 "$release_sha256" --config "$config_path" || {
            echo "Release upgrade did not complete. Read the recovery status above." >&2
            exit 1
        }
        echo "Release upgrade complete. Restart your agents and daemon; existing setup was kept."
        exit 0
    fi
    # Installed already (2026-10-02): `uv tool install` only says so. An update
    # backs the database up before a migration, checks the new version starts
    # and refreshes the connected agents; it takes Varyz from where it was
    # installed from, which `varyz update status` names.
    echo "Varyz is installed; updating it from where it was installed."
    "$varyz" update apply --config "$config_path" || {
        echo "Could not update Varyz (see above). Close your agents, then run: varyz update apply" >&2
        exit 1
    }
else
    "$uv" tool install "$package_source"
    [ -x "$varyz" ] || { echo "varyz installed but its launcher was not found" >&2; exit 1; }
fi

mkdir -p "$(dirname "$config_path")"

if [ "$approve" = 1 ]; then
    "$varyz" setup --config "$config_path" --yes || status=$?
elif [ "$web" = 1 ]; then
    # The questions are on a page in the browser; this window only has to stay open.
    "$varyz" setup --config "$config_path" --web || status=$?
elif (exec </dev/tty) 2>/dev/null; then
    # Under `curl | sh` standard input is the script itself; the questions
    # are for the person at the terminal.
    "$varyz" setup --config "$config_path" </dev/tty || status=$?
else
    echo "No terminal to ask on. Re-run with --yes to connect every agent and source setup finds, or with --web to tick them on a page in the browser." >&2
    exit 1
fi
# 130 is a setup paused in a download; it has said how to carry on.
if [ "${status:-0}" = 130 ]; then
    exit 130
fi
if [ "${status:-0}" != 0 ]; then
    echo "Setup finished with problems (see above). Re-run it any time: varyz setup --config \"$config_path\"" >&2
    exit "$status"
fi
echo "If a new terminal cannot find varyz, run: uv tool update-shell"
