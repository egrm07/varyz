# Cloud retention preparation

> **STATUS 2026-10-12: activated with owner approval.** A fresh successful backup preceded Cloud recreation using the identical image, environment and state-volume mount. Journald now expires at 28 days with daily rotation; rsyslog rotates daily with 28 archives and maximum archive age 28 days. No account records were deleted.

## 1 — Proposed security log policy

Target ordinary security logs at **30 days or less during normal operation**. Configure journald for 28 days with daily file rotation, and rsyslog for daily rotation with 28 archives and a 28-day maximum archive age. Rotation granularity and outages prevent promising exact per-entry deletion times. Verify actual oldest entries after activation and after the first scheduled rotations before publishing a maximum-age claim.

`journald.conf` is a host-wide drop-in candidate. `rsyslog` replaces the host's existing `/etc/logrotate.d/rsyslog`; never install both copies for the same paths. The inventory matches the 2026-10-12 read-only inspection; recheck it before activation. Preserve existing files as root-only rollback copies. Check later-sorting journal drop-ins for conflicting values.

The container override `../compose.retention.yaml` routes future container output to the bounded journal. It explicitly clears inherited JSON logging options. Run `docker compose ... config --quiet` with the actual Compose files first. Recreate only the Cloud container after a successful backup, preserving the named state volume and all server-only environment settings. Confirm healthy service, billing mode and OAuth after recreation. Do not rebuild or deploy the whole dirty working tree merely to change logging.

The old container had ten JSON log entries from 2026-10-12; normal container recreation removed its old Docker log files. No active files were truncated and no volumes were pruned. Future output uses journald. Historical host journal entries remained within the new window: the oldest inspected entry was 9.46 days old.

The active Compose override is `/etc/varyz-cloud/compose.retention.yaml`, outside replaceable source checkouts. `deploy.py` now detects and preserves that activated override on deployments and rollback. Manual Compose commands must include it after the base and archive overrides. Do not replace the live environment or rebuild the image merely to change retention. Root-only prior configuration copies are in `/etc/varyz-cloud/retention-rollback-20261012`.

Activation introduces automatic irreversible log expiry and needs the owner's explicit approval. Validate the candidate logrotate file with `logrotate --debug` before installing it. The stanza explicitly preserves the host's `su root adm` and `create` defaults, including when validated independently. After approval, verify the effective journal configuration, daily logrotate timer, container logging driver and oldest retained timestamps. Keep success/failure receipts without recording log contents, tokens or addresses. A stalled rotation or restart leaves the policy unfinished.

## 2 — Financial records

Separate service entitlement rows from accounting records. Current `delete_owner` removes account-linked payment/refund rows after confirmed cancellation; this behavior must not be silently changed into long-term retention of deleted accounts.

Dutch official guidance describes seven years for ordinary business administration, with ten-year exceptions for specified records. This is a research baseline, not confirmation of the owner's tax establishment or applicable exception. Identify the actual seller/accounting entity and its existing accounting storage before settling that schedule. Paddle's merchant-of-record records and the owner's payout/invoice accounting are separate from the Varyz account database. Do not assume Paddle's retention substitutes for the owner's accounting obligations.

Prepare a minimal invoice/payout record inventory, restricted storage, export/restore check and due-date schedule with the actual accounting arrangement. No customer messages, preferences or history belong in that financial archive. Align the public privacy wording only with the verified arrangement; do not promise erasure from third-party financial records or claim that an unimplemented archive exists.

## 3 — Evidence and next steps

- Production read-only baseline: `reports/release-readiness-2026-10-12/production-ops-readonly.log`.
- Live billing and SMTP configured; owner confirmed payments work. Recovery email reached the owner's Gmail INBOX on 2026-10-12 at 20:17 UTC; no password changed.
- Native Compose/logrotate validation passed. Owner approved expiry and restart. Activation and fresh verification receipts are `retention-activation.log` and `retention-verification.log` in the report directory above; health, recovery and OAuth discovery returned HTTP 200. Daily logrotate and backup timers were active. Continue checking scheduled rotation during operations; financial retention remains separate.
- Public policy source is `docs/PRIVACY.md`; generated page is `website/privacy.html`. Local corrections are not proof of public deployment.

Sources: [systemd journal configuration](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html), [Docker journald driver](https://docs.docker.com/engine/logging/drivers/journald/), [Compose merge and reset](https://docs.docker.com/reference/compose-file/merge/), [Dutch official business-record guidance](https://business.gov.nl/running-your-business/administration/how-to-keep-business-records/).
