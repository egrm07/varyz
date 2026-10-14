# Cloud hosting

> Design and price check 2026-10-02. Owner mandate: build cloud, **no free trial**, propose infrastructure before provisioning. Owner prefers the lower idle bill and a **USD 50/month tolerance with service limits**. Provider remains open. Architecture contract: `docs/CLOUD_BUILD.md`.

**Do not buy capacity from a user-count forecast. Start with measured request cost, bounded capacity and a migration path.** Cloud stores selected confirmed preferences; extraction and archive search remain local. No cloud inference budget is included.

## Options to review

Prices below are infrastructure baselines, not a quote. Currency, region, VAT, bandwidth, backups, domains and payment-processing fees change the total. Recheck the provider calculator before purchase; no resources have been provisioned.

| Option | Small idle baseline | Unexpected demand | Main trade-off |
|---|---|---|---|
| Hetzner CX23, IPv4 and server backups | **EUR 7.09/month net**: 5.49 server + 0.50 IPv4 + approximately 1.10 backups; approximately EUR 8.44 with 19% VAT | Fixed capacity; reject excess work, then resize/migrate deliberately | Lowest predictable server bill; we own OS, TLS and tested database backups |
| Render app + managed PostgreSQL, no autoscaling | **USD 13.30/month**: 7 app + 6 DB compute + 0.30 for 1 GB DB storage; USD 14.50 with 5 GB | One app instance; manual resizing and service admission caps | Smallest managed baseline; tiny DB needs capacity tests |
| Render with autoscaling | **USD 38.30/month** minimum: above 1 GB baseline + 25 Pro workspace; USD 39.50 with 5 GB | App replicas within configured min/max, each billed | Less room inside the USD 50 tolerance; three app replicas + 5 GB DB = USD 53.50 before overages |
| Cloud Run + Cloud SQL micro | **USD 9.37/month idle** before backup bytes/app usage at published Iowa rates; approximately USD 10.17 including an illustrative 10 GiB retained backup allowance | App scales to zero/bursts; database stays running and limits throughput | Usage-based app bill; EU region needs its own quote, shared-core DB has no SLA |

Cloud SQL arithmetic uses the pricing page's **Iowa (`us-central1`) default**, Enterprise shared-core `db-f1-micro`, no HA, 730 hours: 730 × USD 0.0105 = USD 7.665 compute; 10 GiB SSD × USD 0.17 = USD 1.70; total USD 9.365. Backup storage is usage-based: 10 GiB retained × USD 0.08 = USD 0.80, giving USD 10.165. The 10 GiB disk is the documented minimum. This is calculated from published rates, not a saved provider-calculator quote, and **does not select a US region**. A European-region quote remains open. [Cloud SQL rates](https://cloud.google.com/sql/pricing?hl=en), [minimum disk size](https://docs.cloud.google.com/sql/docs/postgres/issues-updating-storage-capacity).

Cloud Run app costs are separate. Illustrative gross calculation: 1 million requests, 100 ms billed instance time per request, no overlapping requests, 1 vCPU and 0.5 GiB: CPU USD 2.40 + memory USD 0.125 + requests USD 0.40 = **USD 2.925**, before allowances, egress and supporting services. This is a workload example, not a measured Varyz latency or traffic forecast. Ten times the duration raises the compute component tenfold. The former USD 25–60 allowance is superseded by these explicit assumptions. [Cloud Run pricing](https://cloud.google.com/run/pricing).

Google excludes shared-core and single-zone instances from its Cloud SQL SLA; a production HA configuration requires a separate, larger quote. The starter allowance does not buy an availability commitment. [Cloud SQL SLA](https://cloud.google.com/sql/sla-20250910).

Render's Pro plan is a flat USD 25/month; legacy per-seat pricing should not be used. DB storage costs USD 0.30/GB/month; creation accepts 1 GB or multiples of 5 GB. Storage increases cannot be reversed. Start with storage autoscaling disabled if predictable bills take priority; monitor free space and reject writes before exhaustion. This deliberately sacrifices unlimited growth. [Render pricing](https://render.com/pricing), [DB storage sizes](https://render.com/docs/postgresql-creating-connecting), [workspace plans](https://render.com/docs/new-workspace-plans).

Render Hobby includes 5 GB monthly outbound bandwidth; excess costs USD 0.15/GB. Pro includes 25 GB. TLS and a first custom domain are included, but a domain registration is separate. DB backups, sender service, payment fees and build/egress overages still need deployment-specific review. [Workspace allowances](https://render.com/docs/new-workspace-plans), [scaling limitations](https://render.com/docs/scaling).

Hetzner's price is for Germany/Finland. Primary IPv4 is separately billed at EUR 0.50/month and server backups at 20% of server price (seven slots). The rounded net sum is 5.49 + 0.50 + 1.10 = EUR 7.09. VAT depends on billing country; EUR 8.44 illustrates 19% applied to that rounded net subtotal, not the owner's confirmed tax treatment. Server backups do not replace a consistent off-host database backup/restore process. [Server price](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [IPv4 price](https://docs.hetzner.com/cloud/servers/primary-ips/overview/), [backup charging](https://docs.hetzner.com/cloud/billing/faq/), [VAT policy](https://docs.hetzner.com/general/billing-and-account-management/billing-at-hetzner/value-added-tax/).

## Proposed staged choice — awaiting owner selection

1. **Validate privately with the single-instance persistent-volume container.** Use synthetic accounts, billing test mode and an isolated HTTPS staging hostname. Do not place a SQLite file on ephemeral Cloud Run storage.
2. **For the owner's lower-bill preference, propose Hetzner with a fixed single instance or Render without autoscaling first.** Hetzner is the lowest listed infrastructure bill; Render buys managed operations at approximately USD 13.30–14.50 before variable charges. Choose explicitly before provisioning.
3. **Keep admission limits and rejection visible during a spike.** Validate PostgreSQL/shared limits before replicas. Consider Cloud Run after obtaining the approved-region quote if automatic bursting becomes worth variable costs. A sudden launch spike may receive 429/503 responses until capacity increases; this is the chosen consequence of bounded service capacity.

None of these options requires a reserved annual commitment. The USD 50 tolerance includes more than app compute: leave room for storage, egress, SMTP, domains and tax rather than spending all of it on autoscaling. Fixed server sizes plus request/storage/enrollment caps reduce runaway cost; they do not make every provider fee a hard USD 50 ceiling. Ordinary billing alerts never guarantee that ceiling.

## Capacity and bill controls

| Control | What it bounds | What it does not bound |
|---|---|---|
| Maximum app replicas | Concurrent compute footprint | Total hours, egress, database storage or all invoice charges |
| Request concurrency, bounded queue and timeout | Work admitted per instance | Requests reaching an external load balancer |
| Shared DB connection pool | Database connections; reserve admin/maintenance headroom | Cost of poorly indexed or expensive queries |
| Per-account and global request caps | Accepted application work; respond with 429 and retry guidance | Provider charges for rejected traffic before admission |
| Payload, record and retention limits | Stored and processed bytes per account | Provider billing lag or malicious traffic at the network edge |
| Budget alerts | Notify at spending thresholds | They do not stop ordinary billing |
| Provider spend cap, if supported | Pauses supported usage after threshold enforcement | Instant cutoff, all products or fixed resources |

Provisional hosted settings: min instances 0, service max 3, request concurrency 8, a five-connection pool per instance, 30-second request deadline. These are **initial test settings**, not measured capacity or an owner-approved budget. The adapter's default pool maximum is 8 per process; set `VARYZ_CLOUD_DB_POOL_MAX=5` for this provisional hosted configuration. Minimum pool size is zero, acquisition timeout ten seconds and wait queue maximum 32. Reserve DB connections for deployment overlap, platform overshoot and maintenance; verify limits against the selected DB plan. Record p95 latency, error/429 rate, pool wait, database CPU and cost per 1,000 accepted requests under a synthetic burst before changing them.

Cloud Run explicitly warns that its configured maximum can be briefly exceeded. Use service-level limits, not one independent limit per revision. [Maximum-instance behavior](https://docs.cloud.google.com/run/docs/configuring/max-instances).

Google's ordinary budgets are alerts. A separate **spend-cap budget is in preview** and currently lists Cloud Run as eligible. It pauses supported usage with delay; in-flight requests and fixed resources can continue accruing charges. A Cloud Run cap does not cap the Cloud SQL bill. Configure below the absolute tolerance and accept that a cap can cause an outage. [Budget alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets), [spend-cap limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## Production gates

- [ ] Select provider/region, configure the owner's USD 50/month tolerance with headroom, and review rejection behavior when capacity is reached.
- [ ] Run PostgreSQL concurrency gates against an isolated database; add versioned migrations and restore tests before multi-instance service.
- [ ] Verify shared database admission limits and durable webhook replay state during deploy overlap and failure; tune rate/cost caps from measurements.
- [ ] HTTPS/domain, verification/recovery SMTP with a verified sender, OAuth platform interoperation, paid checkout and signed billing webhooks in staging.
- [ ] Encrypted backups outside the app host, documented retention and successful isolated restore. A named Docker volume is persistence, not a backup.
- [ ] Load test bursts and retry storms; exercise graceful rejection, cold start, billing failure and database exhaustion.
- [ ] Set account storage/history limits and enrollment limits to fit the selected disk; monitor physical free space and database bloat. Per-account quotas do not cap aggregate storage across unlimited accounts.
- [ ] Privacy/retention terms, deletion/export, abuse controls, on-call alerts and payment-provider account setup.

**Current limitation:** SQLite is the default; an optional PostgreSQL adapter, shared database request limits and bounded connection pool now exist. Their real-database concurrency gates have not run without an explicitly supplied test database. The container example deliberately runs one worker; use a persistent volume for SQLite or an externally supplied PostgreSQL URI via the override. Automatic replicas remain a production gate until burst/concurrency tests, migration/restore verification and provider cap configuration pass. See `deploy/cloud/README.md` for the runnable local/staging layout.
