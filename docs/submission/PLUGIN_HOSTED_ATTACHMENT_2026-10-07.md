# Varyz hosted attachment diagnosis, 2026-10-07

The owner reports that the installed 0.1.5 plugin still says hosted tools are not
attached and no connection action is available. The previous instruction update
did not establish a native hosted connection.

## Evidence

- Plugin Creator read-back confirms private personal package
  `Plugin_913b3b5264148191a76724390c946ccd`, release
  `pluginrel_6ac62f5e8c308191a9b6a2885f3b19cc`, version 0.1.5. It contains URL-based
  `mcp.json` and `.mcp.json`, three skills and no `.app.json` binding.
- ChatGPT connection inventory reports no installed hosted connection for that
  package ID. This is distinct from the desktop package installation.
- The browser's personal plugin inventory shows Varyz installed as a package and
  labels it desktop-only. Its detail page offers Open in desktop app, without a
  hosted connect action. The already open browser page showed cached version
  0.1.2; the saved backend source is 0.1.5.
- Public endpoint authentication and synthetic-account protocol checks passed in
  the preceding turn. They do not prove tools are attached to the owner's chat.

The supported private testing path is to register the hosted MCP connection in
ChatGPT, obtain its real `plugin_asdk_app_...` technical ID, and bind it through
`.app.json` and `extensions.com.openai.apps`. Root portable MCP URLs remain the
public submission format. Reference:
[OpenAI package documentation](https://developers.openai.com/plugins/build/plugins)
and [registered mapping validation](https://developers.openai.com/plugins/deploy/submission-errors).

## Prepared repair

`scripts/plugin/build.py` now accepts `--private-app-id` to build a separate
private test archive with the verified registered connection mapping. It accepts
the host's technical reference, normalizes it to the mapping's `asdk_app_...` ID,
rejects package IDs/URLs, preserves other metadata and leaves public package
generation unchanged. The separate output prevents personal dependency IDs from
entering the publisher ZIP. No fabricated connection ID has been applied.

Packaging and private/public isolation checks: **23 tests passed**, up from the
previous 17. Focused Ruff and generated-bundle drift checks passed.

## Native authorization and verified repair

The existing installed Orca helper was started without a shell window. Native
inspection works outside the sandbox; the other browser/computer runtimes timed
out. In ChatGPT's Add custom MCP server form, the connection is prepared as
Varyz Cloud, `https://cloud.varyzai.com/mcp/plugin`, authentication OAuth.

Automatic approval review initially rejected acknowledging the custom MCP
warning without specific data-access approval. The owner then replied
"approved" to the explicit ChatGPT connection permission request. Registration
and the native OAuth flow completed; the connection detail page shows
Egor Menshikov's Varyz Cloud account as Primary.

The registered technical reference is
`plugin_asdk_app_6ac633bf3e248191a73dc24717e1590e`. Its mapping ID is
`asdk_app_6ac633bf3e248191a73dc24717e1590e`.

The existing private package was updated in place to **0.1.6**, release
`pluginrel_6ac6349c35c88191922e962dc20cd813`, using a guarded three-file overlay:
both manifests and `.app.json`. Other files, audience and identity were
preserved. Backend read-back confirms the required app mapping. After loading
the updated page, ChatGPT shows **Your cloud plugin**, **Apps 1**, **Varyz Cloud
Connected**, all three skills, version 0.1.6 and **Try in chat**. Previously the
same package was desktop-only with no hosted app attachment.

A native temporary ChatGPT test requested exactly one read-only `start_task`
for project `demo`, task connection verification, with no rule writes, history
uploads or diagnostic questions. ChatGPT completed in six seconds. Expanded
native activity shows **Read confirmed demo project workflows**; the response
reports that the hosted call returned the project's confirmed rules. No setup
question or connection-error fallback appeared. This is a native ChatGPT test,
separate from the earlier synthetic OAuth protocol checks.

No permissions were weakened, personal archive uploaded, account created or
public plugin submitted. The private account's hosted attachment is repaired
and tested. Public packages retain portable MCP configuration; this personal
registered app ID must not be copied into the public submission ZIP. A future
public release still needs its platform registration/review. New users may
still need native account sign-in and consent; manual diagnostics are removed.

No local Varyz executable/startup changes were made, so executable launch and
windowless-startup checks are outside this repair's scope. Unrelated historical
Varyz questions remain outside this task.
