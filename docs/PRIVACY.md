# Privacy

Last updated: 18 October 2026

This is the privacy notice for the Varyz website (varyzai.com) and for the Varyz software it
distributes. It is the canonical copy: the website's privacy page is generated from this file
by `scripts/site/build_site.py` and is never edited by hand.

## Who is responsible

Egor Menshikov, Anthony Fokkerlaan 3, 2024 JA Harlem, Netherlands, is responsible for the website and the software. For anything about your privacy, write to support@varyzai.com.

## The website

- The public website counts public HTML requests by default to measure page popularity,
  broad traffic sources, HTTP status categories and server response-time ranges. These
  are daily aggregate counters, not individual visit records. No visitor identifier,
  IP address, user agent, full referrer, query string, precise request timestamp or
  sequence of pages is stored in this layer. No analytics code runs in your browser
  for these counters. Requests from known bots and browser privacy signals are excluded.
  Counters are retained for 90 days and are used only to operate and improve the site.
  Network delivery still involves our hosting providers, as described below.
- The public website also offers optional detailed usage and performance analytics
  using self-hosted Umami on our Hetzner server in Germany. This browser tracker starts
  only when you choose **Allow cookies** in the Cookies & privacy popup. The name is a
  familiar privacy-control label; our analytics do not set tracking cookies. Declining
  or dismissing the popup does not affect access. Cookie settings in the footer lets
  you withdraw consent at any time. We respect Global
  Privacy Control and Do Not Track signals. We store only your consent choice, version
  and date in browser local storage for up to 180 days; the tracker sets no cookies.
- With consent we measure public pages visited, referring website, selected campaign
  labels (UTM), browser, OS, device, screen size, language, approximate location,
  navigation and download clicks, section views, scrolling, visible-page time, FAQ use,
  repeated clicks, media controls, copy actions (without clipboard content), performance
  and error categories (without error messages or stacks). Query strings and URL
  fragments are excluded except selected UTM labels. No advertising identifiers,
  account identities, form values, messages or private Varyz content are sent.
- Choosing **Allow cookies** includes masked session recordings and click/scroll
  heatmaps for website improvement. Recordings mask text and inputs, block forms and private-marked elements,
  and stop after five minutes per page. Pages opened with query strings or URL fragments
  are excluded from recordings and heatmaps. Private Cloud account and payment pages
  are outside this tracker. A download click does not tell us whether installation
  succeeded. Copy events do not include the copied text.
- Our server processes the network address to derive rotating pseudonymous session
  identifiers and approximate location. Umami does not store the raw IP address in
  its analytics tables. This is pseudonymous measurement, not a promise of anonymity.
  Analytics events expire after 90 days; recordings and heatmaps after 30 days, with
  daily cleanup. Encrypted analytics backups rotate after seven days; Hetzner's
  whole-server snapshots may retain copies within their separate rotation below.
  Withdrawal stops future collection and cannot recall already delivered records.
  Optional detailed website analytics rely on your consent (GDPR Article 6(1)(a)).
  Default counters contain no visitor-level records; transient processing of ordinary
  request metadata to form these counters supports our legitimate interest in improving
  and operating the website (Article 6(1)(f)). Browser GPC and DNT signals stop counting.
- Fonts, images and scripts are served from the website itself. Nothing is loaded from Google
  Fonts. Consented analytics scripts are served through analytics.varyzai.com on our
  infrastructure; no analytics data is sent to Umami's hosted service.
- Like every website, it is served by a hosting provider. That provider may keep standard,
  short-lived server logs (your IP address, the time, the page requested and the browser or
  program that asked for it) to deliver the site and protect it from abuse. We do not use
  those logs to profile anyone.
- The public information website uses Cloudflare static hosting. Cloudflare processes requests to deliver and secure the site under its [privacy policy](https://www.cloudflare.com/privacypolicy/). This is separate from the private Cloud account and history deployment described below; public paid launch remains subject to release checks. To show Cloud prices in your currency, the site uses the country Cloudflare detects for your request and sends only that two-letter country code to the Varyz Cloud service, which asks Paddle for that country's price. The site keeps no record of it.
- Email you send to an @varyzai.com address is delivered to the operator's Google Workspace mailbox for varyzai.com, where Google stores it under the [Google Workspace privacy notice](https://workspace.google.com/terms/privacy/). It is not forwarded anywhere else. Replies are sent from that mailbox; verification and recovery email is sent through Resend (described below). We use your address and message only to answer you.
- The software's source repository is [egrm07/varyz](https://github.com/egrm07/varyz). Native installer availability is subject to separate release checks.

## The software

- Varyz runs on your own computer and needs no account. Sharing with the Varyz network is
  off by default and governed by one choice: a single popup, `varyz data on` or
  `varyz data off`. Setup starts with sharing off and does not show the popup. It appears
  when you first save a rules review after your first 3 conversations and 24 hours with
  Varyz. Closing the popup means no; after a no it is offered again at most once, no
  sooner than 30 days later. If you opt in, Varyz sends two things to
  Egor Menshikov, Varyz's creator, at cloud.varyzai.com. The first is daily usage and
  reliability counts (UTC day, app version and OS family, no content); received count
  reports expire after 30 days.
- The second is anonymized examples created after you opt in: questions your agents asked
  Varyz and the answers they got, whether you adopted, overrode or corrected them and the
  correction, how long an answer took, and the rules you confirmed, revised or rejected.
  Vision notes, chats, files, connected sources and the archive itself are never sent.
- Before an example leaves your computer, Varyz replaces names of people it knows,
  project names, emails, links, file paths, phone and other long numbers, network
  addresses and handles with placeholders. It drops any example that holds a password,
  key or token, or that Varyz withheld from an agent. Cleaning is automatic and can miss
  something, so treat shared examples as pseudonymous, not guaranteed anonymous.
  `varyz data preview` shows the exact records the next upload would hold.
- Examples are linked only to a random contributor key that your installation creates;
  the server stores a hash of that key, not an account, name or device. The server sees
  your network address during delivery and uses it for temporary hashed rate limits.
  Examples are kept until you delete them. Anonymized examples may be licensed to AI
  companies to improve their models; that is how the network is funded. We do not sell
  your account details, and we do not use shared examples for advertising.
- Turning sharing off (`varyz data off`) stops all
  sending, discards anything unsent and asks the server to delete every example your
  installation shared, retrying until it succeeds. Copies already included in a dataset
  delivered to a licensee before your deletion cannot be recalled by us. Encrypted backups
  can retain deleted records until backup rotation. See `ANALYTICS.md` for exact fields
  and controls.
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
- Connected tools receive relevant cloud preferences and task context when you authorize them. Your cloud preferences are private to your account; we do not sell them or use them for advertising. Only examples you choose to share through the Varyz network, described under The software, may be licensed. Your chosen AI provider's own terms apply to data its agent receives.
- The cloud sign-in pages use essential session, form-security and authorization cookies.
  The optional public website tracker does not run on these account pages.
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

We use account, authentication and subscription information to provide the service you request and administer your contract (GDPR Article 6(1)(b)). Necessary security logging, rate limits and abuse prevention support our legitimate interests in keeping accounts and the service safe (Article 6(1)(f)); we limit their scope and retention as described above. Records that we must retain for tax, accounting or other legal obligations are processed on that legal basis (Article 6(1)(c)), rather than optional synchronization consent.

Optional Varyz network sharing (usage counts and anonymized examples) and the optional synchronization of selected preferences or separately approved history rely on your informed choice and consent (Article 6(1)(a)). You can withdraw that permission through the relevant consent, project revocation or connection controls. Withdrawal does not invalidate earlier lawful processing. Disconnecting a tool stops its future access but does not itself delete previously synchronized records or copies already delivered to another provider. Use the account or project deletion controls to request removal of active Cloud copies.

Your connected AI provider processes the information it receives under its own terms and privacy notice. It may be outside the EEA. Read that provider's notice before connecting it; a German Cloud server does not mean that every recipient or backup stays in Germany. The suppliers and transfer information described above apply to their respective functions. Contact us for information about the safeguards applicable to a transfer and how to obtain a copy where available.

Do not synchronize or send through the hosted plugin payment-card details, government identifiers, passwords, API keys, protected health information, or personal information about children below the applicable age of digital consent (16 in the Netherlands). Keep special-category information, such as identifiable health, sexuality or religious information, out of hosted preferences and history. Varyz Cloud currently provides no supported process for accepting special-category personal information. A project approval or a connected tool's request is not blanket permission for sensitive data. Automated checks cannot reliably recognize every sensitive record; you must also choose carefully what to share.

Only share content you are entitled to process and disclose. An imported conversation can contain someone else's personal information or copyrighted words. Your approval does not provide that person's consent or transfer their rights. This notice does not grant permission to upload unlawful content. If you believe information about you was uploaded by another person, contact us without sending an entire private conversation; we will assess the request and the applicable rights.

## Your rights

The public information website keeps the short-lived logs described above. Private
Cloud accounts keep account information, selected preferences and separately approved
history. You can export account data or request deletion through the account page.
You can also ask us for access, correction, erasure, restriction of processing and, where applicable, a portable copy of your data. You may object to processing based on legitimate interests and withdraw consent for optional processing. These rights have legal conditions and exceptions; for example, necessary accounting records may need to be retained after account deletion. We may ask for proportionate information to verify your identity, without routinely requiring an identity-document copy.

Write to the contact address above. We normally respond within one month; where the law allows an extension for complexity or volume, we explain it within that first month. You can complain to the [Autoriteit Persoonsgegevens](https://autoriteitpersoonsgegevens.nl/) in the Netherlands or your relevant data protection authority. You do not have to contact us first.

Account email and authentication information are needed for a Cloud account; without them we cannot provide that account. Local Varyz remains available without one. Optional analytics and optional history synchronization can be declined. Varyz's preference suggestions are assistance for your chosen AI tools; we do not use them to make solely automated decisions with legal or similarly significant effects about you.

## Changes

If this notice changes, the date at the top changes with it.
