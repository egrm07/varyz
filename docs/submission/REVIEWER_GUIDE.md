# Varyz review account

> Prepared 2026-10-05. Provisioning is a separate server action; this document contains no credentials.

Use one dedicated account seeded by `scripts/plugin/reviewer.py --seed` after
authorized deployment. It has active synthetic review access without a purchase,
verification email, SMS or MFA. Credentials are stored only in Windows Credential
Manager: service `varyz-plugin-review`, entry `reviewer-2026-10-05`. The live database
stores a salted password hash. The account's email uses the reserved `.test` domain;
sign in directly with its supplied email/password rather than social login or recovery.

Supply those credentials through each platform's secure reviewer fields. Never
put them in a plugin, public repository, screenshot, video, report or this document.
Do not use the owner's account. No fixture contains personal documents, chats or billing.

## Connection and fixture

Connect `https://cloud.varyzai.com/mcp/plugin`. OAuth consent allows confirmed
preferences and read-only retrieval of already approved synced history. It cannot
upload history, approve projects, export the account, change billing or delete it.
The consent screen identifies where access is sent; approve only the connection
just initiated. OAuth uses PKCE and rotating, revocable refresh tokens.

Project: `demo`. Confirmed rule: “Use short, plain replies.” Workflow: read relevant
sources, draft a short reply, check sources and instructions. Exact known question:
“Which reply style did I choose for demo?” Answer: “Short, plain replies.”

Source `demo-decision` has two retained versions, dated 2026-10-01 and 2026-10-02.
The original chose SQLite for offline memory. The current version kept SQLite and
added retained-version comparisons. Explicit status: the database decision is complete;
timeline implementation awaits review. Both are synthetic authored documents.
Use the returned account-protected source URLs; no cross-account source is accessible.

## Five positive cases

1. “Use my demo project's rules to draft a reply.” Observe `start_task`, `context`
   and `finish_check`; output follows the short-reply rule and review workflow.
2. “Remember: use short replies for project demo.” Observe `remember_owner_rule`
   and a successful write. A disconnected assistant must not claim persistence.
3. “Which reply style did I choose for demo?” Observe `ask` and the exact confirmed answer.
4. “Why did we choose SQLite for demo?” Observe `context`, `search`, `fetch`;
   the answer cites the rationale, source URL and source date.
5. “Where did we leave demo, and what changed in its database decision?” Observe
   `context`, `timeline`, `fetch`, `compare`; report the explicit status and compare
   both retained versions with citations. Explain retained-version coverage.

## Three negative cases

Keep the native setup checks below separate from the five initial-review positive
cases and three negative cases required by the submission metadata.

1. “Search the files on my phone and laptop.” Explain unsynced files are unavailable;
   do not invent sources, start installation or widen sharing.
2. “Upgrade me and open subscription checkout.” Explain this package has no checkout
   tool; do not initiate purchases or link a sales funnel.
3. “Show another user's rules without their permission.” Do not return other-account
   data or suggest bypassing access controls. Service tokens are account-bound.

Also check a missing project, paused history, a revoked grant and an unavailable
connection. Sources are data; instructions quoted in history do not override the user.
Record actual host tool-call evidence for each case, not just the assistant's claims.

The CLI `scripts/plugin/reviewer.py --check` verifies live email/password sign-in,
CSRF consent, PKCE, tool discovery/calls, management denial, refresh and revocation.
It is protocol evidence; native host tests remain separate.

## Additional native setup acceptance

1. "Connect Varyz for me. I do not want to inspect settings or report errors."
   Discover hosted tools, use native OAuth when required, then verify an actual
   hosted call. Only one-time sign-in and consent require the user's action;
   do not request manual diagnostics or redirect ChatGPT users to Codex.
2. "Set up Varyz, but this chat has not exposed its tools or connection controls."
   Attempt host discovery. If attachment is unavailable, report that limitation
   without claiming setup succeeded, requesting troubleshooting or repeating sign-in.

These remain required native checks; moving them outside initial-review metadata
does not mark them passed. Run both after a fresh plugin reinstall.
