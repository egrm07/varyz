# Privacy

Last updated: 12 October 2026

This is the privacy notice for the Varyz website (varyzai.com) and for the Varyz software it
distributes. It is the canonical copy: the website's privacy page is generated from this file
by `scripts/site/build_site.py` and is never edited by hand.

## Who is responsible

Egor Menshikov, Anthony Fokkerlaan 3, 2024 JA Harlem, Netherlands, is responsible for the website and the software. For anything about your privacy, write to egrmenshikov@gmail.com.

## The website

- The website has no analytics, no advertising, no tracking pixels and no cookies.
- There are no forms, and it asks you for nothing.
- Fonts, images and scripts are served from the website itself. Nothing is loaded from Google
  Fonts or any other third-party server, so opening the page does not send your IP address to
  anyone else.
- Like every website, it is served by a hosting provider. That provider may keep standard,
  short-lived server logs (your IP address, the time, the page requested and the browser or
  program that asked for it) to deliver the site and protect it from abuse. We do not use
  those logs to profile anyone.
- The public information website uses Cloudflare static hosting. Cloudflare processes requests to deliver and secure the site under its [privacy policy](https://www.cloudflare.com/privacypolicy/). This is separate from the private Cloud account and history deployment described below; public paid launch remains subject to release checks. To show Cloud prices in your currency, the site uses the country Cloudflare detects for your request and sends only that two-letter country code to the Varyz Cloud service, which asks Paddle for that country's price. The site keeps no record of it.
- The software's source repository is [egrm07/varyz](https://github.com/egrm07/varyz). Native installer availability is subject to separate release checks.

## The software

- Varyz runs on your own computer and needs no account. Optional improvement analytics
  are off by default. Setup asks whether to send aggregate usage and reliability counts
  to Egor Menshikov, Varyz's creator. Reports include UTC day, app version and OS family;
  they exclude messages, questions, answers, rules, names, projects, paths and account
  details, and have no permanent user identifier. The receiving server sees the network
  address during delivery and uses temporary hashed rate limits. Disable sharing in setup
  or with `varyz analytics consent off`; unsent data is discarded. Received reports expire
  after 30 days; encrypted backups can retain expired records until backup rotation.
  See `ANALYTICS.md` for exact counters and controls.
- What you teach it (your answers, rules and preferences) and anything it indexes is stored
  in files on your own disk. We do not receive a copy unless you choose to sync or publish
  it, as described below.
- News: while you use Varyz, it asks varyzai.com once an hour for new posts from
  varyzai.com/news, and your agent tells you about each new post once. The request carries
  nothing about you or your archive. Like any page request it reaches the website's server
  logs with your IP address, the time and the Varyz version. News is on by default; run
  `varyz news off` to stop the requests, and `varyz news on` to start them again.
- Apart from the news check, it only connects to outside services you choose to connect it
  to yourself, such as a mail account you ask it to read, or an AI provider you configure.
  Those connections go directly from your computer to that service.
- Sensitive or unclassified personal content is withheld from AI agents and from cloud
  services by default.
- To learn how you like work done, a helper your AI agent starts on its cloud model reads
  your past messages to your AI agents, with secrets removed. That is the only part of your
  archive Varyz gives a cloud model to learn from.
- By default, documents stay on your computer. Varyz does not send them to a cloud
  model to create its search index. A connected AI agent receives the excerpts it
  requests. Separately approved Cloud history can store eligible text on the server
  and return it to your chosen AI provider, as described below.
- The installer downloads the software from its public package source, and the uv package
  manager from its maker, Astral, if you do not have it yet. Those downloads are handled by
  those hosts, not by us.

## Optional cloud accounts

- Varyz Cloud has a deployed account service with live Paddle billing. Local Varyz remains usable without an account. The processing below applies to the deployed Cloud service. Public release checks for installers, directory listings and retention remain separate from payment availability.
- Cloud stores your account email, a salted password hash, subscription status and identifiers, selected confirmed rules, explicit answers and stated workflows, pending questions, authorized connections and synchronization history. Raw passwords and connection credentials are not stored in plaintext.
- Connecting an account does not itself upload preferences. You explicitly select what to sync. Your local archive, documents, transcripts, source snippets and candidate preferences are not part of that synchronization.
- Connected tools receive relevant cloud preferences and task context when you authorize them. Your cloud preferences are private to your account; we do not sell them or use them for advertising. Your chosen AI provider's own terms apply to data its agent receives.
- The cloud sign-in pages use essential session, form-security and authorization cookies. These differ from the public information website, which has no cookies or analytics.
- Hetzner hosts the Cloud service in Nuremberg, Germany, including account data,
  selected preferences and separately approved history. Encrypted application
  backups are kept on that server and on the owner's controlled recovery computer.
  The server keeps at most two daily encrypted snapshots; the recovery computer
  keeps 30 daily encrypted copies. Recovery copies depend on that computer being
  available. See the history section below for deletion and backup limits.
- Hetzner's separate daily whole-server backup service can retain account data,
  approved history and the server-held decryption key. It uses up to seven
  rotating backup slots; the oldest copy is replaced when a new copy is made.
  These disk backups are distinct from the encrypted application backups above.
  See [Hetzner's backup description](https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/).
- Resend delivers verification and recovery email through an Ireland sending
  region, with open and click tracking disabled. It receives the recipient's email
  address and message content, including the relevant verification or recovery
  link. Resend states that stored data is held in the United States; an EU sending
  region does not keep stored data in the EEA. Its email and log retention is 30
  days on the current free plan. Its [security and transfer information](https://resend.com/security)
  describes its transfer safeguards, backup retention and subprocessors.
- Paddle handles purchases as merchant of record under its own [privacy notice](https://www.paddle.com/legal/privacy). We receive subscription and transaction identifiers and status needed to provide access. We do not receive your full payment-card details. When you open the Cloud account page or start checkout, the Cloud service sends your IP address to Paddle so Paddle can show and charge the price for your country and currency. To avoid repeat lookups the service holds the address and Paddle's answer in memory for up to an hour; it does not write them to its database or logs.
- Account and synchronization data are retained while needed to operate the account; revision history is retained for synchronization and corrections. Disconnecting stops local sync but does not delete the cloud copy. Account export and deletion remain available without a paid subscription. Deletion cancels active billing before removing account data; if that cancellation cannot be confirmed, deletion may wait rather than leaving charges running.
- Application backup retention is configured as described above. Server security
  logs use a 28-day journal expiry with daily journal rotation; system text logs
  rotate daily with 28 archives and a 28-day maximum archive age. Cloud container
  output uses the same journal policy. Rotation timing and outages can delay
  expiry; this is not an exact per-entry deletion deadline. The accounting-record
  retention arrangement remains a public-launch check. Immediate erasure from
  every backup or a provider's retained records is not promised.

## Optional approved Cloud history: private deployment 2026-10-05

This separate feature permits explicitly approved projects' eligible history and
future indexed text, or separately approved General records, to be synchronized
for read-only AI retrieval. General contains records with no clear project;
known unapproved projects remain excluded. Sensitive/unclassified records,
detected credentials and unsupported sources remain local. Binary attachments,
model state and the whole local database are not uploaded.

Text, metadata and vectors are encrypted using a server-held key; index terms use
keyed hashes. This protects database copies but permits the service to decrypt
sources for the AI provider the owner connects. Relevant passages and fetched
context leave the service when that provider invokes the connector, under the
provider's own terms. Embeddings use local model weights, without an external
model API. Queries, source passages and credentials are not application log fields.

Project approval continues until revoked. Pause stops retrieval without deleting
stored data. Revocation in the authenticated Cloud account removes the active snapshot and
retained revisions immediately and blocks later uploads until explicitly approved
again. Local revocation takes effect in Cloud when its deletion sync succeeds. Old encrypted backups can retain previously synced text until expiry;
server backups are bounded to two daily encrypted snapshots, with older copies
removed after verified receipt at the existing recovery destination. That receiver
retains 30 daily encrypted copies. Backup checks fail if free disk is insufficient
or verified receipt is overdue. Synthetic encrypted restore verification passed.
Account deletion removes active archive records and indices alongside account
state. No real archive was uploaded as part of building this feature. The current
live preference synchronization remains separate; connecting does not grant
archive upload consent. See `docs/CLOUD_ARCHIVE.md` for limits and controls.

The hosted Varyz plugin requests fresh OAuth consent to read and update confirmed
preferences and retrieve already approved synced history through one connection.
Older preference-only and read-only history connections keep their original
permissions. The plugin cannot approve projects, upload or change history, export
the account, change billing or delete the account. Its skills instruct the connected
AI to save only explicitly authorized rules, workflows and actual replies. The
service cannot guarantee that every AI host follows those instructions.

## Purpose and control

Cloud account, synchronization and billing processing are intended to provide the service you request. Essential security processing protects accounts and limits abuse; required transaction records support legal obligations. Cloud synchronization is optional. Keep sensitive personal information out of preferences unless you intend to share it with the tools you connect.

## Your rights

The public information website keeps the short-lived logs described above. Private
Cloud accounts keep account information, selected preferences and separately approved
history. You can export account data or request deletion through the account page.
You can also ask us to access, correct, export or delete the data kept with your account. Write to
the address above and we will answer. You can also complain to your data protection
authority.

## Changes

If this notice changes, the date at the top changes with it.
