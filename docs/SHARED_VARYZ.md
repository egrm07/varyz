# Shared Varyz

**An owner publishes selected confirmed project preferences to named recipients;
their AI consults that publication and asks the owner missing decisions. A creator
can also publish the standards they actually apply for anyone to follow by
username (Public Varyz, below).**

Implementation date: 2026-10-09. The feature is disabled by default. Pilot access
can be limited to verified account usernames. No pricing or license changes are
part of this implementation.

## Use it

Fastest: tell your agent "add egr to this project at Decide". It calls the
`collaboration` tool with action `add` (or runs
`varyz cloud share add egr --level decide`), which shares this project's confirmed
preferences (never General ones) on first use and invites that Varyz username.
egr's agent sees the invitation in `shared_projects`, asks egr once and joins with
`shared_join`; the returned link is a fallback. Adding a current member changes
their level; `remove` (or `varyz cloud share remove egr`) ends access or cancels
the invitation, and `varyz cloud share status` lists members and invitations.

The person you share with needs a Varyz Cloud account: a free email sign-in at
cloud.varyzai.com/signup, no subscription. Sharing to an email without one stops
with `no_account` and a line asking them to sign up first, then share again.

From the dashboard:

1. Sign in to Cloud, open a project and choose **Share selected project preferences**.
2. Name the collaboration, enter the recipient's account email, pick their level (see
   below) and select individual confirmed preferences. General preferences require
   individual selection.
3. Review the exact guest-facing text, then publish. Varyz tells the recipient by
   email and desktop notification; the invitation link is a fallback.
4. The recipient accepts from their agent or with **Join** under **Shared with
   me**. Guest access requires no subscription.
5. Follow **Connect your AI**, then copy the named setup request into a fresh chat.
   The AI discovers the collaboration by name. **Ready to use** appears only after
   a live shared connection reads every page of the current publication. A changed
   publication requires a fresh read. Technical details are under a collapsed panel.
6. Questions appear under **Questions**. Reply once, or independently select
   **Remember for this project** and **Add to shared preferences**. Both start
   unchecked. Recent sign-in expiry preserves the encrypted draft for ten minutes.

Private history, rationale and unselected records are excluded. Changed private
records remain pending until the owner reviews and publishes. Deletion, loss of
eligibility and scope changes suppress affected items in the sync transaction.
The owner can remove an explicitly shared answer without changing private knowledge.

## Public Varyz: follow someone's standards by username (2026-10-11)

"Use my Varyz", not "look what Varyz learned about me". A creator publishes the
standards they actually apply to one kind of work (code review, writing, design)
and anyone's AI can use them on their next task. The public page is
`cloud.varyzai.com/@username`.

### Creator: three steps

1. **Account → Public Varyz** (`/account/public`) suggests topics from the
   creator's General rules ("Code review, 7 preferences fit"). Pick one or type a topic.
2. **Choose**: General rules and workflows that mention the topic are ticked;
   answers are never pre-ticked, since they are personal facts more often than
   standards. Project preferences are never offered.
3. **Review** shows the exact public text. **Publish** after ticking "I reviewed
   this exact text". The page then offers a ready post ("I published the code review
   standards I actually apply. Your AI can use them on your next project: link").

The same page lists topics, followers and uses this week, and has **Remove** per
topic and **Turn off** for everything. An agent can start the flow with
`collaboration` action `publish` and a topic (or `varyz cloud public "code
review"`): it returns the review link and never publishes on its own. A published
standard leaves the page in the same sync transaction when its source is deleted
or stops being General. A creator's page and standards pause while their Cloud
plan is inactive and return when it is active again.

### Follower: one step

- On the profile, **Follow**. Signed out, the button signs in or signs up once and
  the follow completes on return (`/@username?followed=1` confirms it).
- Or tell the agent "follow @egr" or "use egr's Varyz" (the `follow` tool), or run
  `varyz cloud follow egr`. `varyz cloud following` lists who and which topics;
  `varyz cloud unfollow egr` stops.
- Nothing else to set up: from the next task, `start_task` carries the matching
  standards under `followed`, and `ask` can answer from them.

### Which Varyz applies

1. **The follower's own Varyz always wins.** Followed standards are labelled as
   someone else's (`basis: followed`), are never recorded as owner rules, and the
   agent says whose standard it used.
2. **One creator per topic**, chosen in this order:
   - **named** in the task: "@ann" or "ann's Varyz";
   - **picked**: when two followed creators cover one topic, Shared → With me →
     Following shows "Who answers code review?", or "use @ann for code review"
     (`follow` action `pick`, `varyz cloud follow ann --for "code review"`);
   - **latest**: otherwise the most recent follow.
3. A topic matches the task from plain text, with no model call: the topic's own
   words, the built-in kinds of work, and everyday aliases ("review this PR" is
   code review; "fix the failing test" is testing and debugging). A named creator
   with no matching topic lends all of their topics to that task.
4. `ask` uses a followed answer only for preference and delegated questions, never
   permissions or blockers, and only when the owner has no answer of their own. If
   the owner has an answer this agent may not see, the followed one is dropped and
   the question goes the usual way.

The brief lists other creators covering the same topic under `also_available`,
and the pick is shown with its reason (named, picked or latest).

### Price and privacy

Following is free with a free Cloud account. Publishing needs the Cloud plan
(`402 plan_required`: "Publishing standards needs the Cloud plan. Following others
stays free."). There are no creator fees or payouts.

Local Varyz sends a task or question to Cloud only when the person follows at
least one creator. It checks that with one list call cached for 60 seconds; any
failure goes quiet for ten minutes and never delays or fails a briefing or an ask
(4 second timeout). Cloud does not store task or question text; it counts uses
per creator and day (kept 90 days) so the creator sees "tasks" and "answers" this
week.

### Limits

The constrained server test in `docs/PUBLIC_VARYZ_SERVER_CAPACITY_2026-10-11.md`
failed at 10,000 rate-limit keys: existing global counters could not reset, so
the candidate returned HTTP 429s after 20 error-free minutes at 10 mixed requests/s.
The repair preserves the key ceiling and passed 45 regression/security tests.
The repaired candidate's follow-up in `docs/PUBLIC_VARYZ_MAXIMUM_CAPACITY_2026-10-11.md`
confirmed 30 creator-only requests/s for ten minutes and 8 mixed requests/s for
thirty minutes, with zero errors/drops and each endpoint p95 <=1 s / p99 <=2 s.
The earlier strict 500-ms p95 target still fails at these rates. Creator-only
traffic overloads at 40 requests/s; mixed traffic overloads at 16 requests/s.
These are tested rates under 1.5 CPU / 1,536 MiB, with 10,000 fixture followers
using 1,000 source IPs; the 10,000-counter key ceiling remains. The public creator
feature was deployed on 2026-10-12 as `public-20261012-v1`; the existing
`egr,joemama` pilot gate remains. The rollover repair is live with the cap and
backpressure release in `docs/CLOUD_SPIKE_PROTECTION_2026-10-11.md`.
The earlier report `docs/PUBLIC_VARYZ_CAPACITY_2026-10-11.md` distinguishes
10,000 synthetic stored followers from production capacity. The inspected live
deployment now has a 10,000-account ceiling and bounded fair request admission,
the two-account pilot audience, and Public Varyz profile, creator, following API
and hosted tools. Deployment passed 26 synthetic checks against the running
package and 17 real HTTPS checks; desktop/mobile publishing and following flows
were visually reviewed using the exact candidate runtime. No owner standards
were published. See `reports/PUBLIC_VARYZ_DEPLOYMENT_2026-10-12.md`. These checks
do not establish general availability or a new production capacity promise.

A follower follows at most 50 creators. A topic holds at most 200 standards; a
creator's whole publication follows the 500 item and 256 KiB ceiling. A brief
carries up to about 6,500 characters of standards and pages the rest with
`follow` action `standards`. Publications are encrypted at rest; the last ten
revisions are kept and older ones are removed after 90 days. Followers read only
the current revision.

| Surface | Endpoint or tool |
| --- | --- |
| Profile | `GET /@username`; `POST /@username/follow`, `/unfollow` |
| Creator pages | `/account/public`, `/topic`, `/preview`, `/publish`, `/remove`, `/off` |
| Follower pages | Shared → With me → Following; `POST /account/following`, `/pick` |
| Local and API clients (free accounts) | `POST /api/sharing/v1/following`, `/brief`, `/ask` |
| Hosted owner MCP | `follow` tool; `start_task.followed`; `ask` fallback |
| Guest MCP (`/mcp/shared`) | `follow`, `followed_brief`, `followed_ask` |
| Local MCP | `follow` tool; `start_task.followed`; `ask` `followed_answer` |

## Project explanation and progress (2026-10-10)

The invitation form includes a factual explanation of what the project is and
what it is meant to do. The owner can edit it on the shared project page. No
vision is generated or shared; intended purpose requires the owner's own words
or explicit confirmation. Existing projects without an explanation say so.

People see the explanation, latest attributed work and reported checks, current
open work/questions, next steps and the next reported milestone. These are the
latest shared report's snapshot, not an independently verified complete backlog.
Empty fields mean not reported. Agents submit these fields with owner-side
`collaboration` action `report` or recipient-side `shared_finish`.

The first `shared_brief` page also carries `collaboration.project_context`.
Its `agent_context` contains dated, attributed recent shared reports and any
owner-reviewed story entries. This background stays out of the recipient's page;
agents use it silently unless relevant or requested. Reported decisions and old
plans are evidence, not owner rules, permission or confirmed future goals.

An owner's agent can prepare `collaboration` action `context` with an explanation
and story entries (`text`, `at`, `by`, `source`). It returns the owner's review
page. Drafts stay private until the authenticated owner saves the explanation
and chooses whether to include the story. The same review can remove the story.
Publication changes invalidate the previous brief revision. Only shared reports
and explicitly approved context are exposed; raw owner archives remain excluded.
Context is encrypted at rest, bounded, charged against the sharing storage budget,
and removed with its project. Existing membership, pause and revocation checks
apply. No useful-links section was added.

## Notification delivery

People hear about what needs them without opening Cloud:

| What happened | Who is told |
|---|---|
| An invitation | The person invited |
| A guest asks a question | The owner |
| The owner (or a Decide partner) answers | The guest who asked |
| A question routed to a Decide partner | Each Decide partner |
| A partner decides | The owner |
| A GitHub access request or a guest's work report | The owner |

A notice says who did what in which project, never what was asked, answered or
shared. Cloud writes it in the same transaction as the action
(`src/varyz/cloud/sharing_notify.py`, table `shared_notices`).

- **Email.** Pending notices are grouped in one email. Each email has a one-click
  unsubscribe (RFC 8058) that needs no sign-in. Unverified accounts get none. It
  uses the Cloud SMTP settings already in place.
- **Desktop.** The local autosync daemon asks Cloud about once a minute and shows
  each new notice as a system notification (Windows toast, macOS notification,
  Linux `notify-send`), then marks it shown. No console window opens; notice text
  is passed as data, never as script. Click opens the Cloud page. On Windows the
  toast says "Varyz" with its logo (registered per user under
  `HKCU\Software\Classes\AppUserModelId\VaryzAI.Varyz`) and stays 10 seconds
  before moving to the notification centre.
- **Agent.** Invitations still appear in `shared_projects` and questions in the
  owner's `start_task` digest, as before.

`varyz cloud notifications status | email on|off | desktop on|off | test`
controls both. `test` shows one sample desktop notification and sends one sample
email.

## Levels and working together

GitHub carries the code; Varyz carries the context. The owner picks one level per
person and can change it later on the share page or through their agent.

| Level | What the person's AI may do |
| --- | --- |
| Consult | Follow the owner's shared preferences and ask the owner the rest |
| Contribute | Consult, plus their own Varyz fills small routine gaps; each is provisional and goes to the owner for review |
| Decide | Contribute, plus answer other people's project questions; each answer is attributed and the owner can overrule it |

At every level the owner's preferences come first, nothing becomes the owner's
personal rule, and every answer carries `permission_grant:false`. A guest's AI
answers in this order: owner preferences, Decide partners' decisions, its own
Varyz (Contribute and Decide, routine gaps only, provisional), then a question to
the owner or a Decide partner, where the first answer settles it. When a guest's
AI disagrees with an owner preference, the preference still wins and the
disagreement reaches the owner as a suggestion to adopt or dismiss. While the
owner is away, their agent can route a project question to Decide partners
(`ask_partner`). Guests need no subscription at any level, including Decide.

What each side does (the agents do the work; people answer short questions):

1. The owner's agent links the repository (`link_repo`). The guest's brief then
   carries a `collaboration` block with the repo and one server-computed next step.
2. The guest's agent checks `git`, `gh` and access, asks once before installing
   anything, and calls `shared_github`. With a GitHub username it requests access.
3. The owner's briefing shows the request with the exact
   `gh api -X PUT repos/<owner>/<repo>/collaborators/<user>` command. The agent
   asks once, runs it, then calls `access_granted`. The guest's agent accepts the
   invitation and clones into its own folder.
4. Both sides finish work with a short report (summary, branch, commit, run
   command). The other side sees new reports at its next task, fetches the branch
   into a separate `git worktree`, and asks the person before running a
   collaborator's code the first time.
5. Provisional answers, partner decisions and disagreements wait under **To
   review** for keep or overrule, adopt or dismiss.

Owner surfaces: the cloud and local MCP tool `collaboration` (actions `status`,
`digest`, `add`, `remove`, `link_repo`, `report`, `access_granted`, `set_level`,
`review`, `ask_partner`), the `varyz cloud share add|remove|status` commands,
`POST /api/sharing/v1/owner/collaboration`, the share page, and a counts-only
digest in `start_task` when something needs attention. Guest tools add
`shared_join`, `shared_github` and `shared_answer` (Decide only); `shared_finish`
takes `work`.

## Implementation

| File | Responsibility |
| --- | --- |
| `src/varyz/cloud/sharing_models.py` | Strict versioned requests and tool schemas |
| `src/varyz/cloud/sharing_store.py` | Additive SQLite/PostgreSQL schema, FKs and indexes |
| `src/varyz/cloud/sharing_service.py` | Publications, membership checks, questions and atomic replies |
| `src/varyz/cloud/sharing_routes.py` | Browser/API routes and the separate MCP resource |
| `src/varyz/cloud/sharing_ui.py` | Escaped selection, review, invitations, levels and collaboration forms |
| `src/varyz/cloud/following.py` | Public Varyz: publishing, profiles, following, routing, followed briefs and answers |
| `src/varyz/cloud/collaboration.py` | Local owner tool, `follow` tool and cached `start_task` digest and followed standards through the Cloud account |
| `docs/contracts/shared-varyz-v1.json` | Generated contract snapshot |
| `plugins/varyz-shared` | Guest-only native connector and consultation skill |

All writes use the Cloud critical transaction lock. SQLite foreign keys are
enabled; PostgreSQL uses the existing serialized write/advisory transaction
adapter. Remember, publication, reply and idempotency receipt commit together.
Local ledger supersession retains its Cloud record identity so private corrections
appear as pending replacements.

| Boundary | Enforcement |
| --- | --- |
| Explicit disclosure | Safe canonical projection; reviewed source versions and fingerprint |
| Intended recipient | Verified normalized email, or a username resolved to its verified account; encrypted address and keyed digest |
| Invitation replay | Hashed token; seven-day expiry; revoked tokens/members denied before replay |
| Guest isolation | Eleven tools only; fresh actor/member/owner entitlement check every request |
| Personal resource isolation | Shared OAuth credentials rejected by owner/archive/sync/export endpoints |
| Task selection | Actor, OAuth connection and conversation binding with compare-and-swap revision |
| Complete paging | HMAC cursor binds actor, connection, task, membership and publication |
| Reply audience | Only the requester can retrieve its thread; publication requires owner's checkbox |
| Instruction authority | Attributed preference data; `permission_grant:false` |
| Erasure | Collaboration FK cascades include publication, reply and idempotency content |
| Token disclosure | No-referrer invitation pages; opaque sign-in continuation; no third-party assets |

## Operational limits and retention

Publication ceiling: 500 items and 256 KiB. A projected item must fit the bounded
tool page; oversize text fails explicitly rather than being shortened. Brief pages
return at most 20 items, reserve room for metadata/cursor, and expose incomplete
coverage. Lists page at 20. Question/status requests contain one to five items.
Question and reason length: 500 characters each; reply length: 4,000.

Each collaboration has at most ten pending invitations and ten active members.
Each member has at most 20 open questions; each share has at most 100. New
questions are capped at five per minute and 60 per day; identical open questions
do not consume quota again. An over-limit batch commits no new questions.
Each collaboration keeps its 20 newest work reports; an identical report within
ten minutes is a duplicate. At most 100 items wait for review and 20 questions
wait for Decide partners per collaboration.
Encrypted sharing text has a separate per-owner budget using the configured
`VARYZ_CLOUD_STORAGE_LIMIT_BYTES` ceiling. Limit errors do not block revocation,
retraction or erasure. This is an operational ceiling, not a new paid tier.

The hourly lifespan retention worker removes expired previews and idempotency
receipts, terminal questions/replies after 90 days, non-content events after 90
days, terminal/expired invitations after 30 additional days, and historical
publications after 90 days. Work reports, reviewed items and answered partner
questions are removed after 90 days. The current publication remains. Open questions remain
until resolved, withdrawn or access ends. Idempotency receipts normally expire at
24 hours; invitation-producing responses expire at 15 minutes to bound raw-token
replay. Reviews expire after ten minutes.

Revocation stops future reads; it cannot recall text already received by an AI.
Erasing a collaboration leaves separately remembered private owner knowledge.
Operational backups follow the existing Cloud backup policy; protected rollout
snapshots are recovery copies and must follow that policy too.

## Configure and verify

```powershell
$env:SHARED_VARYZ_ENABLED = "true"
$env:SHARED_VARYZ_ACCOUNTS = "egr,joemama"
# Use the existing Cloud archive Fernet key; never commit it.
uv run python scripts/shared_varyz_schema.py
uv run pytest tests/test_cloud_sharing.py tests/test_cloud_following.py tests/test_local_cloud_sync.py --basetemp=.tmp/shared-tests
uv run python scripts/shared_varyz_probe.py
uv run python scripts/shared_varyz_metrics.py
```

`shared_varyz_metrics.py` prints counts only (events, GitHub access states, the
share of guests stalled before access, and whether a hosted git remote looks
worth building). It is the data for deciding Decide pricing later.

For isolated PostgreSQL fixtures, set `VARYZ_PROBE_POSTGRES` to a disposable
database. The probe creates a unique schema and drops only that schema in `finally`.
The local browser harness `scripts/shared_varyz_preview.py` is synthetic and
loopback-only; never deploy its temporary sign-in endpoints.

`scripts/shared_varyz_rollout.py` captures live source, builds an overlay candidate,
and checks both database backends before `--apply`. It preserves the deployed
owner MCP implementation and unrelated local work. Apply takes a protected SQLite
backup, saves the prior compose file, enables only the two pilot accounts and
checks public resource metadata. Failed health checks restore the previous compose
configuration and image; the additive database schema is retained to avoid losing
concurrent writes. Never restore a database snapshot over live activity casually.

`scripts/shared_varyz_live_probe.py` runs in the deployed container. It creates
only synthetic `joemama` records, exercises real HTTPS with `egr` as recipient,
erases its temporary access grants and test collaboration, and retains a clearly
named demo for native-host verification. It does not inspect `egr` private content.
HTTP probes, browser automation and package upload do not establish native host
success. That gate requires native OAuth and an actual shared tool invocation in
a fresh task.

The setup status API accepts only authenticated members. Schema version 3 stores
the connection, membership, publication and full-read timestamp; a browser flag
or personal OAuth grant cannot mark setup ready. Receipts expire after 90 days.
`SHARED_VARYZ_CHATGPT_PLUGIN_URL` optionally configures the authorized pilot's
connection link. Keep the existing private plugin audience unchanged.

## Licensing boundary

This implementation remains in the existing AGPL repository and reuses its
licensed Cloud store and UI. It does not move contributed source into a proprietary
service or alter contributor terms. Any future proprietary Shared Varyz repository
requires the separate licensing boundary described in the owner's private plan.
