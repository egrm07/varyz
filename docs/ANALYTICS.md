# Optional improvement analytics

> Design 2026-10-04, Varyz network added 2026-10-18. Sharing is off until explicitly
> selected in the one Varyz network popup.

**Varyz sends bounded aggregate counts to its creator, Egor Menshikov, only with consent.**
The receiver is `https://cloud.varyzai.com/api/analytics`; no Cloud account or subscription
is required. The public website has separate default aggregate counters and consent-gated detailed measurement; see
`WEBSITE_ANALYTICS.md`. Website consent never enables software analytics or vice versa.

## Counters and limitations

| Counters | Meaning |
|---|---|
| Answers | Answered, unknown, ambiguous or clarification; explicit/reported/observed/inferred basis |
| Repeated questions | Same normalized question, purpose and project requiring clarification in another identified conversation; matching stays local |
| Outcomes | Explicit adopted/corrected/overridden records; supersession is a preference change, not an error |
| Correction reasons | Explicit wrong preference, stale rule, wrong scope, agent ignored advice, other |
| Rules | Confirmations/revisions and explicit review/trial keep/drop events |
| Setup | Started, ready/failed and seven steps' done/failed/skipped counts |
| Reliability | Resolution failures, sync outcomes, verified repair recovery and owner blockers |
| Response time | Below 100 ms, 1 s or 5 s, or above 5 s |

Reports include UTC day, version, OS family and a random batch ID for retry deduplication.
Synthetic delivery probes are marked separately and excluded from usage totals.
No permanent user/installation ID, messages, questions, answers, rules, excerpts, names,
projects, paths, accounts or exception text are sent. The receiver sees the connection's
IP address; temporary rate limits use hashes. Access/body logging remains disabled.

**Silence is unknown. Counts are not accuracy or unique-user estimates.** Overrides
cannot prove original answers wrong; rule lifecycle changes do not prove usefulness.
Opt-in users are a biased sample. Automatic outcomes currently capture supersession;
explicit corrections/adoption need recorded feedback. Anonymous submissions can be
fabricated and must not be treated as authenticated experiments.

## Controls

| Command | Effect |
|---|---|
| `varyz analytics status` | Consent, pause, pending counts, last delivery/error |
| `varyz analytics consent on` | Disclosure and separate default-no confirmation |
| `varyz analytics consent off` | Stop collection/delivery and discard unsent counts/batches |
| `varyz analytics pause` / `resume` | Stop/resume delivery; resume cannot grant consent |
| `varyz analytics sync --dry-run` | Preview one exact bounded report without network or state change |
| `varyz analytics sync` | Attempt one report with bounded retries |
| `varyz analytics doctor` | Integrity, recorded errors and actual delivery verification |
| `varyz analytics feedback ID corrected --reason wrong_scope` | Explicit outcome for that local resolution ledger ID |
| `varyz analytics report` | Creator-only server summary using OS credential-store token |

Archive commands accept `--config`.

### The Varyz network popup

One popup governs everything Varyz sends: these counts and the anonymized examples
described under **Network contributions** below. Setup does not show it: browser setup
starts with sharing off and asks nothing. The popup appears in rules review
(`varyz rules review`, or **Review preferences** after setup), right after the owner's first
saved review once the trial window is over: more than 3 conversations and at least
24 hours since Varyz first saw one, the same window that holds back news. It is titled
**Join the Varyz network**,
explains that pooled, anonymized examples help everyone's agents repeat fewer mistakes
and waste fewer tokens, and always shows a short notice that anonymized examples may be
licensed to AI companies and that turning sharing off deletes what was shared.
**What gets shared** expands the fields and the scrubbing rules.

There is one button, **Share anonymized data**, which turns sharing on. The small x in
the top right corner, and Escape, record no: closing means no. The popup is shown once.
After a no it waits at least 30 days and is offered again at most once, so it is shown at
most twice per installation; a no given earlier (terminal setup or `varyz data off`)
counts as the first offer. Anyone who has ever turned sharing on is never shown it again;
they use `varyz data on` and `varyz data off`. Each showing is recorded in the
append-only analytics audit as `offer_shown`, which turning sharing on or off never
clears. Terminal setup shows the same text and asks **Share anonymized data?** with a
default-no confirmation. `setup --yes` never grants consent. Missing optional terminal
input preserves existing settings.

Consent is versioned. `network-v1` covers counts and contributions. Installations that
agreed to the older counts-only text (`aggregate-v1`) keep sending counts and never send
contributions until they agree to the new text.

## Network contributions

| Command | Effect |
|---|---|
| `varyz data status` | Consent, pending examples, last upload/error, deletions still owed |
| `varyz data preview --limit 20` | The exact scrubbed records the next upload would hold; sends and changes nothing |
| `varyz data on` | Same disclosure and default-no confirmation as setup |
| `varyz data off` | Stop all sending, discard unsent data and delete shared examples from the server |

With `network-v1` consent, the daemon also builds examples from archive rows created after
opt-in and within the last 30 days: corrections (the agent's question, Varyz's answer,
adopted/overridden/corrected and the owner's correction), answered escalations (tiers
tried, urgency, seconds to answer), confirmed or revised rules, and rules rejected in
review. Only `rule` principles are used; vision notes never leave.

Scrubbing is local and fail-closed. Emails, links, file paths, IP addresses, phone-like
numbers, handles and long hex or ULID identifiers become placeholders, then known project
names become `[project]` and known people, aliases and the OS username become `[person]`.
An example is dropped if a credential is found before or after scrubbing, if Varyz
withheld anything from that answer, or if it fails the strict schema. Cleaning can miss
something, which the popup says.

Each installation creates a random contributor key; the receiver stores only its SHA-256.
Uploads run every six hours (one hour after a failure), at most 200 records per batch with
three retries. Turning sharing off keeps the key in a local erase list and retries
`DELETE /api/contributions` until the server confirms. Examples are kept on the server
until deleted; copies already delivered to a licensee cannot be recalled.

## Persistence and delivery

Local SQLite state is `data/analytics/analytics.sqlite3`. Versioned consent sets a start
timestamp; earlier archive records are excluded. Re-enabling after revocation starts fresh.
The existing resolution daemon runs a worker thread, with no extra process, scheduler
or terminal window. Processes already running old code need a restart.

Each pass reads at most 2,000 new rows/table. Named counters cap at 1,000,000; pending
batches and age are bounded. Delivery attempts one report/day, with at most three retries,
bounded backoff and five-second request timeouts. Failures retain the batch ID and back
off an hour. Explicit sync may retry sooner. HTTPS uses the fixed origin, verifies TLS,
rejects redirects and ignores environmental proxies. Off/pause serializes with in-flight
sending; after return, no further sends occur until consent/resume. Revocation cannot
recall delivered data. Local audit actions/timestamps are append-only.

## Receiver and owner access

`VARYZ_ANALYTICS_ENABLED=true` enables receipt. `VARYZ_ANALYTICS_ADMIN_TOKEN` protects
`GET /api/analytics/summary`. Store the token in the server's root-only environment and
Windows Credential Manager, service `varyz-cloud`, key `VARYZ_ANALYTICS_ADMIN_TOKEN`.
Distributed clients never contain this secret; it never belongs in a URL.
The normal deployment helper restores these analytics variables from the creator's
OS credential store; code-only deployments with `--keep-env` preserve the runtime values.

Receipt persists in the existing Cloud database independently of accounts. Validation
rejects unknown fields and arbitrary strings; limits are six requests/minute per hashed
network address and 100,000 retained reports. Duplicate IDs are idempotent only for the
same payload. Owner totals group by day/version/OS and report count, never user identity.
Reports expire after 30 days, cleaned on receipt/report access. Existing encrypted
backups may retain expired rows until their thirty-day backup rotation completes.

`VARYZ_CONTRIBUTIONS_ENABLED=true` enables receipt of network contributions (default
false). `DELETE /api/contributions` works even while receipt is off, so deletion is always
honoured. The contributor key must be 32 to 128 URL-safe characters; the same hashed rate
limit applies. Capacity is 2,000,000 records, 50,000 per contributor; retried records are
stored once and never overwritten. The owner token also protects
`GET /api/contributions/summary` and `GET /api/contributions/export?kind=&after=&limit=`,
which pages records without contributor IDs.

## Verification

Offline tests cover consent, history/content exclusion, preview rollback, revocation,
retry deduplication, outcome distinctions, schema/rate limits, owner authentication and
end-to-end delivery. Setup/server regression suites and visual review are required:
`scripts/analytics_preview.py` drives the real review page and popup at 1440x900 and
390x844 and saves screenshots to `.tmp/analytics-preview`.
Live receipt and authenticated reporting must be separately verified before claiming
that the creator can receive data.

The focused rollout helper is `deploy/cloud/deploy_analytics.py --host 2.31.3.96` (dry-run).
`--apply` updates only the shared schema, receiver, registration and environment mapping,
preserving other deployed source and billing settings. It backs up changed files and the
root-only environment, rolls back on build/start failure and stores the reporting token
in the owner's OS credential store without printing it. It does not deploy local setup
changes or publish a new installer.
