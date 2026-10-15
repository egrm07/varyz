# Changelog

All notable changes to Varyz are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Release candidate 2026-10-12.** Version 0.1.0 remains unreleased; `v0.1.0`
> is created only after the release checks pass and publication is approved.

## [Unreleased]

### Changed

- **Native acceptance pipeline (2026-10-15):** Public standard runners can build an unsigned Windows installer from locked inputs and test its actual install, reinstall, removal and Start Menu startup on a disposable Windows host. The workflow retains synthetic receipts and holds the unsigned installer. Mac notarization verifies the exact signing run, source and package hashes before Apple submission, stapling and Gatekeeper assessment.

- **Attributed special-category records (2026-10-15):** Hosted screening also refuses detected explicit personal religion, sexuality, political, union, racial/ethnic, genetic and biometric records where no separately supported consent process exists. General discussion and schemas remain usable; quotations or fiction containing explicit personal declarations can conservatively be refused. This remains incomplete pattern detection.

- **Hosted data boundaries and legal readiness (2026-10-15):** Refuse detected government identifiers, personal health records and attributed under-16 personal records at Cloud writes and hosted retrieval, including legacy sources; preserve privacy exports and stored records. Explain prohibited content before upload approval and OAuth consent. Privacy disclosures cover processing bases, recipients, transfers and rights; new compliance documentation states the limits of automated screening. Heuristics do not establish age, legal consent or comprehensive sensitive-data recognition.

- **Account privacy notice (2026-10-15):** Show the canonical privacy notice before account forms and social sign-in, so users can read how account data is handled before continuing. This notice is not treated as blanket consent to upload history.

- **Source publication documentation (2026-10-15):** Security reporting covers local and hosted boundaries, including Cloud account isolation and revocation. Source installation instructions and release status distinguish public source availability from pending native and plugin release checks.

- **Cloud consent preview ordering (2026-10-15):** Validate the approved preference preview before reading Cloud project-pause state. Changed previews stop before that extra request or any upload; project pause behavior remains applied to accepted syncs.

- **Older SQLite search compatibility (2026-10-15):** Use explicit nearest-neighbor counts for sqlite-vec queries so local search, best-distance checks and conversation-window retrieval work on SQLite versions before 3.41. Verified against Debian Bookworm's SQLite 3.40 rather than relying only on newer managed Python runtimes.

- **Cloud project controls (2026-10-15):** Included the completed project-menu update in the launch source: export one project's preferences as JSON, pause/resume new uploads while retaining saved Cloud context, and confirm project deletion. Pauses cover preferences, history and work reports; other projects continue syncing, and privacy erasure remains available.

- **Task completion reminders (2026-10-14):** Finish checks now include the current task's recorded pending work separately from reusable standards. Stop hooks recognize native Codex tool events and correlate successful check results; working tasks receive a bounded reminder for each new checkpoint. Privacy gates, pauses and blocked handoffs remain respected. Repository repair; installed clients require a separate update to receive it.

- **Bundled installer Python (2026-10-14):** Native launchers ignore inherited Python interpreter settings, and worker subprocesses remove foreign PYTHONHOME/PYTHONPATH values while preserving Varyz configuration. This prevents startup failures when a parent process supplies another Python installation. Windows worker failures now include the exit code.

- **Plugin onboarding and discovery (2026-10-14):** Reuse existing local or Cloud connections without reopening local setup. Missing attached tools and empty history no longer imply an absent installation. ChatGPT listing copy, keywords and starter prompts describe writing preferences, project requirements and approved history; 16 discovery and existing-installation cases accompany the update. Private App packages omit duplicate raw MCP servers. Prepared packages still require host upload and verification.

- **Empty-archive setup (2026-10-13):** A fresh archive now skips personal-model compilation until a source or note is added, instead of failing the model's minimum-record gate. Existing nonempty archives still compile and enforce activation checks.

- **Mac notarization sealing (2026-10-13):** Run payload verification before sealing the app bundle and suppress Python bytecode writes. Verify both app signatures after expanding the actual signed installer, so post-signing resource changes fail before Apple submission.

- **Targeted rollout retention (2026-10-13):** Preserve the activated security-log override and configured Cloud image during targeted source rollouts and rollback. Restored journald logging after the away-mode deployment, with a fresh backup and unchanged image, environment and state mounts.

- **Unattended approval handling (2026-10-13):** Included the worker's local and hosted away-mode fix: retain existing consent, defer interactive client approvals until return, continue independent work, propagate away guidance in hosted responses, and preserve unstored blockers when the question queue is full. Prepared ChatGPT 0.1.9 and Claude Cloud 0.1.7 packages; the existing OpenAI publisher draft remains 0.1.8 until refreshed. Windows CI now allows 75 minutes for its complete suite after the previous 45-minute runner limit expired.

- **Mac signed candidates (2026-10-13):** Added native launchers, nested-code and wheel-library signing, regenerated integrity hashes, and Developer ID package signing. Apple Silicon and Intel private CI verified the actual signed payload and offline/repeat installation with preserved-data removal. Notarization and supervised native GUI acceptance remain required before distribution.

- **Public plugin sharing tools (2026-10-12):** Exposed 18 sharing and following operations separately on `/mcp/plugin`, with operation-specific input validation and permission annotations. Native clients keep the existing interface. All 121 targeted regressions and the exact-image authentication/isolation probe passed; the deployed server's OpenAI publisher scan reports no findings. Plugin 0.1.8 remains an unsubmitted draft.

- **Cloud security-log expiry (2026-10-12):** Activated owner-approved 28-day journald expiry and daily system-log rotation after a fresh backup. Cloud recreation preserved the running image, environment and state mount; health, recovery and OAuth discovery passed. Deployments preserve the server's activated logging override. Accounting retention and final publication remain separate checks.

- **Homepage description (2026-10-12):** Replaced the hero paragraph with the approved question about getting guidance without another prompt, explaining decision reuse, workflow guidance and completion standards. The headline remains unchanged.

- **Shared standards picker (2026-10-12):** Standards appear in batches of 12 with Load more, preserving selections across batches and validation errors. Finish stays visible in a bottom action bar and opens the existing review. Deployed as `standards-20261012-v1`; 105 local regressions and 27 candidate/deployed image checks passed. Existing runtime settings and mounts were verified unchanged; rollback retained.

- **Clean checkout and database gates (2026-10-12):** Plugin review handoffs no longer require private website files. Optional policy HTML must be explicitly selected and is validated before writing. Release CI now exercises the real PostgreSQL concurrency and isolation suite on a disposable database.

- **Windows journal and setup configuration writes (2026-10-12):** Atomic replacement retries transient access and sharing locks with bounded backoff. Persistent failures preserve the previous destination and still reach the caller; journals retain their staged update, while configuration writes keep their existing temporary-file cleanup. Added transient and persistent lock regressions after the release suites exposed access-denied renames.

- **Release preparation (2026-10-12):** Publication audits reject empty file inventories. Candidate wheel smoke tests, Windows builds and checksum manifests accept separate artifact folders; provenance distinguishes a dirty checkout from its base commit. Plugin handoffs use the embedded versions and preserve existing candidates. Initial-review metadata contains five positive and three negative cases; native setup scenarios remain separate acceptance checks. The live reviewer requires the current core tools, permits optional sharing tools and rejects unknown capabilities. Local scratch outputs, operational measurements and plugin repair snapshots are excluded from source publication. Reworded four instruction surfaces to remove archive text matches while retaining their requirements; no publication acceptance exceptions were added.

- **Public Varyz deployed (2026-10-12):** Release `public-20261012-v1` adds the reviewed Choose → Review → Publish dashboard, public username profiles, following and topic selection, and hosted follow tools. Preserves the existing two-account pilot, 10,000-account ceiling, runtime settings and prior Cloud fixes. Verified 122 checkout regressions, 99 exact-runtime Cloud tests, desktop/mobile browser flows, 26 candidate/deployed synthetic checks and 17 live HTTPS checks. Database backup and rollback retained; no owner standards published. See `reports/PUBLIC_VARYZ_DEPLOYMENT_2026-10-12.md`.

- **Feature audit fixes deployed (2026-10-11):** Cloud release `audit-20261011-450a8ae80c` carries the verified token-renewal method, MCP excerpt trust warnings and sharing-option explanations. The installed local client received the renewal fix, including retry-safe overload handling and concurrent token rotation, while preserving its other files and feature set. The full checkout suite passed 3,449 tests; the exact Cloud candidate passed 10 acceptance checks and the installed client passed six regression checks. Live HTTPS/OAuth checks, database integrity and all 24 installed doctor checks passed. Runtime settings and data mounts were preserved; image, Compose, source and database rollback copies remain. See `reports/FULL_FEATURE_DEPLOYMENT_2026-10-11.md` for receipts and remaining provider/platform acceptance gaps.

- **Cloud landing page (2026-10-11):** Integrated preferences, history, progress reports and Varyz Shared into a shorter page with distinct editorial layouts and shared-project roles. The overview explains concrete outcomes: work in your style, recover decisions and continue unfinished work. Restored all six original example questions, removed section/setup numbers and clarified the setup and closing headings. Storage and access details remain in an expandable disclosure. Retained the approved hero and removed Availability. Site tests and local/live responsive checks passed.

- **Restore sharing after the hand design rollout (2026-10-11):** Restored the live sharing switch, existing pilot access and shared AI setup link in the persistent server environment. Project sharing, invitations, permissions, questions, shared context and reports use the new Marcellus, night and marigold design. Deployment environment rewrites now preserve sharing settings, including an explicit disabled state. Verified 90 sharing tests, 21 UI tests, the environment preservation regression, desktop/mobile sharing and context flows, and synthetic signed-in pages inside the running container. The service image and account data were retained.

- **Dot-cloud digital hand (2026-10-10):** the digital hand in the website art is redrawn as God's hand from The Creation of Adam: slim proportions, the index finger reaching straight for the human fingertip and the other fingers curled under. It is drawn as a point cloud that is densest at the fingertip and thins out smoothly toward the forearm. `website/art/hands.webp` (and its copy in `scripts/site/art/`) stays 3344x1882 at 158 KB, and the social image `og-varyz.png` is re-rendered from it. Verified at 1440 and 390 px on the home and How it works pages with no horizontal scroll. On 2026-10-11 the warm glow behind the hands was lengthened upward: the hero art mask now fades in from 6% of the image height (was 36%) along an eased ramp, so the glow reaches behind the headline while the hands stay in the same place.

- **Hands design across the website and Cloud (2026-10-10):** varyzai.com now uses the hands home on every page: Marcellus headings, IBM Plex Sans body, the shared header with Cloud and Get free, the hands art (WebP, 150 KB) behind each hero, heading-left rows, a closing band and one footer. The home title is "Get more done with fewer prompts"; it keeps all 11 FAQ answers, localised prices and full SEO and social tags. Download opens with the platform boxes in the hero. The social image is redrawn in the same style. The Cloud page hero line is shortened in `docs/website/cloud.md`. The cloud.varyzai.com dashboard, local setup and review pages and the sign-in callback page take the same fonts and colours without the hands, and the dashboard's Shared Varyz panel is removed. Verified at 1440 and 390 px with no horizontal scroll; site news, Cloud UI, dashboard loading, setup and OAuth tests pass (104).

- **Sharing notification delivery (2026-10-10):** Deployed as `notifications-edd4022ff538`. Sharing gates passed on isolated SQLite and PostgreSQL, then again inside the running container with synthetic data. Live health and deployed source verified; rollback retained.

- **Extract preferences while setup downloads (2026-10-10):** `varyz setup --web` now reads the chosen sources before it downloads the search models, and when Claude Code or Codex is connected it asks "Extract preferences from your existing chats?" while the download runs. The question shows the share of the plan's 5-hour and weekly limits in percent and how long the reading takes, estimated from the measured Codex reading (2,807 messages: 12% of the 5-hour limit, 2% of the week, about 9 minutes). A yes starts Varyz's own reader in the background; the page then shows messages read, time left and the limits used so far. "Not now" keeps the offer for the agent's first use. The page also says how long all downloads still take, at their measured speed and counting the classifier still to come in step 7.

- **Invitation confirmation popup (2026-10-10):** Successful invitations redirect to the shared project with a one-time modal confirmation. Done or Escape returns focus to the project; refresh neither resends the invitation nor repeats the popup. Account-bound encrypted notices expire after two minutes.


- **Shared project overview (2026-10-10):** People and pending invitations lead the page with a clear next action. Published instructions are labeled Shared preferences and collapsed rather than appearing as a project description. Optional repository/activity and sharing settings are secondary disclosures; pending reviews open automatically. Historical invitations no longer clutter access management.


- **Learning step at setup (2026-10-10):** the first reading of past agent conversations is now the last setup step. The agent explains what it reads, on which plan, what it learns, what it skips and where rules go, then starts it without asking. On "not now" it asks when and calls the new `schedule_reading` tool; nothing is read or asked until then, a reading under way stops, and Varyz's own reader starts at that time. `varyz rules read --now` reads sooner.
- **Shared project card actions (2026-10-10):** Manage people opens invitation and access controls directly from each owner project. Leave project on joined projects opens a named confirmation with Cancel; confirming ends only that membership and returns to With me. Persistent sign-in, CSRF and ownership checks retained; paused projects can still be left. Deployment receipts: `.tmp/sharing-card-actions-deploy`.


- Persistent Cloud sharing sign-in (2026-10-10): routine invitations, replies,
  publishing and reversible access changes use the existing valid session instead
  of asking for another sign-in after fifteen minutes. Sharing form cookies now
  last with the thirty-day session. Irreversible deletion retains its fresh-sign-in
  check; truly expired sessions still restore encrypted drafts. Reproduced the
  reported username invitation failure and verified direct sends with sixteen-minute
  and twenty-day-old sessions across browser/application restarts. The owner's
  persistent-sign-in requirement is recorded for Varyz. Rollout receipts are in
  `.tmp/sharing-persistent-deploy/`.

- Cloud sharing resilience (2026-10-10): malformed action fields and revisions
  return readable validation errors. Inviting from a paused project cannot create
  another active share; a full pending invitation list still permits resending to
  the same recipient. Fully expired sessions restore reply drafts, preference
  selections and invitation confirmation without sending, publishing or joining
  automatically. Unrestorable forms fail safely. Invitation email workers claim
  queued messages to prevent concurrent duplicate sends; failed sends and expired
  worker claims remain retryable. Broad sharing/account regressions and inspected
  desktop/phone flows passed. Exact-image gates and rollout receipts are retained
  in `.tmp/sharing-audit-deploy/`.

- Cloud sharing sign-in recovery (2026-10-10): an expired sign-in opens login and
  restores an encrypted draft for the same account. Sending invitations, publishing
  updates and changing access require an explicit action after recovery. Expired
  drafts and failed sign-ins show readable pages; CSRF and recent-authentication
  checks remain enforced. Desktop and phone recovery screens were inspected.
  Password login, session rotation, wrong-account privacy, tampering and duplicate
  submission checks passed locally and inside the deployed image. Live HTTPS
  recovery preserved the recipient, access and send key without sending an
  invitation. Two-module overlay receipts and rollback: `.tmp/sharing-reauth-deploy/`.

- Cloud project invitations (2026-10-10): use the existing project name and one
  email-or-username field, an access selector and Send invitation. Preference
  selection and publication review stay outside the invitation flow. People without
  accounts receive a signup invitation; joining still requires the verified invited
  email. Deployed as a six-module image overlay with source/image rollback. All 43
  sharing regressions passed; isolated candidate and running-image gates verified
  invitations, joining, privacy and revocation. Live HTTPS health, module hashes and
  the authenticated invitation screen passed; no real invitations were sent during
  verification. Deployment receipts: `.tmp/sharing-invite-deploy/`.

- History before clarification (2026-10-08): shared agent guidance requires relevant
  approved project history before clarifying or choosing ambiguous scope, including
  small changes. Cloud `start_task` accepts optional task text and returns bounded
  source excerpts automatically under the existing archive grant and reader limit.
  Older calls explicitly require task-specific retrieval; preference-only tokens
  retain their original scope. Authenticated local briefings retrieve permitted
  project evidence within disclosure budgets; rules-only hooks retain the reminder
  on subsequent task prompts. Hosted bundles are ChatGPT 0.1.8 and Claude Cloud
  0.1.6. Deployment, acceptance and distribution limits are recorded in
  `reports/HISTORY_BEFORE_CLARIFICATION_2026-10-08.md`.

- Cloud account entry (2026-10-08): show the neutral Your Cloud heading and a small
  Skip to dashboard link. Default entry checks the saved local connection against
  the signed-in account and opens the dashboard when sharing is already approved,
  including older helpers and accounts without the usual server connection hint.
  Explicit Choose projects still opens the chooser. Payment confirmation returns
  through the same account entry check. Deployed with source/image rollback;
  acceptance evidence is in `reports/CLOUD_RETURNING_ENTRY_2026-10-08.md`.

- Cloud website guide (2026-10-08): varyzai.com/cloud describes session reports
  (separate per-project approval, agents' own accounts of their work) and Google or
  GitHub sign-in, and its availability note says history search and session reports
  run in the private deployment. Same layout; reviewed at desktop and phone widths and
  published as Worker version 4bc7721a.
  A later pass names ChatGPT, Claude, Codex and Cursor instead of "connected AI",
  sharpens the lead and search description, and was published as Worker version 27a3e427.

- Cloud pricing (2026-10-07): USD 12 a month or USD 108 a year, tax included. Euro
  countries pay EUR 12 / 108 and the UK GBP 9.99 / 89.99; elsewhere Paddle converts the
  dollar price. The Cloud account page shows each buyer their own currency from a Paddle
  pricing preview, checkout charges in that currency, and `GET /api/pricing?country=XX`
  serves country prices to varyzai.com, whose Worker (`deploy/site/worker.js`) swaps them
  into the home and pricing pages. The dollar price is the fallback everywhere.

### Fixed

- Cloud session reports deployed (2026-10-08): production at cloud.varyzai.com now
  serves `/api/reports/*` and the read-only `session_history` tool. The candidate
  check in `deploy/cloud/verify_archive.py` expects 15 plugin tools (it still
  expected 14). Candidate search: 3.6 to 4.1 seconds per query on 19,880 synthetic
  chunks, 785 MiB peak. Live Paddle mode and the earlier connection patches are kept.
  An authenticated report round trip on production has not been run yet.

- Release candidate (2026-10-07): the local `route_task` tool carries the same
  quoted-data warning as every other tool, and the checked-in archive plugin manifest
  is regenerated to match its generator. The setup-page close test waits for the
  close flag instead of racing the reply, which failed once on Windows CI. Launch status notes in `SHIPPING.md`,
  `RELEASE_CHECKLIST.md`, `PADDLE_SETUP.md` and `CLOUD_HANDOFF.md` now match what is
  live (site at varyzai.com, Cloud pricing deployed, repository still private).

- Cloud billing (2026-10-07): the account page lists every plan with its price
  from Paddle (the annual plan was unreachable), shows the renewal date or, after
  canceling, the date access ends, returns to the dashboard after canceling, and
  shows checkout or cancel failures as a page instead of raw JSON. Verified with
  real Paddle sandbox purchases of both plans and a dashboard cancel.

- ChatGPT hosted plugin 0.1.7 (2026-10-07): bundle the hosted preference/history
  server and three skills under Varyz, remove manual setup diagnostics and a
  separate-connection installation workaround. Private clean reinstall of Varyz
  automatically invoked the required connection flow and the getting-started
  skill retrieved saved preferences. Public publisher draft updated; not published.

- Privacy notice (2026-10-07): publish the canonical approved-history notice with
  verified private hosting, email processing and application backup details.
  Disclose Resend's US storage and separate Hetzner disk backups; keep unresolved
  security-log and billing-record retention visible as public paid-launch checks.

- Release verification (2026-10-06): explain `cloud archive control --entry` in
  CLI help, align guided connection tests with project-history previews, and
  check that prelaunch downloads omit installer endpoints. The clean-machine
  smoke now explicitly saves its cross-project fixture in General; CI installs
  the Cloud test dependencies from the lockfile.

### Added

- **Public Varyz (2026-10-11):** creators publish the standards they actually apply
  to one kind of work (code review, writing, design) at `cloud.varyzai.com/@username`,
  in three steps: pick a suggested topic, tick General preferences, review the
  exact text and publish. Anyone follows by username for free, from the profile
  (signed out, the follow completes after sign-in), the `follow` tool or
  `varyz cloud follow`. From the next task `start_task` carries the matching
  standards and `ask` can answer preference and delegated questions from them.
  The follower's own Varyz always wins; one creator per topic, chosen by name in
  the task, then the follower's pick, then the latest follow, with plain-text topic
  matching and everyday aliases ("review this PR" is code review). Publishing needs
  the Cloud plan; there are no creator fees or payouts. Local Varyz sends a task to
  Cloud only when the person follows someone. 14 new tests; 122 sharing, following
  and relay tests pass.

- Shared Varyz collaboration (2026-10-09): owners pick a level per collaborator
  (Consult, Contribute, Decide). Code travels through GitHub; Varyz carries the
  context. Guests answer from owner preferences, then partner decisions, then their
  own Varyz (marked provisional), then ask a person. Contributors post short work
  reports (branch, commit, run command); provisional answers and disagreements go
  to the owner as review items. A GitHub handshake walks both agents through
  access. While the owner is away, questions go to Decide partners. The owner gets
  one `collaboration` tool and a short digest in `start_task`. Guests stay free at
  every level; usage metrics are counts only. Deployed to cloud.varyzai.com as an
  overlay image (`shared-f7c48798a826`) with database backup and rollback compose;
  14 isolation and concurrency checks pass on SQLite and PostgreSQL, and the
  joemama/egr HTTPS acceptance passes 16 of 16. A native AI client on the guest
  side is not yet verified. See `docs/SHARED_VARYZ.md`.
- Add people by username (2026-10-09): tell your agent "add egr to this project at
  Decide" and it calls `collaboration` with action `add`, or runs
  `varyz cloud share add egr --level decide`. The first add shares the project's
  confirmed preferences (never General ones) and invites that account; adding a
  member again changes their level. The guest's agent sees the invitation in
  `shared_projects`, asks once and joins with the new `shared_join` tool, so no
  link or dashboard is needed. `remove` and `varyz cloud share remove|status`
  cover the rest. Live on cloud.varyzai.com since 2026-10-09; the real HTTPS test with joemama and egr passes 19 of 19 checks.
- Shared Varyz notifications (2026-10-09): invitations, questions, answers,
  partner decisions, access requests and work reports now reach the right person
  by email (pending updates grouped together, one-click
  unsubscribe, never content) and as a desktop notification from the local
  daemon. `varyz cloud notifications` turns either off and sends a test. Sharing
  now needs the other person to have a free Varyz Cloud account; without one the
  owner is told to ask them to sign up first. Pending invitations show a **Join**
  button under Shared with me. Deployed 2026-10-09 as shared-9f88f231258b; live HTTPS test 20/20. Windows toasts say "Varyz", show the logo and stay 10 seconds before moving to the notification centre.

- Local/hosted coordination (2026-10-07): `route_task` selects one workflow
  backend per conversation, with revision-checked handoffs and suppression of
  duplicate briefings, questions and new preference writes. Authorized history
  remains available on both. Updated agent instructions prefer working local
  Varyz and use Cloud when local transport is unavailable. Verified with 266
  regression tests and an installed-local/live-Cloud round trip. Requires the
  agent handshake; project aliases and away queues are not synchronized by it.

- Hosted plugin review candidate 0.1.1 (2026-10-05): portable ChatGPT manifests,
  one consented `/mcp/plugin` resource, exactly five positive and three negative
  reviewer cases, remote Claude README/licences, synthetic reviewer tooling and
  isolated submission artifacts. Legacy scopes stay narrower; plugin grants cannot
  upload history or manage/export accounts. Updated canonical privacy and service
  terms for approved-history retrieval. Live staging/native upload were blocked by
  automatic approval review; no deployment, submission or publication occurred.
  See `docs/PLUGIN_SUBMISSION.md` for the remaining gates.

- Plugin capability refresh (2026-10-05): combine confirmed-preference tools and
  approved Cloud context/history in the ChatGPT package; add an optional remote
  Claude package with the same shared skills. Route source retrieval, timelines
  and retained-version comparisons through the deployed read-only context grant.
  Keep full local Claude Code/Codex retrieval and hooks; rebuild local package
  artifacts separately from existing installer candidates. No personal upload or
  platform publication; local/Cloud retrieval differences remain documented in
  `docs/PLUGIN_PARITY.md`.

- Cloud context deployment (2026-10-05): combined read-only preferences/history
  connection, dated project timelines, encrypted retained-version comparisons and
  full account export. Guided setup approves project history and General separately;
  browser pause/revoke works while the computer is off and blocks stale uploaders.
  Shipped the cached semantic-search image after isolated contract/load tests;
  live HTTPS and official MCP SDK verification passed. Recovery uses disk staging,
  streaming hashes and bounded encrypted snapshots at the existing destination.
  Updated the installed client without personal uploads. Phone-session validation
  and public launch remain pending; version stays the unreleased 0.1.0.

- Website headline transition (2026-10-05): "Install / yourself" becomes
  "yourself / everywhere" through a shared-word slide, reversed on return.
  Refined to 1.25 seconds with delayed line reveals and padded inline captures
  that preserve italic overhangs and descenders without shifting the layout.
  Keeps the original static word capture throughout the slide to avoid an
  incoming live-text handoff; matches headline sizes through the mobile breakpoint.
  Supports browser Back; reduced-motion and unsupported browsers navigate normally.
  Removed the homepage's top Launching soon label.

- Cloud website guide (2026-10-05): concise question cards, feature overview and
  three-step sharing flow; describes history search as built with release checks
  remaining. Uses the owner's two-line "you / everywhere" headline, with yellow
  italic "you" matching the homepage and Cloud beside the header wordmark. Linked from
  the main website and published after desktop/mobile review.

- Approved Cloud history (2026-10-05): opt-in project approval covers eligible
  existing and future records; unclear project associations belong to separately
  approved General. Adds encrypted bounded snapshots, resumable sync, deletion,
  pause controls, source citations, optional local hybrid retrieval and a separate
  read-only ChatGPT plugin draft. Deployment, archive backup capacity and a real
  phone test remain pending. See `docs/CLOUD_ARCHIVE.md`.

### Fixed

- Website header logos (2026-10-05): all main-site pages use the homepage's
  wordmark size, weight, spacing and icon dimensions at each viewport size.

- On-demand startup (2026-10-05): `varyz daemon on-demand` removes only owned
  login recovery, preserves first-request autostart and marks intentional idle
  state as standby. It does not start services or alter source-sync settings.

- Windows startup (2026-10-05): the daemon watchdog now uses a native GUI
  launcher with console-free Python creation, drained diagnostic pipes and
  preserved failure status for retries. Connection refresh updates an existing
  owned startup registration without starting services. Local installed code
  was rebuilt from the checkout; owner-observed fresh-start verification is pending.

- Cloud setup (2026-10-05): show one setup step at a time, hide the dashboard
  during setup, and show a clear completion confirmation only after sync finishes.
  Continue reloads the dashboard with current preferences; empty selections explain
  that there are no preferences to check yet. Published with a source backup,
  live-container setup checks and public HTTPS verification.

- Owner questions (2026-10-04): delegated implementation lookups no longer create
  new questions in normal mode. Local and hosted agents check existing answers and
  ask only consequential owner choices, using short questions and brief options.
  Technical context stays internal; permission wording retains material consequences.

- Vector retrieval (2026-10-04): expand a bounded chunk candidate pool when repeated
  passages from a long document would otherwise prevent distinct nearby entries from
  reaching rank fusion. Keep each entry's nearest passage and stop on index exhaustion
  or the scan cap. The GBrain-inspired comparison and independent synthetic replication
  run through `scripts/evaluate_gbrain_techniques.py`.

- Release verification (2026-10-04): regenerate double-click and website installer
  copies with `scripts/sync_installers.py`; retry transient Windows directory locks
  when publishing complete search models or a built macOS app, preserving atomic
  rename and resumable staging on failure. The release copy and its local payment
  gate check distinguish the optional hosted Cloud service from free local use.

- Windows MCP startup (2026-10-04): client registrations now use a native GUI
  launcher instead of the console `varyz.exe`. The launcher starts Python with
  `CreateNoWindow`, preserves binary stdin/stdout and stderr, and propagates the
  server exit status. Connection refresh migrates existing console registrations
  and refreshes older GUI launchers belonging to the same Python installation.
  Dry-run registration does not compile or write launchers.

### Changed

- Analytics consent (2026-10-04): browser setup opens a separate optional popup with
  Egor's solo-developer note, explicit share/no-thanks choices and an honest disclosure
  that messages never enter reports while the server sees the network address during
  delivery. Escape preserves existing consent; declining revokes it on a setup rerun.

- Cloud setup (2026-10-04): connect this computer, choose projects, then review
  counts and approve. The local bridge keeps selection and preview local until
  approval; sync and hosted retrieval checks run automatically after approval.

- Cloud interface (2026-10-04): calmer responsive layouts, clearer preference
  cards and account controls, section navigation, and consistent sign-in,
  recovery and verification forms. The connection guide collapses only after
  a local computer is connected and preferences have been received.

### Added

- Optional improvement analytics (2026-10-04): default-off browser/terminal setup
  consent; bounded counts for answer outcomes, repeated questions, rule reviews,
  setup and reliability; local preview, pause, revocation, delivery and doctor.
  The existing HTTPS server now accepts strict aggregate reports and protects
  creator summaries with a separate credential. Personal content and permanent
  user identifiers are excluded; synthetic probes do not count as usage.

- Continuing doctor repairs (2026-10-03): generated repair prompts and installed
  agent instructions let the parent answer while repair continues; the ten-minute
  and single-pass cutoff is removed. Repair jobs renew their lease during long
  work and cannot mark recovery while a fresh doctor check still fails.

- Doctor repair handoff (2026-10-03): `varyz doctor --fix` tries stopped-daemon
  and agent-connection repairs, then verifies. Failure notices can ask the working
  agent for a background repair subagent through the managed `AGENTS.md` and
  `CLAUDE.md` block, with one 20-minute lease per archive and a bounded repair pass.
  Pauses, consent and upgrade locks remain in force; rule-reader jobs never spawn
  recursive repair jobs.

- Operational doctor (2026-10-03): `varyz doctor [--json]` checks the archive,
  daemon, agent credentials and hooks, source sync, autosync heartbeat and reader
  failures. AI session hooks and MCP calls relay a short failure note in the
  working chat, check at most once a minute and suppress unchanged problems.
  Checks do not repair state, load models or send archive content.

- Post-purchase Cloud connection guide (2026-10-03): directs subscribers through
  local Varyz, their first Claude Code/Codex account, consent-based sync and a fresh
  task check, with optional additional connections and payment-confirmation status.

- Headless rule reader (2026-10-03): varyz starts the rule reader itself instead
  of asking the working agent to. It runs `claude -p` (Haiku) or `codex exec` (newest
  luna) with only the two reading tools and no transcript, on whichever has more
  plan left. Large readings (the backlog, `varyz rules mine` on a sign-in) need more
  than 30% of the five hours and 25% of the week left; small (live) ones more than
  10% and 5%. Usage is polled while it reads. A reader that crosses a limit stops,
  keeps what it submitted and resumes when the window resets. `varyz rules read`
  (`--status`, `--stop`) and `[workflows] reader_*` control it.
  When varyz cannot run it, the agent runs it: connecting adds a marked section to
  `~/.claude/CLAUDE.md` and Codex's `AGENTS.md` that allows the agent to start the
  reader subagent without asking. The owner's own text is kept and `varyz uninstall`
  removes the section.
- Quiet rule mining (2026-10-03): daily and live readings are never mentioned to the
  owner and their results are not relayed. Only a major reading (the first, or 300+
  unread messages) is announced, once a month at most, in one plain line naming the
  owner's own plan, the sensitive-message skip and `varyz rules review`. The Claude
  Code subagent is now `varyz-rule-miner`; connecting removes the old `varyz-reader`.
- Cloud browser sign-in (2026-10-03): Google and GitHub OAuth flows with PKCE,
  browser-bound state, verified emails and explicit account linking. Social
  signup requires a unique username. Provider credentials are required before
  buttons become active; icons remain visible while setup is pending. Apple is
  excluded. Responsive auth/dashboard layouts and
  revocable 30-day sign-in cookies are deployed to Hetzner.

- Live reading (2026-10-03): what the owner says to an agent is read as they say it.
  Each prompt nudges the daemon, which reads the AI chats' newest messages into the
  archive two seconds later (`POST /v1/live/nudge`). A prompt that holds an
  instruction or a preference gets a background job for the `varyz-rule-miner`
  subagent, limited to the last day (`profile_reading_task` with `recent: true`).
  It is asked at most every 5 minutes, and at once for "from now on", "always" or
  "never". Wording like that is now a trial signal of its own, so the rule is in
  effect on trial for the next task. `[workflows] live_reading`,
  `live_reading_min_minutes` and `live_reading_hours` control it.
- Realtime Cloud sync (2026-10-03): while the daemon runs, a local change is sent
  about a second after it is committed. Another device's change arrives over a long
  poll (`GET /api/sync?wait=`, up to 25 s, woken by that account's pushes); servers
  that do not hold the request are polled every 15 s. The server caps held requests
  with `VARYZ_CLOUD_SYNC_WAITERS` (default 8, 4 per account).

- Cloud signup usernames (2026-10-03): new accounts choose a unique 3–30 character
  handle using letters, numbers and underscores. Handles are stored lowercase;
  SQLite/PostgreSQL enforce uniqueness ignoring capitalization. The dashboard and
  account exports include the username. Existing accounts and email sign-in are
  preserved; no handle is invented for a legacy account.

- Trial rules (2026-10-03): a rule the reader found with strong evidence (a
  correction, two conversations or two projects) goes into effect at once, labelled
  as unconfirmed in briefings; it holds for good after 3 tasks over 7 days without a
  correction. `varyz rules review` spot-checks 10 of them, new trials pause when
  fewer than 90% are kept, and `varyz rules trials` / `rules activate` show, stop,
  undo and sort them. Visions, decisions, workflows and rules that loosen what an
  agent may do with sensitive things always wait for review. On by default
  (`[workflows] trial_rules`).

- Downloaded-release upgrades for existing uv-managed installations (2026-10-03):
  retained environment/archive backups, copied-database migration verification,
  failure rollback, interrupted-upgrade recovery and busy-process gates. Keeps
  configuration, sources and existing credentials; downloaded wheels do not fall
  back to a package-index update. Full-product cross-OS upgrades remain release checks.
- Existing-account Cloud activation workflow and read-only selected-rule verification
  through hosted MCP (2026-10-03). Prepared local wheel/source/installer bundle with
  checksums and remote-only ChatGPT listing metadata, icon and eight reviewer cases.
  Native-agent/phone testing, live hosting, recording and public release remain pending.
- Local agent-delivered Cloud offers (2026-10-03): approved cross-device/phone copy,
  authenticated reviewed-integration and service-readiness gates, one atomic offer,
  30-day snooze and permanent opt-out with local controls. OpenAI/plugin paths,
  unknown integrations, existing connections and away tasks receive no offer.
  Launch readiness and integration review remain off; no paid promotion is activated.
- Guided plugin discovery and first-result workflow (2026-10-03): apply explicit
  preferences to a real task, explain relevant local-history benefits, offer optional
  consented setup and cite a first local result. Includes a remote-only ChatGPT draft
  package; hosting, public installer, listing assets and approval remain launch gates.
- Local Claude Code and Codex plugin bundles with an explicit setup workflow
  (2026-10-02). Bootstrap performs no install or indexing; migration preserves the
  existing grant in OS keyring, removes redundant MCP registration and retains hooks.
- Optional paid cloud implementation (2026-10-02): verified accounts and recovery,
  connector OAuth with PKCE, hosted compiled preferences, confirmed-only opt-in sync,
  version conflicts and retractions, no-trial checkout/webhooks, export/deletion and
  PostgreSQL storage. Deployment, actual billing, native platform interoperation and
  public distribution remain release gates; archive relay is not included.
- Everyday kinds of work (2026-10-03). Varyz now recognises everyday tasks: writing and
  email, job applications, travel, shopping, cooking, social posts, events and study.
  Each brings its own open questions, such as the tone for a recipient or a trip's
  budget. Rules the owner files under their own kind of task, such as `gardening`,
  now reach agents when a task names that kind; before, they were stored and never shown.
  Workflows filed under such a kind reach agents the same way, `varyz rules checklist`
  lists the owner's own kinds next to the built-in ones, and the profile reader files a
  lasting everyday preference ("Book an aisle seat.") under its kind of work instead of
  as a rule for all work.

### Fixed

- Hosted Cloud billing acceptance fixes (2026-10-03): malformed signed subscription
  payloads return 422 without consuming the event; lifecycle events without account
  metadata find the already linked subscription. Paddle dashboard refunds of the
  entire charge can be labelled `partial`; matching approved amount and currency
  now revoke access, including refund-before-payment delivery. Smaller partial
  refunds retain access. Existing SQLite/PostgreSQL ledgers upgrade in place.
  The HTTPS edge sends HSTS. Live sandbox evidence and remaining gates are in
  `reports/CLOUD_TESTS_2026-10-03.md`.
- Cloud sign-up, sign-in and connector consent work in real browsers (2026-10-03). Pages
  now send a same-origin referrer, so form posts carry an origin the cloud accepts, and
  consent may redirect to the connecting app. The consent page names where access goes.
  Behind the edge proxy each visitor gets their own rate limit. Connector tokens can no
  longer start checkout, cancel billing or delete the account. Apps on the owner's
  computer can register loopback or app-scheme callbacks. Answered questions no longer
  use storage, and a full question queue makes the agent ask the owner directly instead
  of failing. A stalled checkout can be retried, and switching plans cancels the unpaid
  earlier checkout. Tool failures reach the agent as tool errors. The container installs
  hash-pinned dependencies.
- Setup runs the same steps in the terminal as on the page (2026-10-02). Both now run
  one runner (`setup_run.py`) and reach their GO / NO-GO the same way. In the terminal,
  the steps after Agents printed the wrong numbers. An agent whose connect raised an
  error did not count as a failure. A data source that could not be indexed printed
  GO and then exited 1. The terminal asks every question first, then runs the steps
  with no one at the keyboard. A setup paused in a download exits 130 on the page as
  in the terminal, and the installers report it as paused, not as "finished with
  problems".

- The reader's ask is counted only once a reader takes it (2026-10-02). Varyz used to
  record the ask when `start_task` showed it, so an agent that passed over it held
  the reading back for a day. The prompt hook's first briefing in a session now
  carries the ask too, so every session gets it. It is recorded when the reader calls
  `profile_reading_task`.

- Re-running an installer updates an installed Varyz (2026-10-02). Where Varyz and
  its config are already installed, `install.ps1` and `install.sh` run
  `varyz update apply`. It backs up the database before a migration, checks that the
  new version starts and refreshes the connected agents. `uv tool install` only said
  Varyz was already installed. The update takes Varyz from where it was installed
  from.

- The shell installer hands Windows its own paths under Git Bash (2026-09-30).
  Run from a checkout in Git Bash, `install.sh` named the folder and the config as
  `/c/Users/...`, which the plan printed and `uv` and `varyz` (Windows programs) only
  understood through Git Bash's automatic path conversion. It now converts both with
  `cygpath`, so the plan shows what runs and nothing depends on that conversion. macOS
  and Linux have no `cygpath` and are unchanged. This was the one failing test on the
  Windows runner.

- `varyz install` no longer ends in a traceback when Codex's CLI is missing (2026-09-29).
  A Codex config with no `codex` command on PATH (an uninstalled Codex, or the app
  without its CLI) stopped the whole install with `RuntimeError: Codex CLI is not on
  PATH`. Codex is now skipped in one line that says to install its CLI and run
  `varyz install` again; the other clients still install. `varyz install --client codex`
  prints the same line and exits 1, like any install that could not finish.

- Rules read from your turns name the right place and keep to what lasts (2026-09-29).
  A Codex chat outside a project is kept in a folder named after its first words, and
  those words ("my", "hel", "help-me-tailor...") were filed as projects; a home or
  scratch folder, and a session's id, were too. Now only a project folder names a
  project, and the next sync files your stored turns again under that rule. Each item
  now says what it applies to: how you work with agents on any task, your taste in
  everything you build, or one project. The review page shows these as three kinds of
  group, and editing "Applies to" moves an item between them. The reader now sees each
  conversation whole, with the end of the agent's reply each message answered (kept
  beside your words, never mixed into them). Feedback on one piece of work ("spin it in
  every direction, not only left") is no rule, and neither is what held for the moment
  ("not right now", "for now", "this time"). A habit now needs two conversations: asking
  twice in one conversation is one piece of work. Taste in what you build that shows in
  one project only (thin fonts in one app) stays in that project. Every item is one
  complete sentence in the reader's words, never your message copied. Candidates queued by an older
  reading or by the word matcher are replaced by the next reading; rules you kept are
  untouched. With no model to read, `varyz rules mine` no longer falls back to matching
  words. It queues nothing and asks you to let your agent do the reading.

### Added

- News from varyzai.com/news reaches the people who use Varyz (2026-09-30). While Varyz
  is in use it checks `varyzai.com/news.json` once an hour in a detached process, and
  the `user-prompt` hook hands the agent each new post once, at most two at a time, to
  pass on in one line. It is on by default; `varyz news off` stops the check and the
  notices, `varyz news on` starts them again, and `varyz news check` checks now. The
  request carries nothing about the user or their archive. Posts are written in
  `docs/NEWS.md`, and `scripts/site/build_site.py` builds the page
  `website/news/index.html`, the feed `website/news.json` and an Atom feed
  `website/news.xml` for feed readers from it.

- An update found by an automatic check reaches the owner (2026-09-30). With
  `[update] mode = "notify"`, or `auto` waiting for `varyz update apply`, the result
  used to go only into `data/autosync/update.json`. The `user-prompt` hook now hands
  it to the agent, which tells the owner in one line, once per new version.

- A rule you correct is updated with your correction (2026-09-29). When an agent did
  something because one of your rules said so and you correct it, the agent now
  replaces that rule with the corrected one at once, without asking, and tells you in
  one line which rule it updated. `remember_owner_rule` takes `replaces` (the rule's id
  from the briefing); the new rule keeps the old one's place unless you moved it, and
  records what it corrected.

- Varyz installs without a package index and without typing commands (2026-09-29).
  `varyzai` was never published, so the installers install the repository they sit in
  and, copied anywhere else, `git+https://github.com/egrm07/varyz` (they stop with a
  plain reason when git is missing). The README names three ways in: ask your agent,
  double-click the launcher, or run the installer. docs/AGENT_SETUP.md has the steps an
  agent follows to install Varyz for its owner. The connected agents are told which
  Varyz command fits each request ("update Varyz", "review my rules") and run it for
  you, the page-opening ones in the background.
- `varyz update apply` updates an installed Varyz too (2026-09-29). A GitHub install is
  compared with GitHub's latest commit; a folder install with the folder, pulled first
  when it is a clone. It reinstalls with uv (keeping the DirectML build), backs the
  database up before a migration and checks the new version starts. It then refreshes
  every connected agent's Varyz files: skill, hooks, reader and MCP entry. The first
  agent session on a new version does the same, so no update needs `varyz connect`
  again. An automatic check never installs uncommitted work from a folder.

- `varyz rules mine` reads your turns for how you work, not one rule at a time
  (2026-09-29). The word matcher took "I want it to spin in all directions" for a rule;
  now a model reads each project's turns, oldest first, and notes what they show: the
  project's vision, what it must never do, your taste and how you want agents to work,
  what "done" means for a kind of work, and the order you take a kind of work in. A
  second pass joins the notes that say the same thing, and what holds in every project
  is joined once more across projects. Varyz checks every item before it is kept: each
  quote must be your own words from the message it names, and an item needs two
  messages or more behind it (one for a vision, or a rule you said would always hold).
  What passes is a candidate, never advice until you keep it. New turns are read as they
  arrive, their project joined again, and what you already keep is not suggested again.
  The cloud model you signed in to reads them by default; the local model
  (`[workflows] classifier_model`, `qwen3.5:4b` through Ollama) reads on this machine
  when you keep your turns here or the cloud cannot. Instruction files (`--from-file`) are still matched by words;
  `--lexical` or `[workflows] rule_miner = "lexical"` matches turns by words too, and so
  does a run no model can read for. A long prompt that opens the same way in several
  sessions (Claude Code's /doctor "Help me fix these issues") is skipped as pasted.
- The reading finds what you already decided, and why (2026-09-29): a project's
  lasting choices (its stack, where it runs and ships, the services it uses, names,
  brand, tone of copy) are read as a question and your answer. When you changed your
  mind, the latest answer counts, and a question `start_task` would open as a choice
  keeps its wording. A kept decision is an answer `ask` gives agents, not a rule, and it
  replaces your earlier answer to the same question. The reading also notes your reason
  where you gave one, shown to agents as the why; what you want to be asked about and
  what agents should decide alone; and the check that would have caught a fault you found after an
  agent said it was done. On the review page decisions sit under "Decided", and Edit
  changes the question and your answer.
- `varyz rules review` opens a page (2026-09-29): the candidates grouped by project
  (vision, never, how you work, done means, workflows), each with the words it came
  from, to keep or drop one at a time or a project at once. Edit on any item changes
  its wording, the project it applies to (or every project), and a workflow's name and
  steps; an edited item is kept as you wrote it. A kept workflow becomes your stated
  order. `--terminal` asks in the terminal instead.
- `varyz rules mine` reads with the cloud model you signed in to by default
  (2026-09-29): `[llm] provider` through the sign-in `varyz connect` made, at most
  `[workflows] rule_reader_cloud_max_calls` (30) calls a run. Only turns classified
  public or personal go, with credentials removed first, and the cloud keeps its own
  notes apart from the local model's, so nothing only the local model saw reaches it.
  `--no-cloud` keeps your turns on this machine and is remembered
  (`[workflows] rule_reader_cloud = false`); `--cloud` goes back. When the cloud cannot
  read (no sign-in, busy, unreachable), the local model takes over where the machine
  runs it: not under 2 GB of free disk. An invalid request stops the run.
- The agent you work in does the reading too, with no sign-in of varyz's own
  (2026-09-29): when turns wait to be read, `start_task` asks the agent, once, to spawn
  a background subagent on its cheapest model (Claude Code: Sonnet; Codex: gpt-6-luna).
  The subagent takes each task from the new `profile_reading_task` tool and gives what it
  found to `submit_profile_reading`, which runs the same checks and queues what passes
  for review. It gets the same turns as the cloud reader; only a registered agent gets
  them. A merge of three notes or more that comes back empty is handed out once more,
  since a cheap model sometimes empties the queue instead of doing the work. It asks again once 20 new turns wait, at most once a day; `[workflows]
  rule_reader_agent = false` turns it off. `varyz connect claude-code` installs the
  reader as an agent of its own, `varyz-reader`: Sonnet, the reading prompt, and only
  the two reading tools, so it can do nothing else. The setup page's last step says what
  happens: the connected agent's first use of varyz starts the reading, with nothing to
  paste.
- Setup finishes the job (2026-09-28): `varyz setup` has seven steps, and Varyz is fully
  working when it ends. After the data sources it builds the graph and compiles the
  personal model (step 5), turns on autosync at login (step 6, once a source is
  connected; there is no opt-out), and readies the local classifier (step 7). A config
  still on a semantic extractor is switched to `deterministic` in a build that holds the
  managed model back. Step 8 uses an Ollama already there when it is 0.17.5 or newer and
  otherwise installs Ollama 0.34.4 silently and without admin rights (into
  `~/.varyz/ollama` on macOS and Linux, with its own installer on Windows), starts it,
  and pulls `qwen3.5:4b`. An older Ollama keeps running; the new one serves on the next
  free port, `[workflows] classifier_url` is updated, and the daemon is restarted on it.
  Setup then asks macOS which folders it keeps from autosync and says how to allow them. Autosync compiles the personal
  model again after each graph update, and the classifier starts the recorded Ollama when
  it finds it down.
- macOS asks for a folder, not the disk (2026-09-28): autosync runs through a small
  Varyz app built on the Mac at install (an AppleScript applet, signed ad hoc, with a
  reason for the Documents, Desktop, Downloads and volume folders only), so macOS asks
  "Varyz would like to access files in your Documents folder" and Allow opens that
  folder alone. Setup says ahead that macOS may ask, and shows a permission panel only
  when macOS actually kept a folder, naming its source ("Documents needs one more
  permission"), opening Files and Folders, and offering to skip and run `varyz sync` by
  hand. Full Disk Access is offered only where the app cannot be built, and is described
  as opening more than those folders.
- Progress bars (2026-09-28): setup's long steps (indexing, embedding, the graph, the
  Ollama download and the model pull) show a bar on the page and in the terminal, filled
  as far as the step can say and sweeping while it cannot, such as a first model
  download. `varyz sync`, `varyz embed` and `varyz graph rebuild` show the same bar.
  Setup numbers the running step ("Step 6 of 8", `6/8` in the terminal), numbers the
  sources while indexing several, and says how long a measured phase has left ("about
  3 min left"), from that phase's own pace once it has run a few seconds. Reading a
  source is measured too: documents by the files to read, AI conversations by the bytes
  not yet read, git by repository and commit.
- Downloads pause and resume (2026-09-28): a download in setup (the search models,
  Ollama, the classifier's model) can be paused with **Pause download** on the page, or
  Ctrl+C in the terminal, and carries on from what has arrived, on the page with
  **Resume** or later with the same command. The search models (about 1.3 GB) are now
  fetched before indexing into a staging folder, resuming a partial file and checking
  each against the size and hash the Hugging Face Hub lists; they move into fastembed's
  cache only when whole. A Hub that cannot be reached leaves them to fastembed as before.
- Setup as a page (2026-09-28): `varyz setup` now runs its five steps as a page in the
  browser, styled like the Taste Card, where agents and data sources are ticked instead
  of answered y/n (AI conversations start ticked; git history and documents unticked).
  It asks in the terminal as before with `--terminal`, with `--yes`, `--dry-run`,
  `--source` or `--no-sources`, or where no browser can open (over SSH, or Linux without
  a display); `--web` serves the page even there. The page is served on 127.0.0.1 only,
  behind a one-time key in the printed link, and can approve only the sources it showed.
  GitHub is linked on the page as well, from the GitHub CLI's login or a pasted token:
  the token is checked with GitHub at once, never sent back to the page, and stored in
  the OS credential store only by a run with GitHub ticked. `scripts/Install
  Varyz.command` (macOS) and `scripts/Install Varyz.cmd` (Windows) are double-click
  installers that end on that page.
- Onboarding in one command (2026-09-27): `varyz setup` creates the archive (in
  `~/.varyz` by default), starts its daemon, connects the agents
  it finds (Claude Code, Codex, Cursor), offers the local data sources it detects, indexes
  what was approved and ends with the doctor's GO/NO-GO. It asks before each agent and
  source; `--yes` answers every question, `--source <id>` limits the offers, `--no-sources`
  skips them and `--dry-run` changes nothing. A re-run keeps what is already there. The
  installers now hand the whole first run to it.
- Which data sources are connected: `varyz sources` lists every alpha source as
  connected, paused, not approved or not connected, with its item count, last sync and
  how to connect it. Setup ends with the same table, `varyz connect` says what it
  connected and that it is indexing now, and `varyz hooks doctor` has a data-sources
  line, so a GO no longer hides an archive that reads nothing.
- The daemon names its archive (`GET /v1/identity`). A client refuses a daemon that
  serves another archive, and `setup`/`init` move a new config off a port that is already
  taken, so two archives on one machine can no longer read and write each other's rules.
- No `--config` after setup: every command now falls back to `~/.varyz/config.toml` (or
  `$VARYZ_HOME/config.toml`) when neither `--config`, `$VARYZ_CONFIG` nor a `config.toml`
  in the current folder names an archive, and setup's next steps no longer name it.

- Updates: `[update] mode = "auto"` keeps a git checkout on the latest commit of the
  branch it tracks, checked once a day on the first use of the day; `"notify"` only
  says an update is there. Only fast-forwards are applied, the database is backed up
  before an update that migrates it, and new code that fails to import is rolled back.
  On Windows, an update with new dependencies waits for `varyz update apply`. `varyz
  update status/check/apply [--dry-run]/pause/resume`; every attempt is appended to
  `data/autosync/updates.jsonl`; `autosync doctor` shows the last result. Off by
  default.
- macOS: the autosync launchd agent starts at install rather than at the next login,
  runs as a background process with Homebrew on its `PATH`, and is unloaded on
  uninstall. The memory floor for the local model now reads `vm_stat`: macOS has no
  `SC_AVPHYS_PAGES`, so the guard had never run on a Mac. Autosync checks, from its own
  process, which configured folders macOS privacy blocks, and `autosync doctor` names
  the program that needs Full Disk Access.
- Decisions made in any session: autosync records the option the owner picks in a
  Claude Code question (AskUserQuestion) as that project's user-confirmed decision. A
  newer pick supersedes the older answer, an older session never overrides a newer one,
  and a retracted pick is never recorded again. It reads only each transcript's new
  lines. `varyz knowledge capture [--apply]` previews it; `[autosync]
  capture_decisions` switches it off.
- Workflows: the order the owner guides agents through a kind of work (a website:
  colour, logo, page content; a product: vision, design, monetization) and how they
  guide each step. `start_task` returns `owner_workflows` with settled steps and the
  task's step. A task that starts at a later step while an earlier one is open is
  flagged once, never blocked, and `finish_check` lists `workflow_steps_open`. MCP
  `remember_owner_workflow` records one after the owner agrees; `varyz workflows
  list|add|retract|learn` manage them. Autosync learns orders from the owner's own
  AI-conversation turns and uses them straight away, marked learned. Corrections and
  regrets outrank the order things came up, so a slip is not learned. Migration 0039.
- Workflow nudges are sized and answered. Only a task that starts new work at a
  later step is raised; a small change ("fix", "tweak", "rename") is just done. Agents
  record the owner's answer (`record_workflow_nudge`: took, skipped, or rework after a
  skip). A skip that led to rework is the reason the next nudge gives. A step skipped
  3 times with no rework is put to the owner once (`answer_workflow_review`: keep,
  optional, drop); varyz never changes an order without that answer. Optional steps
  stay in the order but are never raised. `varyz workflows review` shows the counts
  and records a decision. `finish_check` adds `workflow_reviews_due` and
  `workflow_reworks`. Migration 0041.
- Workflow tasks are sized by a local classifier instead of a verb list. A local
  Ollama model (`qwen3.5:4b`, eight authored examples, one letter read from its
  log-probabilities) decides new work vs small change: 68/69 on a held-out set against
  42/69 for the verb list. It falls back to the local embedding (62/69), then the verb
  list, when Ollama is down, the model is missing, or loading it would leave the GPU
  under 30% free. Loopback only. `varyz hooks doctor` says which one answers now.
  `[workflows]` in the config;
  `scripts/task_size_bench.py`; `reports/TASK_SIZE_CLASSIFIER_2026-09-25.md`.
- The same local model answers five more closed questions. Each falls back to the
  rule it replaced. Measured against that rule on data it was not tuned on
  (`reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md`,
  `scripts/classifier_rollout_bench.py`):
  - `ask`: when no stored owner answer matches a question word for word or by
    strict paraphrase, the model checks the three nearest of the owner's own
    decisions. It answers only on a single yes at P >= 0.9, never for a decision that
    names another project, and at confidence 0.7. On a fresh held-out set, recall
    rose from 13% to 72% with 0 wrong answers. It takes about 585 ms, and only when
    strict matching finds nothing.
  - The prompt hook tells work from a question with the model: 86.7% on 188 real
    prompts against 64.9% for the wording rule, and 93.8% against 75.0% held out. It
    never waits for a model load. A cold model is warmed in the background, also at
    session start, and the rule answers meanwhile.
  - The done gate skips a small change (`[agent] done_gate_small_changes = "skip"`),
    but only when the model is already loaded and calls it small. It skipped 9 of
    21 small changes and 0 of 21 new tasks.
  - Rework (`is_redo`): 92.3% against 73.1% for the verb list. It is asked only when
    an open skip could count it.
  - The scope default for an owner answer: general only when the model is at least
    90% sure, otherwise this project. 78.3% against 66.7%, with no one-off stored as
    general (not significant, p = 0.065).
  - `[workflows]` `redo_classifier`, `prompt_classifier`, `scope_classifier`,
    `paraphrase_verifier`.
  - The model now stays loaded for 10 minutes after its last answer, up from 5
    (`classifier_keep_alive`). On the owner's prompt gaps it is already loaded for
    75% of prompts against 62%, for about 4.2 GB-hours of VRAM a day against 2.8.
- A workflow step is put up for review after 2 skips within 7 days (was 3 at any
  time). Older skips expire and the step stays. `varyz workflows review` shows the
  skips this week.
- Migration 0040 adds an empty `workflow_turn_labels` table. Nothing writes it in this
  release: the step labeller it was made for learned nothing measurable on the owner's
  archive and is parked (`reports/VARYZ_SHIP_2026-09-25.md`).
- Claude Code prompt history: sessions whose transcripts are gone are ingested from
  `~/.claude/history.jsonl` (the user's prompts only; pasted contents, slash commands
  and shell escapes are dropped).
- One owner-resolution primitive: `resolve_for_owner(question, caller, purpose)`.
- Append-only explicit owner knowledge with supersession, device stamps and monotonic
  ordering.
- Durable owner escalations; answers become `USER_CONFIRMED` tier-1 knowledge.
- Authenticated loopback daemon used by the CLI, MCP server and local owner inbox.
- Named agent principals, hashed and revocable tokens, bounded capabilities and a
  low-trust fallback for unregistered callers.
- Writable MCP `resolve`, plus retained `recall`, `brief`, `lookup` and `interpret`.
- Idempotent Codex, Cursor and Claude Code connection with the Varyz routing skill.
- Incremental personal-model compilation by recent days and hot resolution topics.
- Trusted Resolution Rate fixtures, failure taxonomy and one-use held-out ledger.
- Agent relay for owner questions: an escalated `resolve` carries an `ask_owner` block,
  the agent asks with its own question UI (AskUserQuestion in Claude Code, chat in
  Codex), and MCP `answer_owner_question` records the reply as `USER_CONFIRMED`
  tier-1 knowledge with `agent_relay:<agent>` provenance; `owner_questions` lists
  what is still pending, and `varyz knowledge list` now shows each answer's source.
- Varyz as an advisor to coding agents: MCP `advise` returns the owner's confirmed
  rules and project vision for a plan, plus related owner decisions. `context` now
  carries `owner_rules`. After the owner agrees, `remember_owner_rule` records a
  correction as a standing rule. The ledger is append-only (migration 0036), and
  `varyz rules list|add|retract|review|mine|stats|advise` manage it.
  `rules mine` suggests rules from the owner's own turns and instruction files for
  review, and never feeds agents directly. `scripts/advice_bench.py` measures it.
- The owner workflow: brief → ask → check. MCP `start_task` returns how the owner
  always works, the project's vision, rules and one-off choices, related general
  preferences, the definition of done for each kind of work the task involves, and the
  choices to ask about. `ask` answers from what the owner said before, or relays an
  `ask_owner` block with a scope question; `finish_check` returns the checklist to
  verify before reporting done. `advise` and `resolve` remain as older names.
- Scoped owner knowledge (migration 0037): every answer and rule is general (`*`) or one
  project's choice; one-off choices never reach another project. The owner picks the
  scope when answering (`answer_owner_question scope`, `varyz answer --scope`,
  `varyz know --project`); otherwise varyz guesses and fails narrow. Pending questions
  are per project.
- Seams for answers that are not only the local owner's (migration 0038, 2026-09-24),
  added before the first public install. Existing rows default to the local owner.
  No network code.
  - Owner knowledge carries `author_id` (default `owner:local`) and `origin`
    (`own`, `org` or `published`).
  - Tier 1 recognises `org:<id>` and `published:<id>` scopes below the owner's own
    answers. The order is own project, own general `*`, org, published, so the
    owner's answer always wins and a project's answer still never answers for
    another project.
  - An answer that is not the owner's own resolves as `reported`. MCP `resolve`/`ask`
    add `answered_by` (knowledge id, author, origin, scope), with a label for org and
    published answers.
  - Append-only `resolution_outcomes` (`adopted`, `overridden`, `corrected`) are
    device-stamped and sequenced. Superseding an answer records `overridden` for
    every resolution it answered, in the same transaction.
  - Escalations carry `recipient_id` (default `owner:local`). The same question for
    two recipients is two questions, and the answer's author is the recipient.
    `owner_questions` relays only the owner's own questions. The notifier still
    receives only the id and urgency.
- Definitions of done: rules carry a kind of work (`website`, `python-tool`,
  `mobile-app`, `docs`, `evaluation`, `automation`, `ai-integration`, `data-connector`,
  `dashboard`, `release`), detected from the task; `varyz rules checklist`,
  `varyz rules add --domain`.
- Briefing hooks for Claude Code and Codex, installed by `varyz connect` next to the
  client's other hooks (`varyz.hooks`): `SessionStart` starts the daemon if down,
  `UserPromptSubmit` injects the briefing with the first task and new kinds of work
  later, `Stop` blocks once per task after edits with the finish checklist. Fail-open;
  `[agent] briefing_hooks` / `done_gate` turn them off; `varyz uninstall` removes them.
- `varyz hooks doctor` (go/no-go preflight for the workflow) and `varyz hooks
  pause|resume` (kill switch: a `PAUSED` sentinel every hook honours).
- The prompt hook skips plain questions and client-written notices; a request phrased
  as a question ("can you add a pricing page?") is still briefed. In a prompt that
  mixes a request with questions, only the request decides which definitions of done
  apply.
- `varyz task` previews the briefing an agent gets; `varyz dismiss` closes owner
  questions without teaching anything; `varyz questions` shows each question's project.
- `scripts/workflow_bench.py`: scope, kind-of-work, checklist and interruption
  measurements on a labelled fixture (`reports/VARYZ_WORKFLOW_2026-09-24.md`).
- Publication audit covering archive quotations, account identifiers, private-key
  markers and machine-specific paths.
- Owner retraction of explicit knowledge: append-only `owner_knowledge_retractions`
  (migration 0035), `varyz knowledge list|retract`, and daemon `GET /v1/knowledge` and
  `POST /v1/knowledge/{id}/retract`.
- MCP `resolve` starts a down loopback daemon once and retries (`resolution.autostart`).
- Vision-conformance benchmark `scripts/vision_bench.py` with one-shot held-out ledgers
  (`reports/VARYZ_VISION_BENCHMARK_2026-09-24.md`).

- `varyz wrapped` (2026-09-25) makes a shareable "My Way" card. It comes as SVG and PNG, in
  square 1080×1080 and story 1080×1920, and is built only from your own confirmed rules and
  answers.
  - General rules only by default. A project rule appears only when named with
    `--include-project <id>`.
  - A preview lists every line with its rule id before anything is written. `--exclude` and
    `--only` pick rules, and `--dry-run` writes nothing.
  - The archetype is one of 29 titles ("The Gatekeeper", "The Paranoid Shipper", "The
    Clockmaker", …): seven for a single leading streak, one for each of the 21 pairs of
    streaks running level, and one fallback. It is computed from fixed word lists, with no model and no randomness,
    and the preview explains the choice.
  - Long rules are shortened with a visible ellipsis, never reworded. An answer that only
    makes sense next to its question is left off rather than printed as a question.
  - `--handle @you` puts your handle in the top-right corner.
  - Set in Source Serif 4 and IBM Plex Sans, the website's fonts. The PNG uses small
    bundled copies (OFL 1.1, Latin and Latin Extended, renamed as "Plex" is a reserved
    name), so it looks the same on every machine. `scripts/build_card_fonts.py` rebuilds
    them.
  - It reads the database read-only and makes no network calls.
  - Pillow is now a declared dependency. It was already installed through fastembed.

### Changed

- Cloud pages share Varyz's Taste Card/setup/review design (2026-10-03): the existing
  night/cream/gold palette, bird mark, Source Serif 4 and IBM Plex Sans, regular-weight
  type and outlined panels. Signup, sign-in, recovery, consent, account review and
  the checkout wrapper use the shared styling. Fonts are served from an allowlist
  of bundled assets; CSP permits only same-origin fonts. Desktop/mobile layouts
  and account/security regressions were checked before deployment.

- Documents stay local (2026-10-01). Varyz never sends an entry from the `documents`
  source to a cloud model for its own processing (enrichment, graph extraction,
  personal-model annotation), whatever its sensitivity label: the egress policy returns
  `local_only` with reason `local_only_source`, and an episode holding a document is
  annotated on the machine. Learning how the owner works reads only their AI
  conversations. An agent that looks something up (`answer_question`) still receives
  the excerpt it asked for. `docs/PRIVACY.md` and the README say so.

- The docs no longer rule out accounts (2026-09-30). Accounts are next: an account ties
  a person to the data they sync to it, private to them unless they publish it
  themselves. `docs/PRIVACY.md` now names the news check and the plan for accounts.

- A folder's version is not a project of its own (2026-09-29). What you told an agent in
  SAN7.14 now holds in SAN7.15: a trailing version (`san7.14`, `shop-v2`) is dropped from
  the project's name, for rules, answers and workflows already kept as well as new ones,
  and the rule reading joins the versions' turns into one project.

- `ask` answers from the archive the way MCP `recall` does when a local model is there to
  judge: keyword and vector search, the cross-encoder's shortlist and the model's picks
  from it, instead of keyword search alone. Records the model picked skip the word-coverage
  check; the evidence verifier still reads them. Without a managed model, `[retrieval]
  judge = "auto"` now uses the `[workflows]` classifier model through Ollama on a GPU (MCP
  `recall` too). The daemon starts loading the three models in the background on the first
  question and lets them go after five idle minutes; while they load (1.4-2.9 s), or if the
  model fails, questions get the keyword answer rather than wait. recall_bench tier 2, questions answered with every needed record
  (qwen3.5:4b, RTX 5070), scored once each: held-out 11 to 19 of 27, sealed 9 to 15,
  sealed2 6 to 15; no question the old path answered was lost (sign test p = 0.004, 0.016,
  0.002). Wrong answers went 4 to 4, 5 to 7 and 5 to 6; no sensitive record leaked; TRR dev
  6/8 with nothing fabricated. It costs time: p50 1.7-1.9 s and p95 2.5-2.6 s per
  recall_bench question, against 5-150 ms. Agents ask longer questions than the benchmark
  does, and on the processor the owner's took a median 5.7 s (next entry but one).
- The daemon answers agents at the same time. It served one request at a time, so four
  agents asking at once waited 8.1 s for the last answer and a status check sent meanwhile
  waited 7.8 s, past the hooks' 2 s timeout, which then reported the daemon as down. Tier 2
  now reads through a read-only connection of its own per question, and everything else
  (the other tiers, the other endpoints, the ledger) takes turns on the shared one. With the
  models loaded, on the recall_bench dev questions: two at once 4.1 to 3.2 s, four at once
  8.1 to 5.7 s, the status check 7.8 s to under 10 ms; the 27 questions cite the same
  records asked four at once as one at a time. The local model still answers one call at a
  time (Ollama's `OLLAMA_NUM_PARALLEL`, 1 by default), so four at once is not four times
  faster.
- The cross-encoder runs on an NVIDIA card. Agents ask in sentences (a median 175
  characters in the owner's transcripts, against 50 on recall_bench), and on the processor
  it spent 3.8 s of a median 5.7 s `ask`. It now runs through DirectML when onnxruntime can
  reach the card: `install.ps1` adds `onnxruntime-directml` on Windows when `nvidia-smi` is
  there. The owner's 56 distinct `ask` questions, replayed against a copy of their archive
  on an RTX 5070, CPU then GPU: median 5.7 to 2.7 s and p95 11.5 to 4.3 s; the 39 that
  reached the cross-encoder in both runs went 7.2 to 3.3 s (the cross-encoder alone, 5.0 s
  to 0.7 s). The 149 cross-encoder calls scored the same to four places and ranked the
  same, and all 56 answers came out the same. The session takes up to 1.8 GB of VRAM
  (1,789 MiB measured), so it goes on the card only when 30% of the card stays free after
  it, and a card that will not open leaves it on the processor. On the card questions take
  turns; on the processor they still read side by side. Linux (`onnxruntime-gpu`) and AMD
  or Intel cards stay on the processor.
- The classifier now asks Ollama for an 8k window, the same as the judge. Ollama reloads a
  model whose window changes, and the two had reloaded qwen3.5:4b at every switch (1,194
  loads in a day's server log). qwen3.5:4b takes 204 MiB more VRAM (3,187 MiB).
- AI conversations are no longer sensitive by source (owner decision, 2026-09-27). Every
  turn to a coding agent was held back, so agents never saw the owner's own words, owner
  questions whose best match was a turn were withheld, and `rules mine` found nothing in
  2,542 turns. Turns are now personal: secrets are still redacted on the way out, and the
  content patterns still hold back a turn that names a private topic. `varyz privacy
  release [--source ...] [--apply]` re-labels what an older rule locked away, lowering
  only what today's rules no longer catch, and records each release as an event.
- One report, addressed to the owner. Every varyz text told the agent to "say" how it
  verified each item, so agents wrote the whole exchange with varyz into the chat, and
  the `Stop` hook, which runs after the report is already on screen, made them write it
  a second time. The briefing, `finish_check`, the MCP instructions and the skill now
  say varyz talks to the agent, not the user, and the report names only what is not
  done, not applicable or still open. The `Stop` hook lets the turn end when the agent
  called `finish_check` after its last edit, and when it does send the agent back, tells
  it not to repeat the report: a few lines at most under it.

- The MCP server reads and writes UTF-8 on stdio. It used the Windows code page
  (cp1252), so an em dash from an agent was stored as `â€”`. Rows already stored keep
  their text; the ledgers are append-only.

- Renamed from Varys to Varyz (2026-09-24). The command, module, MCP server key, routing
  skill, hook launchers, `VARYZ_*` variables, credential-store service, `~/.varyz` and
  `varyz.db` all take the new name. The distribution is `varyzai`, and the source is
  github.com/egrm07/varyz. An existing install carries across without steps:
  - `VARYS_*` variables are still read.
  - Keys under the old credential-store service move on first read.
  - `varys.db` becomes `varyz.db` once no process holds it open.
  - `~/.varys/oauth` moves to `~/.varyz/oauth`.
  - The installers reuse an existing `~/.varys/config.toml`.
  - `varyz install`/`connect` replaces the old `varys` client entry for the same archive,
    keeping its agent token. It also replaces the old hooks and routing skill, and backs
    the skill up to `skills.varyz-backup/`.
  - `varyz autosync install` replaces the old login registration, and `varyz doctor`
    reports one that is still in place.
  - Entries that name a different archive are left alone.
- Resolution tier 2 uses abstaining `recall`; a withheld sensitive match cannot be
  replaced by unrelated lower-ranked context. When the caller allows escalation, it goes
  to the owner with its withheld marker instead of dead-ending as `NOT_KNOWN`.
- Tier-1 owner knowledge enforces caller capabilities. Revoked, denied and
  `owner_knowledge`-less callers get a terminal withheld result. Answers longer than the
  caller's `max_chars` are withheld whole.
- Tier-1 paraphrase matching requires every salient query term to be explained, keeps
  named-project scope, and never answers why/when from a recorded choice. Framing words,
  a few abbreviations ("docs", "time zone", "command-line tool") and the unchosen option
  of an "X or Y" question do not block a match. The result is zero wrong owner answers on
  235 held-out cases, at lower recall than the old partial matcher.
- Conversation compilation rejects known role-tag, exact-JSON and multilingual
  instruction-injection shapes before model inference (`window-v2`).
- Semantic-facet prompt examples are synthetic (`semantic-facets-v9`); private prompt
  examples were removed from the distributable source.
- Clean-machine CI now proves the installed `resolve` path through the daemon instead of
  exercising the retired `understand` command.
- Website and privacy notice (2026-09-24): open source leads instead of "free". No line
  promises what an optional online feature would later break. The app "needs no account …
  to run", what you teach it "stays on your computer unless you share it", and "no
  telemetry" is scoped to the software. The FAQ promises that nobody can pay to put an
  answer in front of your agents, and it gains "Is it really open source?". The licence
  page, footer and install note lose rename leftovers.
- Website fonts (2026-09-25): Source Serif 4 for headlines and IBM Plex Sans for body
  text, self-hosted as before. They replace Newsreader and Inter on every page and the
  social image.

### Fixed

- The briefing hooks of a `uv tool install` did nothing (2026-09-29). Their launchers
  named the interpreter behind the tool's environment, which has no Varyz, so every
  hook failed open in silence. They now name the environment's own `python`, and the
  next update or agent session rewrites them.
- Autosync never started from a `uv tool install` (2026-09-28): the login job ran
  `uv run` in the config's folder, which found a Python without Varyz and exited with
  "No module named 'varyz'". It now runs the tool's own Python; `uv run` is kept for a
  source checkout's `.venv`, pointed at that checkout.
- Documents indexed nothing from a folder holding a `$RECYCLE.BIN` directory (left by a
  Windows drive): the name's `.bin` ending marked the whole folder as model weights. Only
  files count now.
- `ask` answered off-topic questions from a large archive with an unrelated record at
  about 0.015 confidence (the rank score of whatever came first). Tier 2 now answers only
  when at least one returned record holds half of the question's words, names counted
  double, within 25 words; otherwise the question goes to the owner. On the benchmarks no
  correct tier-2 answer is lost; recall_bench record recall drops (dev 0.400 to 0.375,
  held-out 0.325 to 0.25) only on questions that were already answered wrongly.
- `ask` still answered a question about something the archive never mentions ("How much
  did I pay for the kayak?") from a record that shared its other words ("pay", "much"),
  because a word no record uses cannot count against any record. Before tier 2 answers,
  the local classifier model now reads the top three records against the question, and
  none about the thing asked sends it to the owner (`[workflows] evidence_verifier`;
  without Ollama, the coverage check decides alone). recall_bench, tier 2 alone:
  off-topic answers to unanswerable questions held-out 2/5 to 0/5, wrong answers
  held-out 11 to 4 and sealed 11 to 5, right answers held-out 9 to 11 and sealed 9 to 9;
  TRR unchanged. It costs about 0.2 s per answered question with the model loaded
  (0.5 s when it reads all three).
- `varyz setup` ended with "GO: agents are briefed from this archive" when it had connected
  no agent. It now says the archive is ready and that no agent is connected yet, and
  `varyz sources` points Git history and Documents at `varyz setup --source git` /
  `--source documents` instead of a bare `varyz setup`. The first model download no
  longer prints the Hugging Face Hub's "set a HF_TOKEN" notice.
- The clean-machine smoke failed on any machine already running a varyz daemon: `init`
  gives a new archive its own port when the default one is taken, and the smoke refused
  a config that already had a `[resolution]` section. It now sets its own free port
  either way.
- Git history read only the approved root when that folder was itself a repository, so a
  projects folder under version control hid every project inside it (13 commits indexed
  instead of 2,601 on the owner's machine). The root is now read and then searched like
  any other folder.
- `varyz rules mine --from-file` read instruction files line by line, so a hard-wrapped
  rule was queued without the words past the wrap, and a lead-in ending in `:` was queued
  as a rule. It now reads whole paragraphs and list items, splits sentences that end
  inside `**emphasis**`, and a folder no longer pulls in markdown under `node_modules`,
  `vendor`, virtual environments or hidden folders (1,080 files to 2 in one app).
- `varyz rules mine` suggested questions typed without a "?" ("What do i need to do"),
  counted "I need to" twice, and suggested instructions meant only for now ("Stop
  everything for now", "not yet"). They no longer count as rules: 90 suggestions from the
  owner's 2,542 turns became 63, with no loss among the standing rules.
- `varyz sources` showed the last sync in UTC with no zone; it is now the local time.
- The install one-liners ran `varyz setup`, which had not been a command since the
  pivot, so every scripted install failed after `uv tool install`. Under `curl | sh` the
  script now asks its questions on the terminal and never hangs without one.

- `ask` matches a decision recorded under the old name: "Varys" and "Varyz" are one
  word again in paraphrase matching (the rename had left the group as "varyz varyz"),
  so "…the Varyz website…" finds the colour answer recorded as "…the Varys website…".
- Autosync no longer stops for good after a code edit. The daemon waits until the
  source has stopped changing for 6 polls before handing off, stays until the
  successor heartbeats, and relaunches with backoff (60 s, doubling to 10 minutes)
  until one does or autosync is disabled. The runner sends stderr to a per-launch file
  and appends it to the launch log afterwards: a `2>>` on the shared log held it open,
  so the successor's own `>>` failed and cmd skipped it. The runner is no longer
  rewritten while a daemon is running from it.
- The resolution daemon (`varyz daemon run`) stops once its source has changed and
  settled, and the next MCP call starts one on the new code. Before, it answered from
  the code it imported at launch until someone killed it.
- A product named as the subject of the work no longer adds its checklist: "a
  landing page for my CLI" is website work, "a README for the CLI" docs work. A domain
  named only after "for", "about" or "showcasing" counts when nothing else names a
  domain. Held-out tasks: 2/12 → 10/12 detected exactly, 11 → 2 extra domains, no
  domain lost.
- AI conversations: a prompt a program typed inside a session the owner typed (a task
  notification, a scheduled check-in; `turnOrigin` "sdk") is not stored as the owner's
  words (authorship rules v3; 3 stored turns withdrawn on first run).
- `varyz hooks doctor` fails only on hook errors newer than the installed code; older
  ones are reported. Hook error lines are timestamped.
- CLI output escapes Rich markup in owner text, paths and errors, so a `[...]` in them
  is printed rather than read as a style.
- 2026-09-26: MCP `context` called without `dimensions` (optional in its schema) no
  longer fails with "dimensions must be a list"; it returns the task evidence alone.
- 2026-09-26: `varyz connect codex` and `uninstall` read the Codex config where the
  Codex CLI writes it: `$CODEX_HOME/config.toml` when `CODEX_HOME` is set. A Codex
  config saved with a UTF-8 byte-order mark is read instead of skipped as invalid TOML.
- 2026-09-26: a CLI command that needs the resolution daemon (`knowledge list`, `know`,
  `questions`, `answer`, …) says "The resolution daemon is not running. Start it with
  `varyz daemon start`." when it is stopped, instead of printing a urllib traceback.
- 2026-09-26: `ask` in a project treats that project's name in the question as
  context, not subject. "Which accent colour should the lumen page use?" now finds the
  owner's lumen decision (it missed, because the decision never repeats "lumen"), and
  "Which analytics should lumen use?" finds the general "Which analytics?" answer. The
  archive tier no longer counts the project's name, or "the lumen page/site/app", as
  proof the archive knows the subject: that let an off-topic note about the project
  come back as an answer instead of a question to the owner.
- 2026-09-26: two test fixture sentences (`behavior_search_dev.json` d3,
  `semantic_facets_dev.json` speculation case) quoted the owner's archive word for
  word; they are reworded with the same meaning, and the publication gate passes.

### Removed

- 2026-09-29: the starter rules. Varyz ships no rules of its own: `varyz rules seed`,
  `standards_seed.toml` and setup's Rules step are gone, so setup has seven steps. Your
  rules are the ones you keep in `varyz rules review`, add with `varyz rules add` or
  confirm when an agent offers to remember a correction. The doctor's definitions-of-done
  line no longer asks for a seed; it only counts. Rules an earlier setup seeded stay
  until you withdraw them with `varyz rules retract`.
- 2026-09-28: escalation notifications. On macOS the daemon showed a Notification
  Centre banner saying an agent was waiting on a question when none was, so the desktop
  banner, the terminal bell, the "owner questions are waiting" line the CLI printed and
  the local-UI badge file are gone, along with `varyz.resolution.notify`. Waiting
  questions are still queued: agents relay them through `owner_questions`, and
  `varyz questions` lists them.
- 2026-09-28: the managed local model (gemma, 7.4 GB) and the semantic graph it builds
  are held back from the alpha. `varyz.llm.local_install`, which holds the list of
  downloads and the one call that fetches them, is left out of the sdist and wheel, so an
  installed `varyzai` has no `varyz local-model install` and never downloads the model or
  its llama.cpp runtime. Nothing in the alpha read the graph: in the owner's own install
  it held 0 claims and the personal model had 0 generations, and 0 of 15 `ask` calls
  reached tier 3. `recall`'s judge keeps the measured 19/27 held-out recall, which was
  `qwen3.5:4b` through Ollama, not gemma. Where the model is missing, messages say this
  build does not install it instead of naming the missing command. The alpha builds the
  deterministic graph by default, which needs no model (on a copy of the owner's archive:
  99 claims from 5,143 entries in 14 s, mostly phrases such as "promised to be gone for
  the day"), and refuses the semantic extractor by name, the cloud (`hybrid`) provider
  included. The personal model stays: its hints name `varyz model compile --literal-only
  --run`, which compiles the explicit claims without a model (on the same copy: 25
  preferences, 2 facts and 3 people), and `varyz model correct` recompiles that way
  instead of refusing every correction for want of the model. The code stays in the
  source tree, where the command still registers and nothing changes.
- 2026-09-27: `varyz enrich` is held back from the alpha. It sent chat-days to a cloud
  model; its module is left out of the sdist and wheel, and the command registers only
  in the source tree. `[llm].enrich_model` still picks the model for graph and model
  extraction.
- Mirror as a generated prose product and its benchmark.
- 2026-09-26: the Mirror code the pivot left behind — `varyz mirror …`, the unregistered
  `setup`, `sources` and `bench` commands, the Personal Insight gate (`insight.py`), the
  inbox's Mirror, setup and importing pages, and the MCP Mirror filters. None of it was
  reachable from a registered command. Migrations `0011` and `0014`–`0018` stay. The alpha
  ships no web UI and no Mirror: it is the CLI and the MCP server.
- 2026-09-26: the local web inbox. `varyz serve` is no longer a command,
  `src/varyz/ui/` and its tests are gone, and `jinja2` is no longer a dependency. Owner
  questions are answered with `varyz questions`, `answer` and `dismiss`, or through the
  agent's `answer_owner_question`.
- RAG `ask` and `understand` agent surfaces.
- Uncopied WhatsApp, Telegram-export and Instagram-export connector surfaces.
- 2026-09-27: Gmail, Google Workspace (Calendar, Drive), calendar-file (`.ics`) and
  personal-archive connectors are left out of the sdist and wheel
  (`source-exclude`). Their modules stay in the source tree and still register there;
  an installed `varyzai` has no `varyz connect gmail` or `varyz connect calendar`, and
  `icalendar` and `imapclient` are dev dependencies only. `strip_html` moved to
  `connectors/documents.py`, which no longer imports the Gmail module.
- 2026-09-27: reading chat apps, contacts and browsing history, on the owner's decision
  that it is outside varyz's scope. The browser-history connector and the Google Contacts
  connector (with its OAuth scope) are deleted, as are the dead `connect whatsapp`,
  `telegram` and `instagram` functions, the Telegram export probe in setup and the chat
  checks in the autosync doctor. `[conversations] sources` defaults to
  `["ai_conversations"]`, and a chat app named there is ignored. Rows an older archive
  still holds from these sources stay sensitive. The repo archive's 47,433 WhatsApp and
  Telegram entries were deleted, with the 9,276 claims and 6,645 entities built only from
  them and 47,433 vault files; no personal-model generation, rule or owner answer drew on
  them.

### Known limitations

- The alpha is single-device and loopback-only. Phone relay, push, accounts,
  multi-device encrypted sync, hosted APIs and billing are post-alpha work.
- The sealed 2026-09-19 TRR score remains immutable. Its sensitive-boundary failure is
  fixed by a new regression and fresh resolver output, not by rewriting that score.
- Local model evaluation is synthetic and small. Gemma remains the conservative default;
  Qwen and SemIf speed paths are not promoted.

## 2026-10-15 Plugin 0.1.13 hosted-data safeguards

- State hosted prohibited-data limits before tool calls and respect withheld records.
- Preserve existing-installation onboarding and native OAuth; retain five positive and three negative review cases.
- Clarify that no hosted special-category-data process is currently supported; align privacy and compliance documentation.

## 2026-10-15 Archive package generation

- Regenerate the separate read-only archive draft after the hosted plugin version and data-limit guidance update.
- Restore the archive export reproducibility check without changing the submitted main plugin package.

## 2026-10-15 Windows release acceptance

- Retain the exact unsigned Windows candidate with provenance and checksums.
- Complete clean and returning-user onboarding using only an isolated fictional document.
- Verify configured reinstall, uninstall/reinstall, and preserved synthetic preferences through the installed CLI.
