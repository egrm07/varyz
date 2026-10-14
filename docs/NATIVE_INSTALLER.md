# Windows native installer

**Download VaryzSetup, install for your Windows user, and choose sources and agents in the browser. Python and runtime dependencies are bundled.**

Prepared on 2026-10-04. This is a local Windows x64 candidate, not a signed or published release. Windows 10 version 1809 or later is required. No administrator rights, Python installation, GitHub login or manual extraction is required. Selected local models still need network access and disk space during consent-based setup.

1. Download the published `VaryzSetup-<version>-windows-x64.exe` once available. Verify its independently published SHA-256 and publisher signature before running it.
2. Close Varyz agents and stop its daemon/background sync before upgrading. The installer refuses active tools or unsupported receipts; it never force-kills them.
3. Run the installer, review the licence, and finish installation. Leave **Open Varyz setup in your browser** selected for first setup.
4. Choose sources and agent connections on the setup page. No account is needed for local use. Cloud selection and uploads require separate approval.
5. Reopen **Varyz Setup** from the Start menu to resume setup. Setup messages are recorded privately in the installation's `setup.log`.

## Installation and removal

**2026-10-14 acceptance update:** Testing the actual EXE exposed a startup failure
when its parent supplied `PYTHONHOME` for another interpreter. Installer, removal
and setup entry points now use Python isolated mode; subprocesses retain Varyz
settings but remove foreign Python path overrides. Both native-platform regression
suites pass. The rebuilt actual EXE passed isolated silent installation, repeat
installation and removal, verified its Start Menu shortcut targets and retained
synthetic archive/configuration data. Clean-machine shortcut launch and visual
wizard acceptance remain separate gates; worker-only checks did not establish
those results.

The application folder is `%LOCALAPPDATA%\Programs\Varyz`. Each content-addressed release payload contains its own Python runtime, uv, wheelhouse, corresponding source archive and licence notices. The CLI remains a standard uv-managed tool, so existing agent integration and release rollback behavior are retained. The installer does not modify PATH or publish a package to an index.

Existing supported uv installations are upgraded using `src/varyz/release_upgrade.py`: preserve the old tool environment, launchers, configuration and consistent SQLite backup; verify imports and migrations before committing; restore on failure. Existing custom Python interpreters and receipt extras are preserved. An offline candidate cannot add extras not present in its wheelhouse; such an upgrade fails rather than silently dropping them. Retained runtimes are immutable and remain available for old receipt references and recovery.

Use Windows **Installed apps → Varyz → Uninstall**, or **Uninstall Varyz** in the Start menu. Close agents and stop background services first. The uninstaller checks tool ownership, unregisters Varyz integrations, then removes its uv tool and native payloads. It deliberately retains the archive, configuration, models, credentials and recovery backups. If the tool receipt changed outside the native installer, automatic removal is refused for review.

## Reproducible build

Use Windows x64, Python 3.12, uv and Inno Setup 6.7+. The builder is local-only and requires an existing standalone Python runtime and hash-verified wheelhouse. The Inno compiler is a build dependency only; users never install it. Obtain it from the official signed distribution. Review its commercial licensing terms before commercial distribution.

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.build-cache/uv-native'
uv export --frozen --no-dev --no-default-groups --no-emit-project --output-file .build-cache/native-tools/requirements.txt | Out-Null
uv build --out-dir dist
# Download on the same Python/Windows architecture as the bundled runtime.
uv run --no-project --python 3.12 --with pip python -m pip download --require-hashes --only-binary=:all: --dest .build-cache/native-tools/wheelhouse -r .build-cache/native-tools/requirements.txt
# Place uv.exe and its LICENSE-MIT / LICENSE-APACHE, renamed UV-LICENSE-*, together.
.venv/Scripts/python.exe scripts/windows/build.py --runtime <standalone-python-folder> --uv <uv.exe> --wheelhouse .build-cache/native-tools/wheelhouse --stage <new-staging-folder> --compiler <ISCC.exe>
```

Only package/source distributions, locked wheels, standalone Python, uv, the installer worker, upgrade worker and explicit notices enter the payload. No recursive repository packaging, local configuration, transcript, credential or archive directory is allowed. `payload.json` verifies every included file; `native-release-checksums.json` publishes the final EXE digest. Embedded checksums establish integrity, not publisher identity. Authenticode signing, native wizard/startup testing, clean Windows VM installation, upgrade and uninstall checks are required before public distribution.

The website keeps **Coming soon** until the release EXE and checksums are anonymously accessible and verified. Separate [macOS PKG candidates](MACOS_INSTALLER.md) for Apple Silicon and Intel are signed, notarized and stapled, with signed-payload installation/removal checks; Installer GUI testing remains pending. Linux packaging is a separate deliverable.
