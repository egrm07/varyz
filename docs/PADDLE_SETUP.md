# Paddle setup

> **STATUS 2026-10-07: live payments on.** https://cloud.varyzai.com runs with live Paddle (deployed with `--live` on 7 October; `/checkout` loads Paddle.js with the live client token). Identity verification and payouts were completed by the owner. Live API key "varyz-cloud-server (live)" (prices:read, subscriptions:read/write, transactions:read/write; expires 2027-01-05, replace before then) and client-side token "varyz-cloud-checkout (live)" are in Credential Manager as `VARYZ_PADDLE_API_KEY` and `VARYZ_PADDLE_CLIENT_TOKEN` (service `varyz-cloud`). **Prices changed 7 October 2026** (owner decision): new live prices USD 12/month and USD 108/year, tax included (`tax_mode: internal`), with country prices EUR 12/108 in the 36 euro countries and territories and GBP 9.99/89.99 in GB, GG, IM and JE; automatic currency conversion is on for every other country. Verified through `/pricing-preview`. The USD 5/48 prices are retired from the server's allowlist but not archived in Paddle. The server code and `paddle-catalog.json` point at the new IDs; deployed to production on 7 October 2026; `/api/pricing` returns USD 12/108, EUR 12/108 and GBP 9.99/89.99 by country. Update 2026-10-12: the owner confirms payments were tested and work. Payments are no longer an open launch gate; individual cancellation/refund scenarios are not newly claimed as independently retested by this agent. Sandbox acceptance: reports/CLOUD_TESTS_2026-10-03.md and reports/CLOUD_PAYMENTS_2026-10-07.md.

## Catalog

Verified through the live API on 7 October 2026. Base currency USD, quantity 1, **no trial**, tax included. Machine-readable IDs and the country list: [paddle-catalog.json](../deploy/cloud/paddle-catalog.json).

| Item | Paddle ID | Amount |
| --- | --- | --- |
| Varyz Cloud product | `pro_01m3zc5xyfqtqe4atdk2gbcvy4` | SaaS tax category |
| Monthly | `pri_01m4bjwrbm5xthqfg7b65cembj` | USD 12 / EUR 12 / GBP 9.99 a month |
| Annual | `pri_01m4bk3rzq586bxsm9rfvr4nfw` | USD 108 / EUR 108 / GBP 89.99 a year |
| Retired monthly | `pri_01m3zc9asv9rqnx7scswsm3pnn` | USD 5/month, plus tax |
| Retired annual | `pri_01m3zcbac30bq4n1eybz9gpxxy` | USD 48/year, plus tax |

How local prices reach buyers: the account page asks Paddle's `/pricing-preview` with the visitor's IP, and checkout creates the transaction with that `currency_code` (a transaction's currency is fixed at creation; with EUR and no address Paddle converts USD 12 to about EUR 10.73, then applies the EUR 12 country price once checkout has the buyer's address). varyzai.com gets country prices from `GET https://cloud.varyzai.com/api/pricing?country=XX`. Both API keys lack price-write permission, so price changes are made in the dashboard.

**Sandbox catalog (3 October 2026; still the old USD 5/48 plus-tax prices, not mirrored).** A separate sandbox account now exists; `cloud.varyzai.com` is approved for checkout there. Same product and prices, no trial, Account default tax:

| Sandbox item | Paddle ID | Amount |
| --- | --- | --- |
| Varyz Cloud product | `pro_01m40gvq8xmmzwgmatf63e8h29` | SaaS tax category |
| Monthly | `pri_01m40gx74qaya1j3dqmm9arn19` | USD 5/month |
| Annual | `pri_01m40gy1exacg735cmyk2y8ccw` | USD 48/year |

Automatic approval review rejected explicitly changing tax handling to Excludes tax because the owner had not chosen that treatment. The attempted change was not saved; prices retain **Account default**. Confirm the account tax treatment during actual checkout testing.

## Owner steps

1. ~~Sign into the separate Paddle sandbox account.~~ Done 3 October 2026 (live credentials do not work there; it is a separate signup). Still to do in sandbox: default payment link `https://cloud.varyzai.com/checkout`, webhook destination, API key and client token.
2. ~~Identity verification~~ Done by the owner 7 October 2026. In the [live onboarding page](https://vendors.paddle.com/onboarding/get-started), complete **Verify your account → Complete identity checks**. The dashboard now asks for identity verification through Sumsub. This supersedes the earlier step-4 handoff; onboarding completion does not mean merchant approval.
3. ~~Payouts~~ Done by the owner 7 October 2026. Complete [payout settings](https://vendors.paddle.com/payouts/settings) personally: accurate account type, representative details and payout method/details. The form currently defaults to Payoneer and a USD 100 threshold; neither has been submitted by the agent. Do not put credentials or bank details in chat.

## Integration preparation

- Runtime env template: [deploy/cloud/.env.example](../deploy/cloud/.env.example). Keep sandbox mode true while testing and use sandbox IDs, never live catalog IDs.
- Runtime API key needs price reading, transaction creation and subscription cancellation permissions. Create narrowly scoped keys only after the owner authorizes new access. Store API keys and webhook signing secrets outside Git; client-side tokens are public but must match their environment.
- Dedicated payment-link page: `/checkout`. Configure it as Paddle's default payment link after the correct domain/page is reachable and approved. The public launch homepage does not load Paddle.js and is unsuitable as a payment link.
- `/checkout` uses the official Paddle.js overlay. Script permissions are isolated to this route; other account pages retain their script-free policy. Missing or mismatched client tokens disable checkout. Transaction creation passes this page as `checkout.url`.
- Webhook destination: `https://cloud.varyzai.com/api/billing/webhook` **only after backend deployment**. Listen to subscription lifecycle events, `transaction.completed`, `adjustment.created` and `adjustment.updated`. A local development URL cannot receive Paddle's webhook deliveries directly. The live destination was created 3 October 2026 (12 events, active); its secret is in Windows Credential Manager (`varyz-cloud` / `VARYZ_PADDLE_WEBHOOK_SECRET`). The live API key and client token were created 7 October 2026 (see the status note).
- Entitlement remains driven by signed notifications, never the browser success screen. Approved full refunds remove the latest paid period's access; refunding an older transaction does not revoke a later paid renewal. Pending, rejected, partial refunds and credits do not automatically revoke access. Partial refunds require case-specific handling before offering them.
- Ordinary cancellation requests the next billing period; account deletion cancels immediately before removing account data. Export remains available without paid access.

## Required sandbox acceptance checks

Run with isolated test accounts/data and official sandbox cards; capture actual transaction IDs and notification delivery results. Executed 3 and 7 October 2026; see the reports named in the status note above.

| Scenario | Required outcome |
| --- | --- |
| Monthly and annual checkout | Correct USD amount, tax/final total displayed, no trial; signed activation opens sync |
| Browser completion without a verified webhook | No paid entitlement granted |
| Renewal | New completed transaction recorded; paid access retained |
| Cancellation | Future renewal canceled; current paid access retained until signed cancellation takes effect |
| Full refund of current payment | Pending approval retains access; approved adjustment ends access for that period |
| Refund of older payment after renewal | Newer paid period retains access |
| Duplicate/out-of-order notifications | No duplicate provisioning or stale entitlement reversal |
| Failed payment | Past-due lifecycle removes paid access; export and recovery remain available |
| Account deletion | Billing cancellation confirmed before cloud preferences/account deletion |

Local verification: **44 tests passed, 10 real PostgreSQL tests deselected**; Ruff passed. Browser review covered the checkout shell and refunded-account controls at desktop and phone widths. The preview omitted Paddle.js and did not test payment collection, overlays or provider delivery. Container execution, real PostgreSQL, SMTP and hosting remain staging gates.

Official references: [sandbox](https://developer.paddle.com/sdks/sandbox/), [default payment link](https://developer.paddle.com/build/transactions/default-payment-link/), [completed transactions](https://developer.paddle.com/webhooks/transactions/transaction-completed/), [refund creation](https://developer.paddle.com/webhooks/adjustments/adjustment-created/), [refund approval updates](https://developer.paddle.com/webhooks/adjustments/adjustment-updated/).
