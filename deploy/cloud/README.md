# Varyz cloud container

**Run the paid cloud API as one persistent SQLite instance for private validation; select public hosting after reviewing `docs/CLOUD_HOSTING.md`.** The staging server is Hetzner `varyz-cloud` (`2.31.3.96`, `cloud.varyzai.com`), built from [cloud-init.yaml](cloud-init.yaml) and updated with `uv run python deploy/cloud/deploy.py --host 2.31.3.96`; details in [CLOUD_HANDOFF.md](../../docs/CLOUD_HANDOFF.md). Paddle products exist; payments stay in sandbox until the acceptance checks pass.

## Private quickstart

From the repository root, with Docker Engine running:

```powershell
docker compose -f deploy/cloud/compose.yaml build
docker compose -f deploy/cloud/compose.yaml up -d
Invoke-RestMethod http://127.0.0.1:8080/health
```

The default binds only to loopback. `/health` confirms the app responds; it does not prove OAuth, payment processing, backup recovery or public readiness. Billing configuration is absent by default; no entitlement or trial is granted. Configure a Paddle sandbox and **paid recurring prices without trials** for end-to-end checkout testing.

The Dockerfile installs package source plus the `cloud` optional dependencies only. It does not install local extraction models or connect an owner's archive. `Dockerfile.dockerignore` is a context allowlist; config, data, reports, Git metadata, `.env` and database files cannot enter the build context. Always build with this Dockerfile, not a generic root-context Dockerfile.

## Configuration

| Variable | Value and purpose |
|---|---|
| `VARYZ_CLOUD_DATABASE` | SQLite path or PostgreSQL URI; base Compose uses `/var/lib/varyz-cloud/cloud.sqlite3` |
| `VARYZ_CLOUD_PUBLIC_URL` | Exact external HTTPS origin for OAuth discovery and redirects; local HTTP accepted only on loopback |
| `VARYZ_PADDLE_SANDBOX` | `true` for isolated billing tests; change to `false` only for approved live deployment |
| `VARYZ_PADDLE_CLIENT_TOKEN` | Public Paddle.js token matching the selected environment; checkout stays unavailable without it |
| `VARYZ_PADDLE_API_KEY` | Billing provider secret; never embed in the image |
| `VARYZ_PADDLE_WEBHOOK_SECRET` | Secret used to authenticate billing events |
| `VARYZ_PADDLE_PRICE_IDS` | Comma-separated allowed recurring paid price IDs |
| `VARYZ_CLOUD_SMTP_HOST`, `VARYZ_CLOUD_SMTP_PORT` | Verification/reset mail server; port defaults to 587 |
| `VARYZ_CLOUD_SMTP_USERNAME`, `VARYZ_CLOUD_SMTP_PASSWORD`, `VARYZ_CLOUD_SMTP_FROM` | Mail authentication and verified sender; secret manager injection |
| `VARYZ_CLOUD_POSTGRES_DSN` | External PostgreSQL URI read by the optional Compose override |
| `VARYZ_CLOUD_DB_POOL_MAX` | PostgreSQL pool connections per process, default 8 (allowed 1–64); reserve DB capacity for replicas/deploy overlap |
| `VARYZ_CLOUD_STORAGE_LIMIT_BYTES`, `VARYZ_CLOUD_CHANGE_LIMIT` | Per-account conservative cumulative history admission limits, defaults 50 MiB and 100,000 changes |
| `VARYZ_CLOUD_SYNC_WAITERS` | Sync requests held open at once by devices waiting for another device's change (`GET /api/sync?wait=`, up to 25 s), default 8, at most 4 per account; past it a device is answered at once and polls. Keep it well under Uvicorn's `--limit-concurrency 32` |
| `VARYZ_CLOUD_ADMIN_EMAILS` | Comma-separated verified account emails that can open `/admin`; empty means nobody, and everyone else gets a 404 |
| `VARYZ_CLOUD_CREATORS_OPEN` | `true` to accept Creator Program applications from the start, default `false`; the admin page can open or close them later |
| `PORT` | HTTP listen port inside the standalone image, defaults to 8080; Compose expects this default |

Set secrets using the platform secret manager or an untracked environment file **outside the repository**. To load an external file locally, pass `--env-file C:/private/varyz-cloud.env` before `-f`. `deploy/cloud/.env.example` contains variable names, not credentials. Check the file's ACLs; rendered Compose config and container inspection can expose supplied secrets.

## Optional improvement analytics

Set `VARYZ_ANALYTICS_ENABLED=true` and a random `VARYZ_ANALYTICS_ADMIN_TOKEN` in the
root-only external environment to enable aggregate receipt. `/api/analytics` accepts
strict reports without accounts or billing; `/api/analytics/summary` requires the owner
token as a Bearer header. Store that token in the owner's OS credential store under
`varyz-cloud` / `VARYZ_ANALYTICS_ADMIN_TOKEN` for `varyz analytics report`. Consent is
separate, default-off local setup state. See `docs/ANALYTICS.md` for retention and limits.

## Persistence and backups

**The `cloud-state` named volume survives ordinary container replacement.** `docker compose down` preserves it; `down --volumes` deletes it. Keep a stable Compose project name/directory so replacement deployments attach the same volume. Existing bind mounts need write permission for UID/GID 10001; Compose's fresh named volume initializes from the image directory ownership.

Use SQLite's backup API, not a live file copy that may miss committed WAL data. The following writes a consistent snapshot inside the volume, then copies it onto the host:

```powershell
docker compose -f deploy/cloud/compose.yaml exec -T cloud python -c "import sqlite3; source=sqlite3.connect('/var/lib/varyz-cloud/cloud.sqlite3'); target=sqlite3.connect('/var/lib/varyz-cloud/cloud-backup.sqlite3'); source.backup(target); target.close(); source.close()"
docker compose -f deploy/cloud/compose.yaml cp cloud:/var/lib/varyz-cloud/cloud-backup.sqlite3 C:/private/varyz-cloud-backup.sqlite3
```

Protect backup credentials and encrypt/store the host snapshot off-host with retention controls. Test restore into a different isolated volume and verify accounts, credentials, sync cursors, tombstones and entitlements. A snapshot in the same volume is not disaster recovery. These commands are a manual staging example; a scheduled encrypted backup/restore system remains a production gate.

## Public deployment and scaling limits

Place TLS at a trusted edge, configure the external HTTPS origin, and keep the container port private. Disable query-string and request-body logging at the edge as well.

**Proxy trust is scoped to the edge.** Uvicorn runs with `--proxy-headers` and reads `X-Forwarded-For` only from connections whose address is in `FORWARDED_ALLOW_IPS`. The image defaults to `127.0.0.1`. Compose attaches the service to a fixed `edge` network (`172.31.250.0/24`) and trusts only its gateway `172.31.250.1`, which is the address a host-installed Caddy uses to reach the published port. Each visitor then gets their own rate-limit bucket instead of all sharing the proxy's. Set `VARYZ_CLOUD_FORWARDED_ALLOW_IPS` to change the trusted address; never set it to `*` while the port is reachable by anything but the edge. The edge must overwrite, not append to, any client-supplied `X-Forwarded-For` (Caddy's `reverse_proxy` does by default).

**Paddle checkout page.** Set the Paddle default payment link to `https://cloud.varyzai.com/checkout` and add `cloud.varyzai.com` to the approved domains, in both sandbox and live. The app accepts only provider checkout URLs that point at that page (`/checkout?_ptxn=txn_…`); anything else fails with 502 instead of redirecting the owner elsewhere.

**SQLite: one service, one worker, one volume.** Do not use Compose `--scale`, rolling replicas or an ephemeral container filesystem for the SQLite backend. Cloud Run is not compatible with this persistence layout. Render disk mode is single-instance. A VPS can run this private validation layout but requires OS, TLS and backup operations. The container bounds Uvicorn concurrency at 32 and backlog at 64; saturated requests may receive 503 before application admission. Long-polling devices hold up to `VARYZ_CLOUD_SYNC_WAITERS` of those 32 for up to 25 s each. The edge must not time out a request sooner: Caddy's `reverse_proxy` has no read timeout by default.

An optional PostgreSQL adapter is implemented in `src/varyz/cloud/postgres.py`. The factory selects it when `VARYZ_CLOUD_DATABASE` starts with `postgres://` or `postgresql://`. To use an **already created isolated test database**, set the DSN in your external environment file and run:

```powershell
docker compose --env-file C:/private/varyz-cloud.env -f deploy/cloud/compose.yaml -f deploy/cloud/compose.postgres.yaml up --build -d
```

This command creates no PostgreSQL service or provider resource. It preserves the base Compose volume but the PostgreSQL backend does not use it. Use provider-required verified TLS or a trusted managed connector; restrict the DB network and create a dedicated least-privilege application role. Schema setup currently uses idempotent DDL at startup (including the email verification column), not a versioned migration framework. An empty database is supported; importing an existing SQLite cloud database is a separate migration.

The shared backend uses tenant predicates, foreign-key deletion cascades, atomic sync batches and a transactionally locked cursor clock. A PostgreSQL sequence alone would let a later cursor commit first and be skipped by a subsequent pull. The clock intentionally serializes sync writes; benchmark contention before increasing replicas. Credentials and billing replay state share the database; the app's single-use critical sections use transaction advisory locks.

Preference storage accounting is shared and committed with the sync batch. Each new history item consumes an estimated `2 × UTF-8 payload bytes + 512` bytes and one change; stale retries with identical payload consume neither. A batch that exceeds either account limit rolls back fully. The estimate is an admission quota, **not the actual PostgreSQL disk size or a service-wide disk cap**. Retained history means tombstones also consume quota; full account export/deletion remain separate APIs. The checkout reservation table also shares account state so concurrent requests cannot silently create multiple new payment transactions. Verify these paths with the real-database gates before production.

Shared fixed-window request limits now live in the database, using hashed keys and bounded expired-row cleanup. PostgreSQL pools have minimum 0 and default maximum 8 connections, a maximum 32 waiters and a ten-second acquisition deadline; each replica owns its own pool. Database statement and lock timeouts are 15 and 10 seconds. The app closes the pool at shutdown. [Psycopg pool behavior](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).

**Real-database concurrency and burst testing are still required before horizontal scaling.** This override deliberately keeps one application worker. `docs/CLOUD_HOSTING.md` proposes provider options and capacity controls; it intentionally does not provide a command that purchases a managed service.

## Google and GitHub sign-in

Provider icons remain visible, with a disabled coming-soon state until both
the client ID and secret are configured. Configured providers become active links.
Register a Google **Web application** OAuth client and a GitHub **OAuth App**
with homepage `https://cloud.varyzai.com` and these exact callback URLs:

| Provider | Callback | Credential Manager entries (service `varyz-cloud`) |
| --- | --- | --- |
| Google | `https://cloud.varyzai.com/auth/google/callback` | `VARYZ_CLOUD_GOOGLE_CLIENT_ID`, `VARYZ_CLOUD_GOOGLE_CLIENT_SECRET` |
| GitHub | `https://cloud.varyzai.com/auth/github/callback` | `VARYZ_CLOUD_GITHUB_CLIENT_ID`, `VARYZ_CLOUD_GITHUB_CLIENT_SECRET` |

Store the four values privately in Windows Credential Manager; the deployment
helper sends them over SSH into the root-only runtime environment. Do not paste
secrets into chat, commit them, or put them in browser URLs. Redeploy after saving
them. Direct deployments can set these same environment variables instead.
Google testing mode restricts sign-in to the registered test users until the
consent screen is published. Verify a real sign-in for each provider after setup.

Google requests only `openid email`; GitHub requests `read:user user:email`.
The callback uses PKCE, single-use browser-bound state and verified email from
the provider API. Provider subjects identify accounts; tokens stay in memory.
New users must choose a unique username. Existing email accounts must first
sign in with email and explicitly connect the provider from the account page.
No Apple provider is included. Protocol references:
[Google](https://developers.google.com/identity/openid-connect/openid-connect),
[GitHub](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

Browser sessions use a Secure, HttpOnly, SameSite=Lax cookie for 30 days, backed
by a hashed, expiring server credential. Restarting the browser preserves sign-in;
logout, credential revocation, expiry and password reset invalidate access.

## Acceptance evidence

On 2026-10-03 the Hetzner deployment at `https://cloud.varyzai.com` passed 20 live
HTTPS acceptance checks, official MCP SDK transport checks, restart persistence,
a restored SQLite snapshot and a bounded 40-request burst. Actual Paddle sandbox
monthly/annual purchases, cancellation, full refund replay and signed renewal/failure
simulations were exercised. Eleven concurrency/ledger tests passed against a disposable
PostgreSQL 17 instance; the deployed backend remains SQLite. See
`reports/CLOUD_TESTS_2026-10-03.md` for evidence and limitations. Phone interoperability,
inbox delivery and live billing remain unverified. Scheduled encrypted off-host
backup recovery was subsequently verified in the security rollout described below.

Docker build and runtime require a running Docker daemon. Syntax/configuration validation alone does not establish the image starts, retains data across replacement or interoperates with platform connectors. Before public launch, build the image, complete sandbox OAuth/checkout/webhook verification, test persistence/recovery and measure a burst with synthetic accounts. Pin the base image digest for the chosen release after the smoke build succeeds.

Python dependencies are already pinned: `deploy/cloud/requirements.txt` holds the `cloud` extra at the `uv.lock` versions with hashes, and the image installs it with `--require-hashes`. Regenerate it after changing the `cloud` extra or `uv.lock`:

```powershell
uv export --frozen --extra cloud --no-hashes --no-emit-project -o $env:TEMP/lock-constraints.txt
uv run python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['cloud']))" > $env:TEMP/cloud.in
uv pip compile $env:TEMP/cloud.in -c $env:TEMP/lock-constraints.txt --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header --no-annotate -o deploy/cloud/requirements.txt
```

Then restore the three header comment lines at the top of the file.

The PostgreSQL concurrency gates require an explicitly supplied disposable database. They create/drop only randomly named test schemas:

```powershell
$env:VARYZ_TEST_POSTGRES_DSN = 'postgresql://test-role:password@localhost/test-database'
uv run pytest -m live tests/test_cloud_postgres.py
```

With no DSN, those tests skip. A skipped test is not evidence of correct PostgreSQL concurrency. The offline SQL placeholder boundary test runs in the normal test suite.

## Security rollout and recovery

The 2026-10-03 security release passed 25 live adversarial checks and 163 local
tests. HTTPS browser cookies now use `__Host-` names, so existing browser users
must sign in again once. Destructive browser actions, provider linking and local
client authorization require authentication within the last 15 minutes. Refreshing
a connector token preserves its original authentication time. Routine sync remains
available with valid older credentials.

For a code-only deployment that preserves the server's existing secrets and billing
mode, use `deploy.py --keep-env` with the normal host arguments. It requires an
existing runtime environment and cannot be combined with `--live`. SSH host keys
must already be verified. The deployed billing mode remains sandbox.

The server's `varyz-cloud-backup.timer` creates a consistent SQLite backup daily at
02:00 UTC, with up to ten minutes of randomized delay. Backups are encrypted using
age to a public recipient; the private identity stays on the owner's computer.
Thirty daily encrypted snapshots are retained in `/var/backups/varyz-cloud` and
in `.deploy-secrets/cloud-backup/snapshots` on that computer. Windows task
`VaryzCloudBackup` pulls hourly while the owner is logged in and the network is
available, verifies the checksum, decrypts into a protected temporary file, checks
SQLite integrity and table counts, removes plaintext and checks HTTPS health.
`status.json` and `runs.jsonl` in its protected state directory record the outcome.
Backups older than 48 hours fail the local verification. Notification delivery is
not yet configured; off-host copies depend on this computer being available.

Pause replication by creating `.deploy-secrets/cloud-backup/PAUSED`; pause server
snapshots by creating `/etc/varyz-cloud/BACKUP_PAUSED`. Remove the respective file
to resume. Disable the Windows task or stop/disable the systemd timer to uninstall
scheduling; retain the identity and encrypted snapshots for recovery.

To verify recovery manually, run `scripts/cloud_backup_pull.py --state` with the
absolute protected state directory under the owner's Windows account. For disaster
recovery, decrypt a retained `.sqlite.age` using that directory's `identity.txt`
with `age --decrypt --identity identity.txt --output recovered.sqlite backup.sqlite.age`.
Run SQLite `PRAGMA integrity_check` before replacing data. Stop the app, preserve
the current volume, install the verified database as UID/GID 10001 with mode 0600
in a 0700 directory, then start the app and verify account access and sync. Protect
and separately preserve the private identity: ciphertext alone cannot restore data.

The dated release report contains the rollback image tag, pre-release snapshot,
verification logs and remaining account/MFA decisions:
`reports/CLOUD_SECURITY_DEPLOY_2026-10-03.md`.

## Signup abuse protection

Email signup creates a hashed, one-hour `pending_signups` request, not an account.
The confirmation POST consumes its token and creates the verified account within
one transaction, rechecking account capacity and username availability. Pending
requests do not reserve usernames or occupy the account limit. Repeating signup
cannot change a pending request's password/username or invalidate its existing link;
account recovery can reissue verification within its quota.

Signup checks syntax, public domain names and DNS mail deliverability using
`email-validator`/`dnspython`. Nonexistent and null-MX domains are rejected; a
temporary DNS failure returns 503 for retry. DNS caching is bounded to 1,000 entries
and individual query lifetimes to two seconds. Only the domain is queried; full
addresses are not sent to an email-validation service. A packaged 9,203-domain CC0
blocklist rejects known disposable providers and their subdomains. Its source
revision, SHA256 and license are in `src/varyz/cloud/disposable_domains.*`.
Update that reviewed snapshot as disposable providers change; a live feed is not
trusted automatically. Legitimate custom domains are allowed even when their names
look unusual. Existing verified accounts retain login/recovery access.
International email domains are normalized consistently at signup, login and recovery.

Signup-specific ceilings are 10 attempts per minute and 60 per hour service-wide,
10 per IP per hour and 3 per normalized email per hour. The separate pending queue
holds at most 500 requests. These fixed-window limits can admit bursts at boundaries.
The existing auth-request and daily email limits also apply. Rate-limit keys are
hashed. SQLite cleanup uses absolute window starts so short-window requests cannot
erase hourly counters, and removes expired pending rows during normal admission.
PostgreSQL also removes expired pending rows on signup/recovery.

The deployed account limit remains 100 and the email budget remains 100/day.
These conservative limits should be revisited with measured demand. CAPTCHA,
mailbox-farm detection and a guarantee of spam-free registrations are not included.
DNS proves a mail-capable domain; the confirmation link proves mailbox access.
The signup release passed 17 deployed signup checks and the previous 25 live
security checks. See `reports/CLOUD_SIGNUP_SECURITY_2026-10-03.md`.


## Optional Cloud archive — 2026-10-05

Base builds include encrypted keyword retrieval. HTTPS archive uploads fail closed
without `VARYZ_CLOUD_ARCHIVE_ENCRYPTION_KEY` (Fernet URL-safe key in a restricted,
untracked deployment secret). The default `VARYZ_CLOUD_ARCHIVE_LIMIT_BYTES` is
104857600 per account. Never rotate/remove the key without migrating and verifying
existing snapshots. Back up the key separately from encrypted database copies.

For hybrid retrieval, add `-f deploy/cloud/compose.archive.yaml` alongside the base
Compose file. This explicitly builds the hash-pinned optional ONNX runtime and
provisions public weights into `/opt/varyz/models`. The image has no private
archive; request-time inference only uses those local weights. Regenerate pinned
requirements with `python scripts/export_cloud_requirements.py` after lock changes.

Do not enable production archive uploads with the current 30-full-local-backup
policy at the 100-account limit. Establish bounded encrypted local retention,
verified off-host backups and restores, disk alerts and disk-based snapshot staging
before enabling content. Container builds, actual server load and real phone
integration were not verified in this build. See `docs/CLOUD_ARCHIVE.md`.


## Tested context rollout — 2026-10-05

Use `python deploy/cloud/deploy.py --host 2.31.3.96 --keep-env --archive` to retain
billing/secrets, build cached model weights and verify the isolated candidate before
activation. `--stage-only` verifies without switching the live service. Activation
keeps a rollback image and restores it if health fails. The archive secret is created
only if missing, in a root-only server environment; encrypted recovery uses the existing
backup destination. `scripts/cloud_context_live_probe.py` exercises HTTPS with
synthetic accounts and cleanup; `cloud_context_sdk_probe.py` verifies the official SDK.
Do not print intermediate credentials or rendered Compose environment values.

The private deployed image is `a7bcbafe0af4`; source code remains unreleased 0.1.0.
See `../../reports/CLOUD_CONTEXT_DEPLOY_2026-10-05.md` for tests, capacity and limitations.
