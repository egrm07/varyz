# Connect the hosted Varyz plugin

Use this guide when the user asks to connect the installed Cloud plugin or reports
a failed connection. Local Varyz and the hosted plugin are separate connections.
The plugin detail page describes the package; opening it does not sign in.
An existing local installation must be reused, not installed or configured again.
Discover attached local tools and hook briefings first; use the selected local
workflow when it is already working. If this hosted chat needs its own connection,
that is only a host authorization step, never a reason to open local setup.
Setup is the agent's job. Never ask the user to inspect connection settings,
transcribe a status/error, provide screenshots, run commands or switch AI hosts.

1. Check whether the host exposes this plugin's actual tools. If the host supports
   deferred tool discovery or loading installed plugin tools, use that facility
   first; absence from the initial tool list is not a connection failure. Use a small hosted
   `start_task` call for the current setup request to verify access, without
   searching history or writing preferences. Do not substitute local tools.
2. If the host reports authentication is needed, let its Varyz connection flow
   start. It opens Varyz Cloud for sign-in and permission approval. Tell the user
   only the sign-in or permission approval they must complete, one step at a time.
   Do not ask for a report afterward; repeat the hosted check yourself. If the plugin's
   MCP server entry visibly offers Connect or Sign in, use that action. Do not
   invent a button or imply the listing itself contains an active prompt.
   The flow should reuse a valid Varyz login in the browser it opens, so an already
   signed-in user normally only reviews the host's permission request. A separate
   browser/profile may still require sign-in. Never copy cookies or tokens between hosts.
3. The host starts OAuth for `https://cloud.varyzai.com/mcp/plugin`. Never build,
   edit or reuse an authorization URL: its callback, state and PKCE challenge
   belong to the host. Do not ask the user to paste tokens or passwords into chat.
4. Explain that approval lets this AI host read/update confirmed preferences and
   read already approved synced history. It does not select sources or upload
   history. In review testing use only the designated synthetic account. Respect
   the user's account choice; never silently reuse another account's session.
5. After the host returns, repeat the small hosted call. Report the actual result
   or precise error. A successful local call, installed package, open listing,
   or the user's "connected" reply is not hosted verification.

If no hosted tools or connection controls are available, say which is missing and
use the current host's installed-plugin controls or available diagnostic tools.
Do not default to Codex: keep ChatGPT users in ChatGPT, Claude users in Claude,
and Codex users in Codex. A missing tool attachment is a host loading problem;
reopening the Varyz website or signing in again does not attach tools to the chat.
If the host cannot expose either a tool or a connection control after discovery,
report that this chat has not attached Varyz. Do not ask the user to find an error
or diagnose the host. Authentication and missing attachment are different states;
only a real authentication challenge should start sign-in. Retry discovery once
after a host connection completes, then stop instead of looping.
Continue with supplied context. Do not loop
through the plugin listing, initiate checkout, change permissions, upload records
or claim an empty archive. Do not start onboarding during unrelated work.

If a connection fails, the error page grants no access. Close it and retry from
the AI host so it creates a fresh flow. A browser may keep a regular tab open;
closing that tab is safe. Never close a still-active sign-in/consent window or
claim success just because the window closed.
