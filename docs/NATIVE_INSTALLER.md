# Windows native installer

**Download VaryzSetup, install for your Windows user, and choose sources and agents in the browser. Python and runtime dependencies are bundled.**

The initial Windows x64 release is unsigned, as authorized by the publisher on
2026-10-18. Windows may show an unknown-publisher warning or block installation
under stricter security policies. Download only through varyzai.com or the linked
GitHub release and compare the published SHA-256. A checksum verifies file
integrity; it does not establish a trusted publisher signature.

Windows 10 version 1809 or later is required. No administrator rights, Python
installation, GitHub login or manual extraction is required. Selected local models
still need network access and disk space during consent-based setup.

**2026-10-17 launch review:** The retained r27 EXE still reports `NotSigned`.
Its SHA-256 matches the successful disposable Windows VM acceptance receipt:
`2591baeb5cbe2bc84d50db8d367106bb9e0b2925be27fe173726e6167f766294`.
Fresh installation, actual shortcut/browser onboarding, repeat installation,
uninstall/reinstall and preserved synthetic preferences passed on that VM.
The first licence screen was visually inspected; later wizard screens remain
unverified. These results apply to r27, not the newer working tree or signed bytes.

1. Download `VaryzSetup-<version>-windows-x64.exe` and verify its published SHA-256.
   The initial release has no publisher signature; later signed releases must also
   pass Windows Authenticode verification.
2. To upgrade by hand, close Varyz agents and stop its daemon/background sync first. The installer refuses active tools or unsupported receipts; it never force-kills them. Saying "update Varyz" to an agent (`varyz update apply`) needs none of this: it downloads and checks the installer, then runs it silently in the background once the agents' Varyz connections have paused, and they reconnect by themselves.
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

Use Windows **Installed apps → Varyz → Uninstall**, or **Uninstall Varyz** in the Start menu. Nothing needs closing first. The uninstaller checks tool ownership, unregisters Varyz integrations and stops every process Varyz still runs (including windowless learning and setup), then removes its uv tool and native payloads. If something will not stop within 20 seconds, it asks for a restart and changes nothing else. It deliberately retains the archive, configuration, models, credentials and recovery backups. If the tool receipt changed outside the native installer, automatic removal is refused for review.

## Reproducible build

The `Windows native acceptance` workflow builds on a disposable standard public
Windows runner using the locked dependency wheelhouse, managed Python, reviewed uv
licences and the official runner image's Inno compiler. It compares the EXE with
captured source provenance before isolated installation and actual shortcut startup.
Only synthetic test receipts and checksums are retained; it does not distribute the
unsigned EXE. A successful run establishes automated native lifecycle/startup checks,
not a visual wizard review or an Authenticode signature.

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

Only package/source distributions, locked wheels, standalone Python, uv, the installer worker, upgrade worker and explicit notices enter the payload. No recursive repository packaging, local configuration, transcript, credential or archive directory is allowed. `payload.json` verifies every included file; `native-release-checksums.json` publishes the final EXE digest. Embedded checksums establish integrity, not publisher identity. Retain native installation, startup, upgrade and removal receipts for the exact distributed bytes. The publisher authorized the initial unsigned release with an explicit download notice; signing remains available for future releases.

## Publisher signing

The builder supports an existing trusted code-signing certificate in the current
Windows user's certificate store (including a compatible hardware-backed key).
Supply both `--signtool <Windows-SDK-signtool.exe>` and
`--certificate-thumbprint <40-hexadecimal-character-thumbprint>` with the normal
build arguments. Missing or malformed signing inputs fail before staging.
Inno Setup signs both the installer and its generated uninstaller with SHA-256
and an RFC 3161 timestamp. No private key or certificate password is exported.

Before writing release checksums, the builder runs `signtool verify /pa /all /tw`
and checks Windows Authenticode trust, the timestamp and the exact selected signer.
The resulting `native-signature.json` records the signed EXE's SHA-256 and public
signer metadata. Test and retain that exact signed EXE on the disposable Windows
runner; the earlier unsigned EXE's acceptance cannot certify different bytes.
The signing path has automated failure/receipt checks but has not been exercised
with a production certificate. Obtaining a trusted certificate or signing-service
account remains an owner action.

A valid signature identifies the publisher; it does not guarantee an immediate
SmartScreen reputation or a warning-free first download. See Microsoft's
[SmartScreen guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
and [SignTool reference](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool).

## Free signing through SignPath Foundation

[SignPath Foundation](https://signpath.org) signs open source releases for free with
its own certificate, so Windows names **SignPath Foundation** as the publisher, not
Varyz or Egor Menshikov. Its [terms](https://signpath.org/terms) require the build to
come from public source on GitHub-hosted runners, a person to approve every signing
request, and a public code signing policy. That policy is
[docs/website/code-signing.md](website/code-signing.md), published at
varyzai.com/code-signing and linked from every page footer, the download page and
the README.

`.github/workflows/windows-signpath.yml` does the whole run on a fresh
`windows-2025` runner: the same locked build as Windows native acceptance, the
provenance check, upload of the unsigned EXE, the signing request (it waits up to an
hour for approval), an Authenticode check that the signer is SignPath Foundation and
the signature is timestamped, new checksums for the signed bytes, then the same
isolated launch checks on the signed EXE. The signed EXE, `native-signature.json`,
its provenance and the acceptance receipt are kept as the `Varyz-Windows-signed`
artifact for 30 days. The job is skipped until the repository variable
`SIGNPATH_ORGANIZATION_ID` is set. It has not run yet.

Owner steps, in order:

1. Turn on multi-factor authentication for GitHub (already required for the
   release workflows) and, once the account exists, for SignPath.
2. Apply at [signpath.org/apply](https://signpath.org/apply) for `egrm07/varyz`.
   Plain answers: AGPL-3.0 only, with no commercial licence for sale and no
   proprietary parts; the paid product is the optional hosted Varyz Cloud service,
   whose code is in the same public repository. Released since 0.1.0 on
   GitHub Releases; download page varyzai.com/download.
3. After approval, in SignPath: create project `varyz`, signing policy
   `release-signing` with manual approval by Egor Menshikov, and artifact
   configuration `windows-installer` from
   `scripts/windows/signpath-artifact-configuration.xml`. Connect the GitHub
   repository as a trusted build system.
4. In the public repository's settings, add secret `SIGNPATH_API_TOKEN` (a SignPath
   CI user token) and variable `SIGNPATH_ORGANIZATION_ID`.
5. Run **Windows signed installer** from the Actions tab, approve the request in
   SignPath, then attach the signed EXE and its checksums to the release in place of
   the unsigned one. Update the status line on the code signing page and the
   unsigned notes in README, `scripts/site/build_site.py` and this file.

Known gaps to settle before or during the application:

- The uninstaller inside the installer stays unsigned. Inno Setup signs it only
  while compiling, and SignPath signs the finished file that GitHub uploads.
- SignPath asks that software which contacts a server the user did not choose
  shows its privacy policy during installation, with a way to opt out. The hourly
  news check to varyzai.com is on by default, so the installer may need a short
  page that names it, says `varyz news off` turns it off and links the privacy
  policy.
- SignPath rejects projects that include proprietary components. The installer is
  built with Inno Setup, whose own licence has commercial terms (see Reproducible
  build above); mention it in the application.
- Windows metadata: the installer's product name comes from Inno Setup's default,
  `Varyz`, which matches the project name SignPath enforces.

The website keeps **Coming soon** until the release EXE and checksums are anonymously accessible and verified. Separate [macOS PKG candidates](MACOS_INSTALLER.md) for Apple Silicon and Intel are signed, notarized and stapled, with signed-payload installation/removal checks; Installer GUI testing remains pending. Linux packaging is a separate deliverable.
