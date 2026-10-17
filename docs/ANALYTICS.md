# Optional improvement analytics

> Design 2026-10-04. Sharing is off until explicitly selected during setup.

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

Archive commands accept `--config`. Browser setup opens a separate optional popup when
the user clicks **Set up Varyz**, before setup starts. Egor's personal note says he is
the solo developer, counts would help improve Varyz, and no message content reaches him
through these reports. The network-address caveat stays visible; **What gets shared**
expands the counter categories and fields.

**No thanks** starts setup with sharing off; **Share anonymous counts** starts it with
explicit consent. Both choices receive the same button styling. Escape closes the popup
without starting setup or changing consent. Reruns disclose existing consent and allow
revocation with **No thanks**. Failed start requests leave the page retryable. Terminal
setup shows the same disclosure and asks separately with a default-no confirmation.
`setup --yes` never grants analytics consent. Missing optional terminal input preserves
existing settings.

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

## Verification

Offline tests cover consent, history/content exclusion, preview rollback, revocation,
retry deduplication, outcome distinctions, schema/rate limits, owner authentication and
end-to-end delivery. Setup/server regression suites and visual review are required.
Live receipt and authenticated reporting must be separately verified before claiming
that the creator can receive data.

The focused rollout helper is `deploy/cloud/deploy_analytics.py --host 2.31.3.96` (dry-run).
`--apply` updates only the shared schema, receiver, registration and environment mapping,
preserving other deployed source and billing settings. It backs up changed files and the
root-only environment, rolls back on build/start failure and stores the reporting token
in the owner's OS credential store without printing it. It does not deploy local setup
changes or publish a new installer.
