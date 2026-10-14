# Varyz automatic hosted setup, 2026-10-07

The owner reported that ChatGPT repeatedly asked them to open connection settings
in Codex and transcribe the status or error. The old Cloud connection reference
explicitly permitted asking for the visible message when the agent could not
inspect the host.

## Changed behavior

Installation onboarding and explicit setup/repair requests now begin with hosted
tool discovery and a read-only access check. Native OAuth runs only when required;
the agent repeats the check after it returns. The skills prohibit settings/status
questions, screenshots, commands, technical setup choices and sending ChatGPT
users to Codex. Missing attachment is reported without a troubleshooting question.
Native sign-in and consent still require the user's action when the host requires
them. Account access and history-upload permissions are preserved.

Canonical skills and generated packages were rebuilt. Two setup cases were added
to the existing five positive reviewer cases; all three negative cases remain.

## Saved release

The existing private personal plugin `Plugin_913b3b5264148191a76724390c946ccd`
was updated from 0.1.4 to **0.1.5**, using its observed release as the update guard.
Saved release: `pluginrel_6ac62f5e8c308191a9b6a2885f3b19cc`.

`dist/varyz-chatgpt-setup-update.zip` overlays only the two manifests and three
changed skill/reference files. Read-back verified exact skill contents, unchanged
MCP mapping, preserved default prompts and preserved manifest metadata outside
version, release notes and reviewer cases. No replacement plugin, public submission,
server rollout, source selection or personal-history upload occurred.

## Verification

- Packaging: **17 tests passed**; generated bundle drift check and focused Ruff
  check passed.
- Hosted capability/authentication checks: **32 tests passed**, one existing
  Starlette/httpx deprecation warning. The sandbox run stalled and was interrupted;
  the isolated tests passed outside the sandbox.
- `scripts/plugin/reviewer.py --check` passed against public HTTPS: anonymous
  discovery, 13 tool authentication challenges, synthetic-account sign-in, OAuth
  consent and PKCE, eight preference/history calls, management denials, token
  refresh and revocation. The reviewer credentials stayed in Windows Credential
  Manager and were not printed. The sandbox could not access that credential;
  the approved check ran outside it.

## Remaining verification

The browser-control inventory timed out. A fresh native ChatGPT conversation
using 0.1.5 could not be exercised in this session. Saved instructions and protocol
checks do not prove the host loads the new release in an already open conversation
or follows the new setup workflow. Installation, sign-in and consent are host
controls; the plugin cannot silently authorize them for the user.
