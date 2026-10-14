# Varyz plugin connection fix, 2026-10-07

The owner reported a stranded browser window showing `Use the explicit plugin scope`, unclear connection instructions, and repeated `Couldn't add plugin` errors when adding the replacement archive.

## Changes and verification

- Resolved the current personal plugin from its visible ChatGPT page: `Plugin_913b3b5264148191a76724390c946ccd`. The previous ID was stale. The generic Add failure does not establish its underlying backend cause.
- Updated that existing private personal plugin directly to 0.1.2 through Plugin Creator's guarded update. Saved release: `pluginrel_6ac582610fc88191b3a59a9152659563`.
- Read back the saved release and confirmed its version, new connection guide, unchanged MCP configuration, unchanged archive skill and preserved manifest metadata except version and release notes. Its personal/private scope remains unchanged.
- Verified the existing ChatGPT plugin page displays 0.1.2, one MCP server and three skills.
- The Cloud authorization handler accepts the observed host scope list `varyz varyz.archive.read varyz.context.read varyz.plugin` only with the explicit `/mcp/plugin` resource and explicit plugin scope included. It normalizes the consent and grant to `varyz.plugin`. Unknown scopes and implicit legacy upgrades remain rejected.
- Invalid browser authorization requests now receive a branded error page with no access granted, a Close window button and a fallback when browser policy prevents closing an ordinary tab. Invalid callback URLs and request state are not echoed or followed.
- Connection instructions distinguish the plugin listing from sign-in and require a real hosted tool result before reporting connection success.

## Validation log

The focused offline command passed **109 tests**, with one warning:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_hosted_plugin_capabilities.py tests/test_cloud_security_audit.py tests/test_cloud_service.py tests/test_plugin.py -q --disable-warnings -p no:cacheprovider
```

Ruff passed for the deployment helper, reviewer script and changed capability tests. Packaging separately passed 17 tests.

The initial interrupted rollout and a subsequent rollout both restored the previous source/image. The latter rolled back because its internal HTTP verification request used a loopback Host header, correctly rejected by Varyz's host protection. The verification request was corrected to the public hostname.

The successful rollout changed four sections of `src/varyz/cloud/app.py` and reused the existing running image's dependencies and model cache in a small image layer. It preserved service secrets, database volumes, resource limits and other source. Server backup: `/srv/varyz/plugin-oauth-releases/640734dd-d35b-4b23-8147-b371ff96c4a8`.

Public HTTPS checks returned health 200 and invalid authorization 400 with the new HTML error page and close button. Desktop and mobile browser screenshots showed readable layout; clicking Close window in the controlled browser displayed the fallback when that browser retained the tab.

## Actual live reviewer protocol calls

`scripts/plugin/reviewer.py --check` retrieved only the synthetic review account from OS Credential Manager. Credentials and tokens were not printed or recorded. It signed in, registered a loopback test client, completed CSRF consent and PKCE using the observed multi-scope request, and asserted the token scope equals `varyz.plugin`.

| MCP call | Actual assertion/result |
| --- | --- |
| `initialize` | Successful JSON-RPC result |
| `tools/list` | 13 tools |
| `start_task`, project `demo` | Returned fixture evidence containing `Use short, plain replies` |
| `ask`, project `demo`, question `Which reply style did I choose for demo?` | Returned `Short, plain replies` |
| `context`, project `demo` | Returned `demo-decision` evidence |
| `search`, project `demo`, query `SQLite offline` | Returned `demo-decision` evidence |
| `fetch`, ID `demo-decision` | Returned evidence containing `retained-version comparisons` |
| `timeline`, project `demo` | Returned evidence containing `awaiting review` |
| `compare`, ID `demo-decision` | Returned evidence containing `original` |
| `finish_check`, project `demo` | Returned `Use short, plain replies` |
| `tools/list` after refresh | Successful result; refreshed token retained plugin scope |
| `tools/list` after revocation | HTTP 401 |

Live management checks returned HTTP 403 for GET `/api/export`, GET `/api/archive/status`, POST `/api/archive/sync` with an empty records array, and DELETE `/api/account`. No account deletion or archive upload occurred. The temporary reviewer connection was revoked; the fixture remains available for platform review.

## Limits and remaining work

These live calls ran through the reviewer protocol script, **not native ChatGPT Desktop**. Native host tool calls, all five positive and three negative native cases, successful callback-window closure and Claude native behavior remain unverified. Ordinary browser tabs may require manually closing them; the error page states this explicitly. No public submission or publication occurred, and no personal history was connected or uploaded.

## Follow-up: automatic connection discovery, 0.1.3

The owner reported that setup said tools were unavailable and sent ChatGPT users to Codex despite an existing Varyz browser sign-in. The hosted endpoint previously required account authorization even to discover tool descriptions, and lacked per-tool OAuth declarations and tool-level authentication challenges.

The `/mcp/plugin` endpoint now allows anonymous protocol initialization, ping, initialized notifications and tool descriptions. All 13 descriptions declare only `varyz.plugin` OAuth access in both `securitySchemes` and the compatibility `_meta` field. An unauthenticated or expired-token tool call returns an MCP error result containing `mcp/www_authenticate`, the exact protected-resource metadata address, plugin scope and linking error description. It executes no tool and returns no account context. Explicit invalid credentials still fail discovery with HTTP 401; legacy endpoints and wrong-scope denial retain their authorization requirements.

Setup instructions now use available host tool discovery/connection controls, keep users in their current AI host, explain reuse of a valid browser session in the same profile, and verify the actual hosted result. A browser login alone does not attach tools or authorize the AI host. Fresh OAuth permission approval remains required.

The existing private personal plugin was updated to **0.1.3**, release `pluginrel_6ac627a1bfa08191ab0c58c624e151c8`. Read-back preserved both manifests except version and release notes, preserved omitted source/assets, and confirmed the new guide. Local ChatGPT and Claude Cloud packages were regenerated. The publisher draft was not submitted or published.

The final regression command covering hosted capabilities, Cloud security/service/archive/context and plugin packaging passed **138 tests**, with one warning. Ruff and both generated-package consistency checks passed. The tests cover public metadata with private canaries, denial for all 13 anonymous tools, stale credentials, cross-account access, scope isolation, session reuse with explicit consent, PKCE, refresh and revocation. Initial test attempts failed on Windows temporary-directory access; a fresh workspace test directory resolved that. One fixture ID mismatch and one legacy error-order regression were corrected. A generated archive manifest was synchronized before the final passing run.

Only two reviewed MCP handler sections were deployed over the running server source/image, preserving other working-tree changes, dependencies, secrets and database volumes. Rollback backup: `/srv/varyz/plugin-oauth-releases/4ac84d2c-8b6f-43c5-a6d1-a482e6531ce4`. Startup verified health, the OAuth error page, public tool discovery and anonymous linking challenge.

The updated reviewer script then made actual live public `initialize` and `tools/list` calls, confirmed 13 OAuth declarations, and called each of the 13 tools anonymously: all returned `isError` and the linking challenge. It then repeated the synthetic-account OAuth/PKCE flow and all eight authorized calls in the table above, management denials, refresh and revocation successfully. Credentials and tokens were never printed. These are protocol checks; **native ChatGPT Desktop and Claude interaction remain pending**.

The separate mypy check could not run because this workspace environment does not contain the `mypy` module. No type-check pass is claimed. Local executable installation/launch and window-flash verification were outside this Cloud plugin change; no claim about those checks is made.
