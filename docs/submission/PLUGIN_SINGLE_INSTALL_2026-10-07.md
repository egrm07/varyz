# Varyz single-install package, 2026-10-07

Version **0.1.7** packages one hosted MCP server and three skills under Varyz.
The existing Varyz publisher draft was updated in place. It owns the hosted
server; it does not depend on the personal Varyz Cloud development connection.

## Saved artifacts and verification

- Source: `scripts/plugin/build.py` and the canonical hosted skills.
- ZIP: `dist/varyz-chatgpt-0.1.7-single-install.zip` (17 files).
- Publisher identity: Varyz organization, developer Egor Menshikov.
- Existing draft: `plugin_asdk_app_6ac4804b02f4819188818b1ffd548453`.
- Existing submission: `appsub_6ac4804b03188191804d3c1375b5603a`.
- Saved portal metadata reports version 0.1.7 and No Issues.
- Portal server remains Configured, Authorized and Domain verified at
  `https://cloud.varyzai.com/mcp/plugin`.
- Fresh portal MCP scan discovers all 13 tools and reports no issues.
- Offline packaging tests: **23 passed**. Ruff and generated-source drift pass.
  The first test invocation failed before execution because its chosen temporary
  parent directory did not exist; a workspace-root temporary directory resolved it.

The ZIP contains portable `mcp.json`, skills, listing assets and license files.
It contains no personal `.app.json` binding. The publisher's MCP conversion owns
the registered server mapping, as required for a combined public plugin. The
instructions use Varyz's bundled native connection and explicitly prohibit a
separate Varyz Cloud installation or a custom-MCP onboarding workaround. Account
sign-in and consent remain required when no valid connection exists.

## Remaining platform work

The portal reports Not submitted and Not published. Review information is
incomplete after reupload; publisher legal/policy attestations must be completed
by the authorized owner. Country targeting, secure reviewer credentials and
supporting materials were not changed or newly asserted during this update.
All three skills subsequently passed the portal checks, verified on 2026-10-07.

The publisher has not made this draft available as a public install. The public
directory install path remains unverified. Private testing does not require
publication; the supported development package uses a registered test connection.

## Full private installation test

The existing private Varyz package was updated in place to 0.1.7, release
`pluginrel_6ac639be7fd08191a517b09fdb28cf36`. Its existing registered development
connection mapping was retained; no additional connection or plugin was created.

The owner then uninstalled both Varyz and Varyz Cloud and asked for the full
installation test. The Varyz 0.1.7 page showed Install plugin. Clicking that
single Varyz install action automatically opened the required Varyz Cloud
connection dialog. Its OAuth reused the existing Varyz login and requested
consent; the owner's prior explicit connection approval was reused. No separate
Varyz Cloud installation action was taken.

ChatGPT returned to **Varyz is ready for use**. The built-in **Set up Varyz**
button started the native getting-started skill. Expanded activity reports
**Made 1 tool call and used Varyz Cloud**. The completed response confirmed the
hosted connection and loaded the owner's actual saved short-reply and sequential
setup preferences. It asked only which useful task to help with, not for settings,
error messages, screenshots, commands or a second plugin installation.

This verifies a single installation action, native consent, automatic dependency
setup, skill execution and hosted preference retrieval after uninstalling both
entries. ChatGPT still names the underlying registered connection Varyz Cloud in
consent and tool activity. This is a host-managed dependency, not an additional
manual installation. A brand-new Varyz account and the eventual public-directory
release were not tested. No public publication was needed for this private test.

No public publication, review submission, agent-initiated uninstall, permissions
weakening, personal-history upload or account creation occurred. The owner
performed the uninstall; the agent performed the authorized reinstall test.
Local executable startup checks are outside this hosted packaging change.

Publisher draft:
https://platform.openai.com/plugins/manage/plugin_asdk_app_6ac4804b02f4819188818b1ffd548453
