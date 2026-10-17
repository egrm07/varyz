# Release artifacts

**Build from a reviewed commit, test the installed wheel, and retain its checksums.**

**2026-10-18 release policy:** The initial Windows release is unsigned, with a clear
security warning on the download page. Artifact checksums and exact-byte native
acceptance remain required; macOS signing/notarization stays in place.

The application remains version 0.1.0 until publication is approved. Hosted plugin
versions come from their manifests (ChatGPT 0.1.8 and Claude Cloud 0.1.6 on
2026-10-12). A candidate artifact does not close
the signing, native user journey or platform submission gates. Payments were
tested by the owner and work; billing is not an unresolved launch blocker.

## Package verification

The Clean machine workflow runs on Linux, macOS and Windows with Python 3.12.
It installs the locked development and Cloud dependencies, runs Ruff, offline
pytest and the publication marks gate, then builds the wheel and source archive.
The smoke test installs that exact wheel in a separate environment with only its
declared dependencies. It exercises isolated first setup, the daemon, connected
MCP retrieval and General knowledge from another working directory.

A separate Linux job exercises the twelve live PostgreSQL isolation and
concurrency checks against an ephemeral PostgreSQL 16 service. It uses synthetic
accounts and unique schemas, requires no production credentials, and retains its
JUnit results alongside the package artifacts.

```powershell
uv sync --frozen --extra cloud
uv run --no-sync ruff check .
uv run --no-sync pytest
uv run --no-sync python scripts/audit_publication.py --without-archive --gate
uv build --no-sources
uv run --no-sync python scripts/ci_release_smoke.py
```

Use a new directory when preparing another candidate so older artifacts remain
available. The smoke test and native Windows builder accept `--package-dir`;
the Windows builder also accepts `--output-dir`. For example:

```powershell
uv build --offline --out-dir dist/my-candidate
uv run --no-sync python scripts/ci_release_smoke.py --offline --package-dir dist/my-candidate
uv run --no-sync python scripts/plugin/prepare_submission.py --output dist/my-candidate/submission
uv run --no-sync python scripts/release_manifest.py --package-dir dist/my-candidate --output dist/my-candidate/artifact-manifest.json
```

The review handoff uses each embedded manifest's version and refuses an existing
output directory. Provenance records whether the working tree is dirty; its
revision identifies the base commit, not uncommitted changes. Package hashes
identify the actual tested files. A final reviewed commit and three-OS CI remain
required before publication.

Before pushing, also run the archive-backed publication gate locally. The archive
and its full findings remain private and never enter CI. Stage only reviewed
source files before the audit; it inventories the Git index, not untracked files.
Use an alternate `GIT_INDEX_FILE` to preserve existing staged work. The initial
public candidate must have exactly one root commit, with no earlier local history.
Keep local data, account configuration, private reports and signing material out
of the reviewed inventory. Push only the reviewed branch to the authorized private
repository; public visibility and release publication remain separate actions.
New findings need individual review and a reason; do not replace the baseline
with an automatic acceptance.

Each successful runner retains the packages and `release-provenance.json` for
14 days in private CI artifacts. Verify its revision and SHA-256 before using a
package. Independently built ZIP timestamps can change artifact hashes; compare
the included files as well as recording each actual artifact hash.

## Native and plugin builds

Follow [Windows](NATIVE_INSTALLER.md) and [macOS](MACOS_INSTALLER.md) build steps
using the same commit. Windows uses the versioned `scripts/windows/assets/icon.png`;
it does not depend on the private website checkout. The macOS candidate workflow
builds Apple Silicon and Intel packages on candidate branches, verifies their
payloads and tests offline install, repeat install and data-preserving removal.
Its retained provenance records the commit, lockfile and artifact hashes.

Generate plugin packages from the canonical allowlists in `scripts/plugin/build.py`
and `build_archive.py`. Bundle local plugins with the candidate wheel and matching
source archive using `bundle_local.py --package-dir <candidate-package-folder>`.
Hosted review ZIPs contain only generated manifests, skills, assets and notices;
credentials and the local archive must never be included.

Keep a private manifest of the commit, source tree, lockfile, compiler/runtime
inputs, every actual artifact digest, CI run IDs and test outcomes. Preserve old
artifact folders rather than overwriting them. Signing and publication happen
after verification and authorization.
