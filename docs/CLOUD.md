# Plugin and cloud

## Expert billing (2026-10-20, local implementation)

Cloud costs USD 12/month or USD 108/year and shares with two people across projects.
Expert (Varyz Expertise) costs USD 29/month or USD 290/year, removes the
account-wide sharing limit, and offers monthly client subscriptions for selected project
preferences. Clients receive Cloud with their plan. The expert keeps 80% of the net
after tax, refunds and lost disputes; Varyz covers processing fees from its 20%.

Cloud and Expertise bill through Paddle. Client plans bill through Stripe (2026-10-22):
Varyz sells them with Stripe Tax, signed Stripe events at `/api/billing/stripe` control
access and earnings, and experts are paid by automatic Connect transfers to their
Express accounts after the 30-day hold. `/expert` manages the payout account, offers
and earnings; `/client/{token}` binds checkout to the invited email. Lapsed experts
keep client management and clients keep their own paid access. Purchases stay closed
until the Paddle prices and Stripe keys are configured. See [Varyz Expertise](https://varyzai.com/expertise).

## Access expiry (2026-10-16)

Cloud checks access on every protected request and stops at the end of the paid or
free period, even if the payment provider's final notice is delayed.

`GET /api/account` includes `cloud_access`. Protected HTTP requests return 402
with a safe reason and user message. Existing hosted MCP tools return an `isError`
tool result with the same notice before reading context or executing a write.
The agent is instructed to tell the user whether their free period or subscription
ran out, stop Cloud calls and use local Varyz where available. This uses the
existing ChatGPT plugin connection; no new plugin submission is required.

The local client preserves credentials, selected projects, pending uploads and
local preferences. Its Cloud status command, task briefings and desktop checks
explain the reason. Desktop notices appear once per access interruption and
respect the existing notification setting. Confirmed renewal clears the notice
and restores the same connection without setup or another OAuth sign-in. Older
local clients require the updated package to show the specific reason.

## Project-based setup and smart loading — live 2026-10-08

The redesigned browser setup connects this computer, presents one project list,
then previews uploads and removals before explicit approval. Selecting a project
includes confirmed preferences, eligible conversations/documents/Git history and
concise session reports, including future eligible records in that project. General
preferences are always included; general history requires selecting General context.
New projects are never silently enrolled. Existing category-based CLI controls and
older browser helpers remain compatible.

Versioned folders share one logical project in the browser chooser. Report identities
remain exact on the server. Explicit project approval covers later versions of that
project; legacy report approvals remain exact. A remote revocation stops automatic
expansion and can only be renewed by a fresh reviewed selection. Report-only projects
appear in the dashboard through aggregate counts without fetching report contents.

The dashboard has Projects, Connections and Account views. Its initial SQL queries
return aggregate scope names/counts; they do not fetch preference bodies. Opening a
project fetches at most 20 preferences, with indexed tenant/scope queries and a cursor
snapshot. Load more fetches the next page. Reopening a loaded project uses its loaded
content. Failed reads offer retry; expired sessions offer sign-in. Server-rendered
bounded pages remain usable without JavaScript. Pending owner questions are also
paged rather than silently truncated.

The local bridge first checks keyring credentials and refreshes expired access
tokens. Automatic recovery never opens OAuth, changes the account, approves new
projects or uploads data. Browser selections survive reload and temporary local
outages; an expired bridge capability can be replaced using the saved account link.
Initial sign-in uses a user-opened temporary tab so its validated PKCE callback can
close it automatically. Popup blockers fall back to the existing browser flow, with
a return link when the browser disallows closing the tab. Source records, sensitivity
gates, conflict handling and fresh-preview checks remain authoritative.

Default account entry shows **Your Cloud** while checking this computer's saved
connection against the signed-in account. Completed sharing opens the dashboard,
including existing helpers whose saved approved scopes provide the completion
signal. Unfinished previews do not count as approval. **Choose projects** explicitly
opens the chooser even on a configured computer. A small **Skip to dashboard** link
also works without JavaScript or a reachable helper; it changes no sharing consent.
Payment confirmation returns through this same automatic account check. See
`reports/CLOUD_RETURNING_ENTRY_2026-10-08.md` for follow-up acceptance evidence.

Acceptance uses synthetic accounts and isolated local archives:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_computer_connection.py tests/test_cloud_callback_return.py
```

The browser harness exercises the actual Cloud service and local bridge, including
PKCE, temporary-tab closure, reload, expired capability/daemon restart recovery,
explicit upload approval, hosted retrieval and lazy pages at desktop/phone sizes.
It does not test a physical phone, send email, charge a card or upload owner records.
The reviewed Cloud changes are deployed with source/image rollback available.
Public HTTPS browser-script checksums match the candidate and the running service
passed bounded-read and tenant-isolation checks against its SQLite backend.
The candidate wheel and checksum receipt are in `.tmp/cloud-redesign-release/`.
The installed helper has been updated and its running daemon capability verified.
The public HTTPS walkthrough passed at desktop and mobile viewport sizes, including
PKCE closure, expired-token refresh, local restart, draft recovery and hosted sync.
Approved project choices also recovered after clearing browser session storage and
restarting the helper together, without another sign-in or upload. The installed
package passed a fresh read-only doctor with no failed checks.
The local helper update uses the resolved configuration and retains open archive
sessions when every release migration is already applied. It backs up the package,
launchers, configuration and archive, verifies the new package against an archive
copy, and restores the prior background-service markers. A schema change requires
the existing offline migration path. See `reports/CLOUD_REDESIGN_2026-10-08.md`
for acceptance evidence and deployment receipts.

> Plugin refresh 2026-10-05: full ChatGPT and optional Claude Cloud packages now
> connect both preferences and approved history/context. The Cloud backend is
> deployed. See `docs/PLUGIN_PARITY.md` and
> `reports/CLOUD_CONTEXT_DEPLOY_2026-10-05.md`; older build notes below are historical.

> Build 2026-10-02. Free local Varyz; optional paid hosted preferences. **No free trial.** This code is a later-release surface. Public package distribution, infrastructure, sender domain, payment account and platform approval are separate launch gates.

## Quickstart — offline verification

```powershell
uv sync --extra cloud
uv run python scripts/plugin/build.py --check
uv run pytest tests/test_plugin.py
```

The tests use temporary archives and synthetic accounts. They do not index your home, upload preferences, send email or charge a card.

## Guided first task — 2026-10-03

`varyz-get-started` first applies explicit preferences to a real user task. When
earlier background is missing, it explains one relevant local-history benefit and
offers optional setup. Setup remains user-requested; the user selects sources and
agent connections. After local sources are ready, the agent retrieves a relevant
result with local `recall` or `varyz search`, cites source references and uses it on
the task. Empty/irrelevant results are reported rather than presented as success.
Remembering a proposed preference requires confirmation. No paid Cloud promotion,
automatic installation, source expansion or upload is part of this journey.

The shared skill and setup reference live in `plugins/skills/`; the builder copies
them into both local distributions and a separate `plugins/chatgpt/` hosted draft.
The hosted package has separate preference/context MCP URLs and three skills, with no local launcher,
installer or lifecycle hooks. It applies account preferences when connected, or
conversation-only preferences when unavailable (without claiming persistence).
Its history connection retrieves explicitly approved synced records, timelines and
retained-version comparisons. It does not implement free hosted accounts or change
the paid-only Cloud entitlement.
The no-install path is useful task guidance, not a new free hosted service.

```powershell
uv run python scripts/plugin/build.py --zip-hosted
uv run python scripts/plugin/build.py --check
uv run pytest tests/test_plugin.py
```

The ZIP at `dist/varyz-chatgpt-draft.zip` uses only generated hosted files, not a
recursive checkout export. It is a **draft, not submission-ready**: the approved
origin `https://cloud.varyzai.com` is deployed, but native-host validation and
platform review remain open. Hosted setup instructions must not invent an installer link or run
an installer on a temporary hosted execution machine. A first archive result runs
in the supported local agent; the full hosted package can retrieve only approved
synced history through its separate read-only context connection.

Policy basis checked 2026-10-03: OpenAI's [plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines)
require relevance to user intent and prohibit advertising vehicles and indirect
digital upsells. This contextual companion onboarding is a proposed implementation,
not an explicit policy approval. [Submission requirements](https://developers.openai.com/plugins/deploy/submission)
currently exclude lifecycle hooks; the remote draft omits them. Public review of
the companion setup journey remains required.

Verification 2026-10-03: 15 offline plugin tests passed, including both clients'
bootstrap without install/upload and a ZIP export that excludes an unrelated secret
file. Ruff, generated-bundle consistency and all three changed/new skill validators
passed. These checks validate packaging and runtime boundaries, not model compliance
with prose instructions; the complete native-host onboarding remains unverified.

## Local paid notice — 2026-10-03

`src/varyz/promotions.py` holds the approved Cloud copy and device-local dismissal
state. It is separate from owner knowledge, archive retrieval, sync and the news
feed. No task text, source contents or marketing profile is stored. Migration
`0043_cloud_offer.sql` adds one state row to the local database.

The local MCP server advertises `cloud_offer` only when all launch and host gates
pass. It authenticates the registered caller, checks the operator's reviewed-agent
allowlist, excludes OpenAI/ChatGPT/Codex/plugin identities and every package launched
through `varyz plugin serve`, and suppresses offers for an existing Cloud connection
or unreadable account journal. These are conservative exclusions, not a claim that
another platform automatically permits paid promotion. Hosted MCP has no offer tool.

An eligible agent may consider one attributed notice after a useful task and a
successful `finish_check` in the same session/conversation. It must report that the
task is completed and the user expressed a need for preferences across devices,
on their phone, in another supported AI tool or while the computer is off. The
server does not infer those needs from archive records. Model judgment determines
relevance and completion; `finish_check` itself is not proof that every requirement
was met. Away mode suppresses notices.

| Response | Enforcement |
|---|---|
| No response, or agent omits the notice | Issuance is reserved once; no repeat on silence |
| "Not now" | Current offer ID plus actual user response; suppress for 30 days |
| "Stop mentioning it" | Persist disabled state until explicit local reset |
| "Show details" | Current offer ID plus actual user request returns the pricing-page URL; no browser opening, subscription or upload |

`varyz cloud promotions status`, `off` and `on` inspect, disable and reset local
dismissal state. `on` does not enable launch or host gates. All controls are local;
none charges a customer, uploads records, queries billing or changes grants.

Configuration is deliberately inactive:

```toml
[promotions]
enabled = false
cloud_ready = false
reviewed_agent_ids = []
```

Before an operator changes these gates, deploy and verify live Cloud (including
phone/native-platform access and actual paid checkout), review
the specific agent platform's promotion rules and the installation journey, and
allowlist only independently eligible registered integrations. Do not add an agent
just because it runs locally, or route plugin-installed users through a disguised
purchase journey. No integration has been approved or enabled by this change.
The offer's approved short copy omits trial wording; existing pricing and billing
pages continue to explain the service's no-trial terms.

Verification 2026-10-03: 128 tests passed and 6 intentionally retired tests skipped
in the plugin/MCP/owner-relay/config/migration/cloud-launch regression selection;
one existing Windows file-replacement rollback test failed with WinError 5 and
passed on isolated retry (129 distinct tests verified). New tests cover atomic
multi-agent issuance, relevance/completion gates, identity/plugin exclusions,
account suppression, snooze, opt-out, explicit details, away mode, revoked callers
and local CLI controls. Native-agent display behavior and live Cloud remain unverified.

## Install through the plugin

The marketplace catalog lives in `plugins/`, not the repository root. Node.js must be on PATH. The plugin starts a small bootstrap MCP before Varyz is installed; it reports setup requirements without downloading or changing anything. Its setup skill invokes the existing installer, opens its consent page, checks sources and hooks, then migrates the already approved MCP credential to the OS keyring. It removes only the redundant standalone MCP entry, retaining global hooks and unrelated configuration.

```powershell
# Codex — local marketplace in this checkout
codex plugin marketplace add "C:\path\to\varyz\plugins"
```

For Claude Code, add the same `plugins/` directory through its plugin marketplace command and install `varyz@varyz`. See `plugins/claude-code/skills/varyz-setup/SKILL.md`. Native-host interoperability and the public Git/package install path must be verified before advertising a one-click public install. The current source fallback may require authenticated Git.

The optional `varyz-cloud@varyz` entry uses the remote preference and history
connections without a local runtime. It is an alternative, not an automatic switch
from the full local plugin. See `docs/PLUGIN_PARITY.md` for account consent and gaps.

## Connect a paid cloud account

### Connect this computer — 2026-10-04

**Connection recovery (2026-10-07):** the browser retries temporary local transport
failures up to three times and keeps its account-specific connection ticket in
tab session storage, so reloads restore the connection. Authenticated polls renew
the 30-minute idle lease. The helper persists ticket/approval hashes, account binding,
preview selections and results in the gitignored `*.cloud-computer.json` journal;
raw capabilities and OAuth tokens are not stored there. Source refresh waits for
active setup to finish. Repeated finish requests return the original operation's
state without starting another upload. A crash during upload returns `interrupted`
and requires a fresh preview and explicit approval; it never assumes completion.

Windows `varyz daemon install` enables invisible login recovery and checks health
every five seconds. `varyz daemon pause` stops recovery and the helper;
`varyz daemon resume` permits recovery again. Login recovery does not expand
source permissions or Cloud upload consent. The computer must be awake and signed in.

The active account page at `https://cloud.varyzai.com` has a **Connect this computer**
button. On a click, it checks the running local Varyz daemon and opens the existing
browser OAuth sign-in. Browser permission to reach loopback may be required. The
page reports success only after the computer has saved credentials for the same
active Cloud account. **This step selects no projects and uploads no preferences.**

The shortcut requires the updated daemon on its default `127.0.0.1:1207` address.
The three-step account flow was deployed on 2026-10-04; 24 live synthetic
acceptance checks passed (see `reports/CLOUD_SETUP_DEPLOY_2026-10-04.md`).
Real owner-click OAuth and owner-approved uploads remain unverified.

Missing/stopped/older installations, custom ports, denied browser permission and
other Cloud origins retain the expandable AI setup instructions. The browser cannot
distinguish a missing installation from a blocked local service; it does not claim
that Varyz is absent. Installation remains consent-based.

`cloud/computer.py` handles only `/cloud/computer`, checks the exact Cloud Origin
and loopback Host, and requires an expiring single-use request for sign-in. Its
CORS permission does not apply to the daemon's resolution API. It accepts no cloud
URL, config path or archive operation from the browser; credentials remain in the
OS keyring. Browser failures expose no provider/keyring details. The account page
loads its bundled script with a nonce and limits local connections in CSP to the
default daemon address. Project selection, upload preview/approval and real AI rule
use remain the subsequent workflow.

Verification: offline bridge/account tests, browser success/fallback simulations,
and desktop/mobile visual checks cover this step. Live HTTPS browser permission
and OAuth acceptance remain unverified until the change is deployed and local
Varyz is updated; no account was connected or data uploaded during development.

After checkout, the account page shows a guided local connection step (2026-10-03).
Payment returns to the guide; an unconfirmed payment shows a waiting state and does
not grant access. Active subscribers receive a setup request to copy into local
Claude Code or Codex. It covers consent-based installation, the first AI account,
Cloud sign-in, project selection, dry-run counts, explicit upload approval and a
fresh task using a confirmed rule. Additional accounts/computers are optional.
The page detects a local OAuth access/refresh grant and received preferences;
browser sessions, authorization codes and hosted-only connections do not establish
local readiness. AI rule use is checked in the fresh task, not inferred by the page.
Public companion downloads and native connector availability remain launch gates.

### Choose projects, approve and finish — 2026-10-04

The browser shortcut continues in the same card. **Choose your projects** shows
project names and preference counts, with general preferences always included.
Nothing is preselected on a first setup. An explicit **All projects, including
future ones** option expands the scope; leaving projects unchecked selects only
general preferences. Completed steps collapse as the next one appears.

**Review what will sync** shows upload/update and deletion counts, without rule
text or technical commands. This preview changes no saved selection and sends
no preferences to Cloud. **Approve and finish** authorizes the displayed selection
and future confirmed preferences in those scopes. A single-use approval binds to
the current journal and projected records; changes require a new preview before
sync is enabled. Selection is saved under the existing sync lock, avoiding a gap
where autosync could send a draft selection before approval.

The existing versioned sync runs, then hosted retrieval checks a selected rule,
answer or workflow. Empty selections say no confirmed preferences exist yet.
Successful sync with failed verification is reported separately; conflicts stop
for review, and the flow never chooses a version automatically. Existing synced
scope selections remain locked in the picker, preserving the CLI's scope-change
guard. **View your preferences** refreshes the Cloud account page after completion.
Actual AI use is still checked in a fresh AI task; no phone connection is inferred.

Implementation: `cloud/guided.py` creates local previews; `/cloud/computer/choices`,
`/preview` and `/finish` retain the exact Origin/Host and expiring capability checks
from step 1. These endpoints expose only scopes, counts and safe completion states.
Offline acceptance uses synthetic local ledgers and an isolated real Cloud API;
browser simulations cover desktop/mobile selection, review, explicit approval and
stale-preview recovery. Deployment and real browser OAuth acceptance remain open.

Verification 2026-10-04: 187 distinct relevant offline tests passed across regression
and final runs; Ruff passed, and the offline wheel contains the final browser asset
and guided setup module. Desktop/mobile browser simulations passed scope changes,
explicit approval, stale-preview and expired-connection recovery, and completion.

Cloud's design uses the existing Taste Card brand tokens and bird mark, with locally
served Source Serif 4 and IBM Plex Sans (2026-10-03). The Cloud service supplies the shared
styling for signup, sign-in, recovery, verification, connector consent, account review
and the checkout wrapper. Font routes expose only the two bundled regular font files;
no external font service or arbitrary path is allowed. Varyz does not restyle the payment
provider's own embedded payment form. Desktop (1440×900) and mobile (390×844) signup/account
previews were visually reviewed; seventy-eight cloud UI/service regression cases
were verified, including asset allowlisting and CSP font loading.

Signup requires email, password and a unique username (2026-10-03). Usernames use
3–30 ASCII letters, numbers or underscores and are stored lowercase; `Egor` and
`egor` cannot belong to separate accounts. A database unique index protects both
SQLite and PostgreSQL against concurrent claims. Invalid or taken names return a
readable signup form with a retry prompt. Sign-in continues to use email. Legacy
accounts retain a null username until a future username-selection flow; migration
does not assign names on their behalf. No public profile/social surface is added.

Verification: nine signup tests passed, username migration/concurrent SQLite claims
passed, and all twelve live PostgreSQL tests passed. The deployed HTTPS service
rejects missing and case-variant duplicate usernames and returns the saved username
in account metadata. Its existing twenty HTTPS acceptance checks passed. Signup was
visually checked at desktop and 390×844 mobile sizes; synthetic fixtures were removed.

The agent runs these controls for the owner. The owner signs in and approves the connection in their browser; no credential is requested in chat.

```powershell
varyz cloud connect --url https://your-approved-cloud-host
varyz cloud select --all-confirmed --project my-project
varyz cloud sync --dry-run
varyz cloud sync
varyz cloud status
```

Connecting **does not select or upload data**. Selection includes current and future confirmed rules, explicit own answers, stated workflows and task recipes within the selected scopes; global knowledge is included with a project selection. Omit `--project` only to select all projects explicitly. Candidates, learned but unstated workflows, archive entries, git, models, transcripts, source snippets, personal-model facts and credentials are excluded. Detected credentials block the projection rather than silently changing its meaning. After selection, the existing autosync cycle sends and receives the projection when it runs; manual sync is also available. While the resolution daemon runs, sync is also realtime (`cloud/live_sync.py`). A local change is sent about a second after it is committed: the daemon checks `PRAGMA data_version` and syncs when `LocalSync.plan` has work. Another device's change arrives over a long poll: `GET /api/sync?wait=25` is held until a push for the same account wakes it. A server that does not hold the request (no `waited` in its answer) is polled every 15 seconds instead.

| Command | Behavior |
|---|---|
| `cloud connect --url URL` | Browser OAuth + PKCE; access/refresh credentials in OS keyring |
| `cloud select --all-confirmed [--project NAME]` | Explicit projection permission; does not send text |
| `cloud sync --dry-run` | Offline upload/deletion counts, no cloud request |
| `cloud verify --rule-id <id> --project <project>` | Check paid account binding and actual hosted MCP retrieval of a selected confirmed rule; sends no uploads and does not prove phone behavior |
| `cloud sync` | Atomic versioned batches, then paginated changes and tombstones |
| `cloud sync --accept-remote` | Explicitly accept cloud revisions where changes conflict |
| `cloud sync --keep-local` | Explicitly publish local revisions where changes conflict |
| `cloud status` | Local selection plus account entitlement |
| `cloud disconnect` | Local kill switch even during outages; attempts remote revocation |
| `cloud export --output FILE` | Private account export without a paid entitlement |

The hosted Cloud service is run by Varyz and is not part of this package.

Connector tokens issued to claude.ai, ChatGPT or other hosted apps can use the preference tools and read the account, but cannot start checkout, cancel billing or delete the account; those need the account page or local Varyz. The consent page names where access goes (Varyz on this computer, an app on this computer, or the requesting website's host) before the owner allows it.

Disconnect retains the cloud copy and the local version journal. Reconnecting must use the same account; use a separate local configuration to connect a different account. Changing scope after syncing is deliberately refused instead of deleting existing cloud preferences silently. Subscription cancellation, account export/deletion, preference editing, pending answers and device revocation are available on the cloud account page. Account deletion cancels billing before deleting state; a provider outage retains the account rather than orphaning charges.

## What remains available offline

Local Varyz remains fully usable without a subscription. Hosted `start_task`, `ask` and `finish_check` use the selected compiled knowledge even while the computer is off. Unknown questions are queued and answered by the owner; cloud answers sync down as explicit knowledge. A cloud correction appends a local ledger row, and a deletion appends a local retraction. Competing changes stop for review; there is no clock-based last-writer-wins.

**The preferences connection does not implement archive relay.** The separate
read-only archive build is documented in [Approved Cloud history](CLOUD_ARCHIVE.md);
its server deployment and real phone verification are pending. Hosted preferences do not reconstruct unuploaded historical evidence, and do not give ChatGPT or claude.ai local briefing hooks. Server-side preference matching is deliberately conservative: exact normalized questions resolve; other questions ask the owner. Platform agents decide whether to invoke the connector.

## Browser sign-in (2026-10-03)

Google and GitHub sign-in are implemented; deployment credentials and real-provider
acceptance are still required. Unconfigured providers show disabled buttons and
a coming-soon note; configured providers show active sign-in links.
Social signup asks for the same unique 3–30 character username as email signup.
Existing email accounts link providers explicitly from the account page.
Apple is excluded. Browser sign-in persists for 30 days using a revocable,
Secure, HttpOnly cookie.

Auth pages use a bounded 660 px column; provider controls stack below 480 px.
Dashboard columns collapse below 980 px, with account cards stacking below
480 px. Short desktop windows reduce auth spacing; all pages remain scrollable.
