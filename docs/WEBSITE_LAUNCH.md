# Public website and Paddle domain review

> Prepared 2026-10-02. Owner says varyzai.com is not hosted. Hetzner, cloud.varyzai.com, USD 5/month and USD 48/year with no trial are approved. Owner approved a 14-day refund window after every payment and permits the existing main email until a domain mailbox is available.

## Current owner decision

The owner explicitly said not to publish yet because the repository is private. Keep the website as a local preview with Launching soon messaging. Do not upload assets, deploy, purchase a server or submit Paddle review. Free website hosting is the eventual plan; backend purchase is deferred. Obtain renewed publication authorization after the owner has reviewed the edits.

## Prepared

The established dark/marigold Varyz branding, bird wordmark and self-hosted fonts are preserved. No analytics or tracking was added. The homepage now links pricing, terms, privacy, refunds and contact. Cloud is labeled coming soon, with no enabled checkout.

Canonical service copy is in docs/website/*.md. Privacy is generated from docs/PRIVACY.md. Run `.venv/Scripts/python.exe scripts/site/build_site.py --no-images` to regenerate pages, navigation and sitemap. Do not edit generated legal HTML directly.

The static website is prepared for free Cloudflare Pages hosting, using website/ only and no analytics. Cloudflare is signed in and owns the varyzai.com zone. A website-only ZIP is prepared in .tmp/cloud-qa/varyz-website.zip; it contains no repository, archive or credentials. The site can alternatively run on the same Hetzner host as cloud using deploy/cloud/Caddyfile.site. Neither is deployed yet.

The actual Hetzner order screen quotes EUR 8.58/month including the account's 21% VAT for CX23 (2 vCPU, 4 GB, 40 GB), Falkenstein, Ubuntu 24.04, IPv4 and daily backups. EUR 7.25 without server backups still requires separate database backups before accounts open. No order placed. The owner chose free website hosting first and deferred backend purchase, then asked to keep the website unpublished for further edits. Do not treat the earlier hosting preference as final permission for the current pending SSH-key/order-terms confirmation.

## Publication gates

- Owner supplied Egor Menshikov and Anthony Fokkerlaan 3, 2024 JA Harlem. The terms/contact/privacy use the supplied city spelling and Netherlands postcode country. Confirm spelling if corrected; main email use is approved.
- Configure the Hetzner account/server and DNS. Copy only website/ into `/srv/varyz/website`; never upload the repository, archives, secrets or private documentation as the web root.
- Confirm the live infrastructure region, SMTP provider, backup location and retention periods. The privacy page must name actual processing before accounts open.
- Review final rendered pages and resolve draft language. Publication authorization was withdrawn on 2 October 2026. Keep this local until the owner explicitly requests publication again.
- Validate HTTPS and every footer/legal link after deployment. Confirm contact mailbox delivery.
- Implement and test the approved 14-day refund-after-each-payment procedure in Paddle and Varyz entitlement handling before paid launch.
- Submit varyzai.com to Paddle website approval. Submit cloud.varyzai.com separately before launching checkout there. Domain approval is not implied by publishing.

Paddle requires HTTPS, clear product/features/pricing, accessible terms/privacy/refunds, and supplier identification. Its domain review may request more information or a test account. Source: https://www.paddle.com/help/start/account-verification/what-is-domain-verification

## Alpha copy and Paddle progress

Owner requested Alpha launching soon, your vision and creative perspective, and Open source in the hero facts. Homepage, generated footer/status, social metadata and social image reflect this. Site remains unpublished.

Paddle product description was submitted with explicit owner confirmation of the AUP declaration. Onboarding reached step 3 (website verification). Planned varyzai.com pricing/terms/privacy/refunds URLs are filled as a draft, but Continue was not clicked because these pages are not publicly hosted. Resume after the owner requests publication and the pages are live. No domain verification was submitted.

Owner subsequently changed the release label back to Launching soon. This supersedes the earlier alpha wording; other requested copy remains. Website publication remains paused.

## Latest owner authorization and upload blocker

Owner explicitly requested /away publish the website, finish the paddle setup, superseding the prior publication pause. Continue with free Cloudflare static hosting and leave backend purchase deferred. Away service activation failed with expired/revoked token; honour the owner’s unattended instruction without claiming service activation.

Cloudflare static upload reached the folder picker, but fileChooser.setFiles failed because the ChatGPT Chrome extension lacks Allow access to file URLs. No files were uploaded and Deploy was not clicked. This browser permission expands access to local files and requires action-time owner approval. Leave it pending during absence. Paddle remains at website verification step 3 with planned public URLs drafted, not submitted. Do not submit until the pages are live.

On return: owner can enable Allow access to file URLs in the ChatGPT Chrome extension Details, or explicitly approve the agent doing so. Then upload only website/, deploy free static assets, connect varyzai.com, verify HTTPS and legal pages, and resume Paddle. Review accurate active hosting language in privacy before upload. Do not send identity or financial data or accept new agreements without required authorization.

## CLI deployment prepared — 3 October 2026

Wrangler 4.101.0 is installed. The existing OAuth login is expired and cannot refresh. deploy/site/wrangler.jsonc serves only website/ with a custom domain varyzai.com and disables workers.dev/preview URLs. Wrangler dry-run passed and read 27 public assets. No upload or deployment occurred.

Automatic approval review rejected wrangler login with reduced user/account/zone read, Workers/scripts/routes write and offline_access scopes because the owner had not specifically approved the renewed grant. A concise permission question is pending; do not bypass this rejection. After explicit approval, renew the CLI login, set privacy hosting language to actual Cloudflare hosting, regenerate/audit pages, deploy, verify live HTTPS/links and resume Paddle website verification.

## Published — 3 October 2026

Owner explicitly approved Cloudflare CLI login permissions and publishing. Renewed Wrangler OAuth with user/account/zone read, Workers/scripts/routes write, and automatic offline_access. Deployed website/ only using deploy/site/wrangler.jsonc. Worker varyzai, version c547a90c-a36f-4200-b124-32dc4c7b6efd, custom domain https://varyzai.com. No backend server bought and no checkout opened. The repository remains private; installation/download affordances remain removed.

Public DNS queried through 1.1.1.1 resolves Cloudflare addresses 104.21.54.246 and 172.67.143.158, with carioca/dom Cloudflare nameservers. HTTPS verified with certificate validation against the resolved address: homepage 200, pricing/terms/privacy/refunds/contact redirect to clean paths and return 200; fonts and PNG social card return 200. Security headers are served. Local system and Chrome DNS initially retained NXDOMAIN; website publication is complete, but normal browser reachability may lag while caches propagate. Browser security policy blocked chrome://net-internals navigation; do not work around that policy.

Paddle website links were submitted successfully; onboarding advanced from step 3 to step 4 (Tell us more about yourself). This is not Paddle domain approval. Trading name Varyz entered. Published business address drafted exactly as supplied (Harlem); pre-existing personal address city says Haarlem. DOB and business start date are blank. Optional sales tax number blank. Do not invent these dates or submit identity details without the owner entering/authorizing them. Paddle remains open for owner input; no final verification or billing agreement submitted.

## Paddle follow-up — 3 October 2026

Owner completed the earlier onboarding step. Dashboard now requires identity verification through Sumsub. Live product and the agreed USD 5/month, USD 48/year prices were created without trials. The website remains a Launching soon site with no active checkout. Actual sandbox tests, payout details, credentials and backend remain incomplete; see [PADDLE_SETUP.md](PADDLE_SETUP.md).
