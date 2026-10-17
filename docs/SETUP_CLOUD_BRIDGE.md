# Setup to Cloud connection - 2026-10-17

The curated private build linked to Cloud without shipping the native connection
bridge. It also used resolution port 8765, while the hosted browser discovers
`http://127.0.0.1:1207/cloud/computer`. A healthy resolution daemon was therefore
not evidence that Cloud could connect.

The yellow setup button now visits an authenticated local `/cloud` route first.
It verifies the bridge's configuration hash, reuses this archive's listener, or
starts a detached browser-only helper on port 1207. A native daemon on that port
is restarted when it lacks the new bridge capability. Existing resolution ports
and agent registrations remain intact. Cloud opens only after the bridge answers.
A failure stays visible with retry and a link back to setup.

The helper exposes only Cloud's connection routes, requires the exact hosted
origin and loopback Host, and supplies the browser's private-network preflight.
It never exposes the resolution API. Discovery and identity checks perform no
Cloud calls, sign-in, data selection or uploads. Existing browser authorization
and the account-bound preview/approval protocol remain separate steps.

Only native dependencies and wire contracts are included. The hosted server and
its database implementation are not bundled. Legacy archives without the work
report ledger expose no report projects; their confirmed preferences and approved
archive history remain available. This reachability fix does not add a background
sync worker to those older builds.

Validation: real loopback HTTP and approval-boundary tests, setup lifecycle tests,
Chrome navigation and retry on desktop/mobile, and an isolated installed-wheel
check that starts the detached helper, checks CORS/private-network preflight, and
previews a confirmed preference without enabling uploads. Live macOS permission
handling and the owner's signed-in Cloud account require a Mac retest.
