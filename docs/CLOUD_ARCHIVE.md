# Approved Cloud history

Compact agent work reports have a separate **Share session reports** project
approval, default off, in Cloud setup. Approving raw project history does not
approve reports, and report approval does not approve raw transcripts. Hosted
plugins retrieve approved synced reports with read-only `session_history`.
See [session progress](SESSION_PROGRESS.md) for source dates, privacy, pagination
and revocation. Mining remains local until this report-specific approval exists.

> Built 2026-10-05; local verification passed. Server deployment, archive backup
> capacity and a real ChatGPT phone connection remain release gates.

Cloud history is a searchable, encrypted snapshot for a read-only ChatGPT
connection. Local Varyz remains the source of truth. After a successful sync the
computer can be off; later work becomes available after the next local sync.
There is no phone capture UI, hosted answer model or external embedding API.

## Approval and operation

**Allow a project once to include eligible existing and future work.** Records
with no clear project belong to **General**, approved separately. General never
includes a clearly identified project merely because that project is unapproved.
Project categorization and sensitivity classification are different: General
does not override a sensitive or unclassified label.

The following is an operator quickstart after the server release gates are met.
Run the same local config throughout. Approval changes local consent; the existing
autosync cycle can subsequently send eligible records. A dry run is offline.

```powershell
varyz cloud archive projects
varyz cloud archive sync --dry-run
# From the project folder, after the owner approves Cloud and connected AI access:
varyz cloud archive allow --approve-sharing
# Independently approve uncategorized records if requested:
varyz cloud archive allow --project General --approve-sharing
varyz cloud archive sync --dry-run
varyz cloud archive sync --semantic
```

| Command | Behavior |
| --- | --- |
| `projects` | Categorized record counts and approved projects; no upload |
| `allow` | Approve eligible project history and future records; current folder by default |
| `allow --project General` | Approve records with no clear project |
| `allow --project NAME --root PATH --repository OWNER/REPO` | Exact folder and optional GitHub repository association |
| `sync --dry-run` / `doctor` | Offline counts, deletions and blocked IDs; no private passages printed |
| `sync --semantic` | Build vectors locally and upload approved text; retain semantic mode for later syncs |
| `control pause` / `resume` | Control local uploads and authenticated Cloud retrieval |
| `control revoke --project NAME` | Remove approval and sync deletions when local sync is enabled |
| `status` | Local consent and remote count/quota state |

`select --entry ID --project NAME --approve-sharing` remains an advanced exact
record selection. Its approval expires after content or metadata changes. Project
approval permits future eligible updates automatically. Individual revocation
excludes that record even inside an approved project. Revocation needs a successful
sync to remove the active remote copy; a network failure is not erasure confirmation.
Pausing retrieval retains storage. Old encrypted backups expire separately.

## Scope and evidence

Eligible sources are indexed AI conversations, documents, Git and GitHub records
marked public or personal. Sensitive/unclassified entries, detected credentials,
unsupported sources and oversized records are blocked. Original binary files,
attachments, model state, mail and messenger archives are not uploaded. Secret
detection is a defense, not proof that arbitrary source code contains no secrets.

Categorization uses connector provenance: conversation project headers, exact
repository/folder boundaries and exact GitHub owner/repository associations.
Nested repositories do not inherit parent approval. Unknown project associations
go to General; known unapproved projects remain unavailable. Path-based associations
use the current local filesystem; ambiguous associations become General and need
its separate approval. Explicit ID selection can resolve an association.

`search` returns source-addressed passages with project and occurrence dates;
`fetch` returns original context, source identity, provenance and synchronization
date. Sources remain untrusted data. Mixed conversations are not wholly the
owner's writing. The connector tells ChatGPT to cite sources and distinguish
missing evidence from proof that work was never completed. ChatGPT generates the
answer; Varyz does not claim generated-answer accuracy or complete project status.

## Security and storage

HTTPS uploads require a configured Fernet key. Text/metadata and float32 vectors
use authenticated encryption; searchable terms and project index keys use
account-specific HMAC values. A database copy does not expose passage text or
vectors. Keep the key outside source control, restrict operator access and back it
up separately. **This is service-readable encryption, not end-to-end encryption.**
The service decrypts evidence to return it to the AI provider the owner connects.
Counts, identifiers, sizes, sync timestamps and term frequencies remain visible
as operational metadata. There is no general external-model fallback.

The combined read-only OAuth scope `varyz.context.read` exposes `search`, `fetch`,
`context`, `timeline` and `compare` at `/mcp/context`. Context includes confirmed
General/project preferences and the latest dated source evidence. Timeline gives
chronology; comparison returns the first retained synced version and the current
source, with authenticated version citations. These are evidence tools: the AI
writes the answer and must not treat absent completion as unfinished work.

Existing `varyz.archive.read` connections keep their original search/fetch-only
access at `/mcp/archive`; consent is never silently widened. Legacy preference
tokens cannot retrieve archives. Read-only tokens cannot upload, change
preferences, export accounts or manage consent. Local credentials manage uploads;
the authenticated account page can pause or revoke sharing while the computer is
off. Server revocation blocks stale uploaders until a new explicit approval.
OAuth codes and refreshes retain their scopes. Citation URLs require account
access and are never public shares.

Each account is bounded to a conservative 100 MiB storage budget, 10,000 record
identities including tombstones, 20,000 chunks and 100,000 count-only audit events.
Updates replace active content and postings, with version checks and idempotent
acknowledgements. The first synced version and four most recent earlier versions
are retained, encrypted and charged to the account budget. Account export includes
current sources and retained revisions, even when paused or unsubscribed. The budget estimates index/encryption overhead rather than JSON
vector text. It is not an exact physical disk allocation. Deletion removes active
content, retained revisions and search indices; tombstones keep version synchronization reliable.

The keyword baseline uses a tenant-scoped BM25 postings index. Optional hybrid
search combines it with local cached multilingual MiniLM cosine ranking and RRF.
Document vectors are generated locally; query inference uses pre-provisioned
server weights. Missing weights preserve keyword search. Two concurrent archive
MCP reads are admitted per service process. A bounded pure-Python vector scan is
bounded to 20,000 chunks per account. On the deployed server, a sequential
19,880-load-chunk probe took 6.3–7.2 seconds per query and peaked at 784 MiB RSS.
Concurrent customer traffic remains unbenchmarked.

## Deployment and capacity gates

`deploy/cloud/compose.yaml` has the encryption and quota settings. Keyword search
uses the base image. Add `compose.archive.yaml` for the optional hash-pinned ONNX
runtime and weights provisioned during image build (1.5 GiB memory / 1.5 CPU cap).
Requests do not download weights. `scripts/export_cloud_requirements.py` regenerates
both cloud dependency closures from `uv.lock`; local document readers and provider
SDKs are excluded. The image's allowlist excludes personal archives and configs.

The combined plugin draft is `plugins/chatgpt-archive`; build/check with
`python scripts/plugin/build_archive.py --zip` / `--check`. Its endpoint is
`/mcp/context`. Discovery and explicit consent include preferences and approved
history. The ZIP is a draft, not platform approval. Live HTTPS and the official
MCP SDK passed search/fetch/context tests. An actual ChatGPT phone session and
phone account availability remain unverified.

The existing setup flow offers preferences and history separately. History
checkboxes are by project, with General separately unchecked by default. Approval
binds the preview to the account, source content and local journal; stale previews
are rejected. The installed local client was updated without selecting or uploading
personal history. No capture interface or standalone phone app was added.

Read-only inventory on 2026-10-05 measured 6,003 local indexed records, 2.92 MB of
entry text, 6,935 chunks and 55.34 MB of existing search tables. The 2.70 GB local
database includes caches and free pages and must not be uploaded wholesale.
The local vault also holds older/unindexed files; these are outside this snapshot.
50–100 MB remains a planning range, not a measured production encrypted-archive size.

The deployed server has 2 CPU / 4 GB RAM / 40 GB disk and 30.70 GB free after
deployment. The account admission cap stays 100, but a separate conservative
4 GiB archive budget permits approximately 40 accounts at 100 MiB each, or 81 at
50 MiB. These are storage bounds, not concurrent-user capacity promises.

Recovery now stages on disk, hashes/transfers as streams, encrypts snapshots and
backs up the archive key separately to the existing recovery destination. The
server keeps at most two new snapshots and prunes only after an exact checksum
has been acknowledged by an off-host restore. If the receiver is unavailable,
backup creation stops at its bound and reports failure rather than filling disk.
The existing receiver retains 30 daily encrypted copies; same-day backups replace
that day's snapshot. Backup checks reserve two database sizes plus 2 GiB free.
A synthetic encrypted archive was restored and decrypted using its recovered key,
then removed; the final backup was restored again after cleanup.

The Cloud deployment is live for private validation. Billing mode, signup gates
and sharing choices were preserved. No personal archive was uploaded. Live
PostgreSQL, real-source answer quality, concurrent load and an actual phone session
remain unverified. See `reports/CLOUD_CONTEXT_DEPLOY_2026-10-05.md` for the shipped
verification results and `reports/CLOUD_ARCHIVE_2026-10-05.md` for the earlier
retrieval experiment, including failed candidates.
