# Plugin journey and launch checks

> Updated 2026-10-05. **Build artifacts remain drafts.** The Cloud context backend is
> deployed; public downloads, platform review and native phone behavior remain gates.
> Full packages target the new `/mcp/plugin` candidate.

## Repeatable build

```powershell
uv build --out-dir dist --cache-dir .build-cache
.venv/Scripts/python.exe scripts/plugin/build.py --zip-hosted
.venv/Scripts/python.exe scripts/plugin/bundle_local.py
.venv/Scripts/python.exe scripts/plugin/build.py --check
```

Outputs: `dist/varyz-local-0.1.0.zip` (wheel, source, installers, local plugins, setup instructions and per-file checksums), `dist/local-release-checksums.json`, and `dist/varyz-chatgpt-draft.zip` (remote-only skills, manifest, icon and MCP mapping). Local installer wrappers use the included wheel; dependencies/models still require network access. Never distribute a recursively zipped checkout, live config, credentials, index or archive. Distribute the corresponding source and licence terms alongside binaries.

## Native journey acceptance

Use a separate disposable test account and a small fixture source containing one known earlier decision. Record client/version, date, tool results and evidence for each row. **A unit test or agent's fluent summary cannot mark these rows passed.**

| Stage | Action | Required evidence |
| --- | --- | --- |
| Discovery without account | Ask ChatGPT for help drafting a reply with preferences supplied in chat | Useful draft; no persistence claim, sale, install, or account gate |
| Contextual local discovery | Ask about an earlier decision missing from chat | At most one relevant explanation; decline ends the offer; no payment link |
| Local install | Explicitly request setup from the supplied release | Consent page; only chosen sources/agents; no Cloud signup/upload |
| First local value | Retrieve the fixture's earlier decision | Actual source reference and correct use; empty index handled honestly |
| Existing account activation | Request Cloud connection | Browser sign-in, active verified entitlement, no checkout initiated |
| Scope choice | Choose one project | Global plus chosen scope explained, future confirmed records disclosed; unchosen project excluded |
| Preview and upload | Preview; explicitly approve upload | Accurate counts, deletions disclosed; only confirmed projection sent |
| Hosted verification | Run `cloud verify` for a selected rule/project | Current rule returned through MCP with bound paid account; phone_verified remains false |
| Phone use | Connect same account in a supported phone tool; ask a fresh real task for that project | Actual hosted tool call returns the selected rule; apply it; record device/client evidence |
| Computer off | Repeat phone task with local computer stopped | Rule still retrieved remotely; no archive/file access claim |
| Reviewed independent agent offer | Express a portability need after useful completed work | Attributed approved copy once, no offer from plugin/OpenAI/unknown hosts; details only on request |
| Dismissals | Ignore, snooze, permanently dismiss; repeat task | No repeat after silence; snooze 30 days; permanent dismissal honored |
| Failures | Expired token, canceled account, wrong account, sync conflict, missing rule | Neutral recoverable status; no false success, automatic checkout, conflict overwrite or scope widening |

Plugin/OpenAI hosts remain ineligible for paid notices. The independent-agent case requires its own host-policy review before adding its exact caller ID to the allowlist. No host is enabled by this build.

## Public download gate

- [ ] Run Windows and macOS/Linux installer dry runs from extracted ZIP paths containing spaces.
- [ ] Complete real clean-device installs and source indexing; verify uninstall/stop behavior.
- [ ] Verify full-product upgrades and interruption recovery on each supported OS. The bundle now has a checksum-verified uv-tool upgrade runner with environment/archive backups, copied-database migration checks, automatic rollback and explicit interrupted-upgrade recovery. Isolated uv tests cover the transaction; public release/update delivery still needs publishing.
- [ ] Audit wheel and source against packaging exclusions, secrets checks and licences.
- [ ] Publish approved ZIP and independent checksums to a publisher-controlled location.
- [ ] Verify public access without GitHub credentials, then update the hosted local-setup reference with the verified URL and setup page.

## ChatGPT submission gate

The draft manifest contains five positive and three negative reviewer cases, existing-account-only copy, required listing URLs, and the existing Varyz icon. It contains no local hooks, install runtime, checkout or paid promotion skill. Specifications: [OpenAI submission documentation](https://developers.openai.com/plugins/deploy/submission) and [plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines).

- [ ] Deploy and verify the new `/mcp/plugin` HTTPS MCP endpoint and its explicit `varyz.plugin` OAuth metadata; old deployed endpoints are narrower.
- [ ] Run discovery/initialize/tools-list/tools-call through the public endpoint, with and without valid account access. Verify account isolation and revocation.
- [ ] Seed a reviewer account with a confirmed short-reply rule, an exact known question/answer and a stated workflow for project `demo`; supply reviewer access through the dashboard, never the ZIP.
- [ ] Run all eight reviewer cases in the native host and correct unexpected behavior.
- [ ] Record a real reviewer-accessible walkthrough; set `extensions.com.openai.review.demo_recording_url` to its verified URL. No fake screenshots or placeholder recording URL.
- [ ] Verify all four listing URLs and support response route; confirm verified publisher identity and current required country/legal settings in the submission dashboard.
- [ ] Obtain platform review before treating directory distribution or phone support as available. A valid package alone is not approval.

Keep Cloud promotional gates off until the backend, payment webhooks, cancellation, backups, privacy flow and supported-device journey are verified. No purchase or test payment is authorized by this checklist.
