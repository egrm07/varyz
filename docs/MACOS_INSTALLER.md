# macOS installer

> **Current preparation 2026-10-14:** Signed, notarized, stapled and
> Gatekeeper-verified Apple Silicon and Intel candidates were produced from the
> 2026-10-14 source, including task-completion repair and bundled interpreter
> isolation in both the native launcher and its setup child process.
> Native visual onboarding and Installer acceptance remain unverified. Earlier
> status sections below describe their dated candidates, not public availability.

> **Status 2026-10-04:** unsigned Apple Silicon and Intel PKG candidates built on native
> Mac runners. Payload verification, offline tool installation, repeat installation,
> inference-library imports and removal passed on both architectures. Real-Mac
> browser onboarding, migration upgrade and Installer GUI tests remain pending.
> No signed or public Mac release exists.

The build produces separate `VaryzSetup-<version>-macos-arm64.pkg` and
`VaryzSetup-<version>-macos-x86_64.pkg` candidates. Each includes standalone
Python 3.12.13, uv 0.11.2, the Varyz wheel, architecture-specific hash-locked
dependencies, source and license notices. The minimum macOS version is derived
from the bundled dependency wheel tags and enforced by Installer and the app.
The current NumPy dependency requires macOS 14 or later. Intel uses ONNX Runtime
1.23.2 (the available Intel wheel), while other platforms retain their existing lock.

Installer places **Varyz Setup.app** and **Varyz Uninstall.app** in `/Applications`.
That step requires administrator authorization. It opens setup as the console user;
it never initializes the administrator's archive. With no signed-in console user,
open Varyz Setup later from Applications. The launcher opens the existing browser
onboarding without a Terminal window. User runtime versions live under
`~/Library/Application Support/Varyz`; configuration and archive remain outside
that removable application folder. Setup keeps a private log there.

Upgrades use the shared native worker's ownership, process, checksum and migration
checks. Running agents or background services must be stopped before upgrading.
Uninstall asks for confirmation, removes integrations and the installed uv tool,
and **keeps archive, configuration, models and credentials**. Move the two
Applications apps to Trash afterwards; retained runtime folders may also be removed
after uninstall completes. The PKG receipt itself is not a live installation.

## Build on a Mac

From this checkout, with uv 0.11.2 available:

```sh
sh scripts/macos/build.sh
```

The `macos-installer.yml` workflow builds both architectures on GitHub's
Mac runners and saves private candidates for 14 days. It does not publish releases,
launch browser setup, or sign packages. It verifies the payload, performs an offline
tool install and repeated install, imports the bundled inference libraries, then
removes the tool and checks that a synthetic archive marker remains.
It must build the reviewed source revision;
uncommitted local changes are not available to a remote runner. It runs on manual
dispatch or relevant changes pushed to `codex/macos-installer-build` or
`codex/launch-candidate-*` branches.

**Release gates:** run onboarding/upgrade/removal through Installer on both Mac architectures with owner-supervised
window checks, then sign the apps and package using Developer ID, notarize and
staple. Signing requires the owner's Apple credentials; never store them in source.
An unsigned candidate can be blocked by Gatekeeper and is not a public download.
Keep varyzai.com's Mac download marked coming soon until a verified public asset exists.

## Signed candidates (2026-10-13)

`scripts/macos/sign.py` replaces the shell launchers with native launchers, signs
bundled Mach-O code and native libraries inside dependency wheels, rebuilds wheel
RECORD hashes and payload fingerprints, signs both app bundles, and creates a
Developer ID Installer-signed package. It verifies every code signature and the
updated payload before packaging. It requires a fresh staged build, an unlocked
temporary keychain and the Application and Installer identity fingerprints.

The `macos-signed-installer.yml` workflow runs on Apple Silicon and Intel after
manual dispatch or relevant changes on this repository's launch-candidate branches.
It uses these private GitHub Actions secrets: `MACOS_APPLICATION_P12_BASE64`,
`MACOS_APPLICATION_P12_PASSWORD`, `MACOS_INSTALLER_P12_BASE64`, and
`MACOS_INSTALLER_P12_PASSWORD`. Both PKCS12 identities need an Apple-compatible
export. It removes the temporary keychain and exports only packages, signing
receipts and provenance. The signed packages are retained privately for 14 days.
Offline installation, inference imports, repeat install, preserved-data removal
and expanded-package integrity are checked on the actual signed payload.

**Signing and notarization are separate checks.** The initial 2026-10-13 signed
candidates still required notarization; the current preparation status above
supersedes that result. For every rebuilt package, an authorized developer must
provide a secure `notarytool` keychain profile on a
Mac, submit each final PKG with `xcrun notarytool submit --keychain-profile <profile>
--wait`, obtain an Accepted result, staple using `xcrun stapler staple <package>`,
and validate with `xcrun stapler validate <package>` and
`spctl --assess --type install --verbose <package>`. Recompute the published hash
and provenance after stapling. Keep credentials out of shell history, source and
artifacts. Real-Mac Installer/onboarding/upgrade acceptance is still required.
