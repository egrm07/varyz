# Cloud deployment handoff

> 2026-10-02. Resume from this file after usage resets. Preserve unrelated worktree changes.

## Owner approvals

- Hetzner hosting: approximately EUR 7.09/month before VAT for proposed CX23, IPv4 and server backups.
- Public origin: `https://cloud.varyzai.com`.
- Customer subscriptions: USD 5/month and USD 48/year, no trial. *Superseded 7 October 2026: USD 12/month and USD 108/year, tax included, with EUR and GBP country prices (see PADDLE_SETUP.md). Sandbox prices remain USD 5/48.*
- Initial infrastructure tolerance about USD 50/month; favor low idle cost and reject excess demand.

These choices are approved; do not ask again. No server, DNS record, SMTP sender or payment product has been provisioned. Account login/billing still requires owner interaction through the browser or protected secret storage.

## Build and checks

See CLOUD.md, CLOUD_VALIDATION.md and deploy/cloud/README.md. Plugin bundles/guided setup, confirmed-preference sync, hosted MCP/OAuth, account dashboard, Paddle integration, quotas and deployment examples exist. Last root run: 66 targeted tests passed; 318 regression tests passed, 13 skipped. Ruff, plugin bundle consistency and Compose syntax passed. Dashboard and sign-in desktop/mobile screenshots were reviewed.

Docker Desktop crashed before engine startup. Startup was stopped without reset; container runtime remains unverified. Do not factory-reset Docker or delete its data.

Post-final review found PostgreSQL inherited SQLite-only question/email SQL. Worker corrected PostgresStore.queue_question and allow_email with native operations. Worker verification: 34 service/UI tests and two offline PostgreSQL tests passed, Ruff passed. Ten live PostgreSQL tests skipped without DSN. Inspect/retest these corrections before deploying; live concurrency remains unverified.

## Resume work

1. Rerun relevant tests/lint for final PostgreSQL changes; update validation counts from actual output.
2. Prepare Hetzner Germany single-instance configuration: x86 server, SSH keys, firewall, private DB, TLS edge, bounded admission, encrypted off-host database backups and tested restore. Server snapshots alone are not DB backups. Show exact total including extra services before purchase.
3. Owner completes Hetzner billing account, Paddle merchant verification and transactional-email sender/domain verification. Never request passwords or bearer credentials in chat.
4. Set DNS after obtaining server IP. Store secrets outside Git. Configure Paddle sandbox before live payments; recurring prices must have no trial.
5. Validate container persistence, PostgreSQL concurrency, native OAuth clients, SMTP, paid checkout/webhooks/cancellation/deletion, backup restore and synthetic burst rejection in staging. Then enable public paid use.
6. Publish actual privacy/processor terms and package/marketplace artifacts; obtain required connector approvals.

Deployment-example caps: 100 accounts, 1 MiB cumulative preference allowance each, 10,000 changes, 1,000 registered clients and 100 emails/day. These limit admitted work, not every invoice charge. Monitor physical disk, mail, egress and billing.

Varyz owner-service calls currently fail with an expired/revoked agent token. Explicit owner approvals above remain authoritative.

## Website publication paused — 2 October 2026

Owner chose free website hosting first with backend purchase deferred, then explicitly requested no publication yet because the repository is private. The website is a local Launching soon preview; public repository links, downloads and install commands are removed. No server was purchased, website deployed or assets uploaded. Do not publish until requested again.

## Static website published — 3 October 2026

Supersedes publication pause: owner approved Cloudflare CLI OAuth and publishing. Website is deployed at https://varyzai.com using static assets only, worker varyzai, version c547a90c-a36f-4200-b124-32dc4c7b6efd. Backend remains deferred. Paddle website URLs submitted and onboarding now waits at step 4 for DOB/business start date; no final account approval or payments activated.

## Paddle catalog and billing preparation — 3 October 2026

Owner completed the earlier onboarding form; Paddle now requires identity verification. Live Varyz Cloud product and USD 5 monthly/USD 48 annual prices created without trials, preserving Account default tax handling. IDs and remaining owner steps are in [PADDLE_SETUP.md](PADDLE_SETUP.md). Sandbox sign-in and payout configuration remain pending. No keys, webhook destination, backend server or live signup activated.

Local integration now includes a gated Paddle.js checkout page, completed-payment/full-refund ledger, and cancellation at the end of the paid period; account deletion still cancels immediately. 44 offline tests pass. Actual Paddle subscription scenarios remain untested until sandbox access and a receiving endpoint are ready.

## cloud.varyzai.com placeholder — 3 October 2026

`cloud.varyzai.com` now resolves through Cloudflare: Worker `varyz-cloud-placeholder` (version 629bac94-0ac0-45bf-91d4-a14b75971513, config in `deploy/cloud-placeholder/`) serves no content and 302-redirects every path to `https://varyzai.com`. It exists so Paddle can review the subdomain before the server is bought. Live Paddle: `varyzai.com` is approved; `cloud.varyzai.com` is approved for checkout and Apple Pay (3 October 2026); the default payment link is set to `https://cloud.varyzai.com/checkout` (by the owner, 3 October 2026); live webhook destination "Varyz Cloud billing" → `https://cloud.varyzai.com/api/billing/webhook` is active with 12 events (`transaction.completed`, all nine `subscription.*`, `adjustment.created`, `adjustment.updated`). Its secret is in Windows Credential Manager on the owner's PC (service `varyz-cloud`, name `VARYZ_PADDLE_WEBHOOK_SECRET`); copy it into the server's secret store at deploy, never into Git or chat. When the Hetzner server is up, delete the Worker (`npx wrangler delete` in `deploy/cloud-placeholder/`) and add a DNS-only A record to the server. The Paddle default payment link already points at `/checkout`, so the server must serve that page before live checkout works.

## Hetzner server — 3 October 2026

Supersedes the placeholder above. Server `varyz-cloud` in Hetzner project "Varyz": CX23 (2 vCPU, 4 GB, 40 GB), Nuremberg, Ubuntu 24.04, IPv4 `2.31.3.96` plus IPv6, daily backups on. EUR 8.58/month incl. 21% VAT (EUR 7.09 net), as approved. Root SSH by key only (`~/.ssh/varyz_cloud_ed25519`, Hetzner key "varyz-cloud-deploy"); user data is [deploy/cloud/cloud-init.yaml](../deploy/cloud/cloud-init.yaml) (ufw: SSH, 80, 443 only; host Caddy terminates TLS and proxies to Compose on 127.0.0.1:8080). The placeholder Worker is deleted; Cloudflare has a DNS-only A record `cloud.varyzai.com` → `2.31.3.96`. Deploy or redeploy with `uv run python deploy/cloud/deploy.py --host 2.31.3.96` (sandbox Paddle; add `--live` only after sandbox acceptance checks pass). Runtime secrets live only in root-only `/etc/varyz-cloud/cloud.env` on the server, written from Credential Manager over SSH stdin.

First deploy (sandbox mode, SQLite in volume `varyz-cloud_cloud-state`): the image first crashlooped because the `cloud` extra lacked `python-ulid` and `rich`, which the server imports while the image installs the package with `--no-deps`. Both were added, `requirements.txt` was regenerated, and a test now fails when a base dependency the server imports is unpinned. Verified from outside after the fix:
- `/health` returns 200 over a Let's Encrypt certificate, and HTTP redirects to HTTPS.
- `/` redirects to `/login`, and OAuth metadata returns 200.
- An unsigned webhook gets 401. Port 8080 is not reachable from outside.
- `/checkout` returns 503 by design, because the sandbox API key and client token are not stored yet.

Still open:
- Caddy sends no HSTS header.
- PostgreSQL, backup restore and the sandbox payment scenarios have not been run.

Sandbox checkout enabled the same day. In Paddle sandbox, the default payment link is `https://cloud.varyzai.com/checkout`. The API key "varyz-cloud-server (sandbox) 2" is limited to prices:read, subscriptions:read/write and transactions:read/write, and expires 2027-01-01, so replace it before then. A client-side token, "varyz-cloud-checkout (sandbox)", is also set. Both are in Credential Manager as `VARYZ_PADDLE_API_KEY_SANDBOX` and `VARYZ_PADDLE_CLIENT_TOKEN_SANDBOX`. An earlier key, "varyz-cloud-server (sandbox)", was never stored and should be revoked. After a redeploy:
- `/checkout` returns 200.
- The server's key reads both prices as active and recurring with no trial (USD 5 a month, USD 48 a year).
- An API-created probe transaction (`txn_01m40me23ksae8n4fpvz6amb47`, a draft that was never paid) received a checkout URL on our page.
- Paddle.js opened the Test Mode overlay. It showed USD 5.00 including VAT because the prices were then "Automatic based on location". The owner chose USD 5 plus tax, so both sandbox prices are now "Excludes tax" (`tax_mode: external`, confirmed through the API). The owner made the same change to the live prices the same day. This is not confirmed through the API, because no live key is stored.

Not yet run: a full sandbox purchase from a signed-in account, then the webhook, entitlement, renewal, cancellation and refund.

## Transactional email (Resend) — 3 October 2026

Hetzner blocks outbound ports 25 and 465, so the server does not send mail itself; it relays through Resend (free tier, 100 emails/day, matching the deployment cap). The domain `varyzai.com` is verified in Resend, region Ireland (eu-west-1), with open/click tracking off. Cloudflare DNS-only records: CNAME `rsend` → `rsend-euw1.forge.rmta.net`, CNAME `send` → `send.forge.rmta.net`, TXT `resend._domainkey` (DKIM) and TXT `_dmarc` = `v=DMARC1; p=none;`. The server uses `smtp.resend.com:587` with STARTTLS, user `resend`, and a sending-only API key named "varyz-cloud-smtp" as the password. All five `VARYZ_CLOUD_SMTP_*` values are in Credential Manager (service `varyz-cloud`); the sender is `Varyz <no-reply@varyzai.com>`. A test sent from inside the container was accepted, Resend shows it as Delivered, and Gmail put it in the inbox rather than spam. Once mail has run cleanly for a while, tighten DMARC from `p=none` to `p=quarantine`.

## Live payments — 7 October 2026

Owner completed Paddle identity verification and payouts. Created in the live dashboard: API key "varyz-cloud-server (live)" (prices:read, subscriptions:read/write, transactions:read/write; expires 2027-01-05, replace before then) and client-side token "varyz-cloud-checkout (live)". The owner stored both in Credential Manager (`varyz-cloud` / `VARYZ_PADDLE_API_KEY`, `VARYZ_PADDLE_CLIENT_TOKEN`); values were never shown to the agent. The live API confirms both prices are active, USD 5 monthly and USD 48 annual, `tax_mode: external`. The key cannot read notification settings (403, by design), so the webhook state is the dashboard check of 3 October. That first live deploy omitted `--archive`, and the env rewrite dropped the server-generated `VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY`, so every signed-in page returned 500 (`Archive encryption key unavailable`). The owner restored the key from the off-host backup copy (`.deploy-secrets/cloud-backup/archive-key.age`, checksum equal to the server's), and production was redeployed with `uv run python deploy/cloud/deploy.py --host 2.31.3.96 --live --archive`. deploy.py now carries server-only values over when it rewrites cloud.env and refuses to deploy without `--archive` while the running image has archive search. **Always deploy production with `--live --archive`.** Remaining: one real purchase, cancel and refund by the owner.

