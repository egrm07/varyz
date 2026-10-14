# Plugin and cloud validation

> 2026-10-02. Local implementation checks; no public service or marketplace release.

## Verified

- 66 targeted plugin, cloud, dashboard, local sync, SQL compatibility and CLI help tests passed.
- 318 existing installer, setup, autosync and CLI regression tests passed; 13 were skipped by their existing gates.
- Ruff passed for the new cloud/plugin modules, their tests and CLI integration.
- Plugin generated bundles passed `scripts/plugin/build.py --check`.
- Compose configuration validated without starting an engine.
- Synthetic account dashboard and sign-in forms were rendered and inspected at 1440×900 and 390×844. Expanded editors fit both widths without horizontal overflow.

## Not verified

- Ten live PostgreSQL tests require an isolated test database and were excluded by the default test profile, including the final question-queue and email-limit gates.
- Docker Desktop failed in its inference manager before the engine started. The startup attempt was stopped; no factory reset or data deletion was performed. Image execution remains unverified.
- Real email delivery, payment checkout/webhook/cancellation, HTTPS edge, backups/restore, burst performance and native platform OAuth/plugin interoperability require staging configuration.
- Public packages, marketplace listings and platform approval have not been published.

## Decisions still open

The owner approved Hetzner, `cloud.varyzai.com`, USD 5/month and USD 48/year without trials. The initial infrastructure tolerance is about USD 50/month with service limits. Cost arithmetic is in [CLOUD_HOSTING.md](CLOUD_HOSTING.md). Backend purchase is deferred; live Paddle product/prices were created on 2026-10-03. Resume from [CLOUD_HANDOFF.md](CLOUD_HANDOFF.md).

The runnable deployment example uses conservative enrollment, preference and daily email limits. Those limits bound admitted work, not every provider charge; physical disk and invoice monitoring remain necessary.

## Billing follow-up — 2026-10-03

44 cloud service, dashboard and offline PostgreSQL boundary tests passed; 10 real PostgreSQL tests were deselected. Ruff passed. Added checks cover approved/pending/rejected refunds, notification ordering/replay, old-payment refunds after renewal, tenant isolation, cancellation versus deletion, timestamp offsets, and checkout token environment/script isolation. Provider HTTP responses remain mocked. Actual Paddle sandbox payments and webhook deliveries have not been tested. See [PADDLE_SETUP.md](PADDLE_SETUP.md).
