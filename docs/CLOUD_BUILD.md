# Plugin and cloud build

> Design 2026-10-02. Owner mandate: build plugin adaptation, easier installation and cloud; **no free trial**. Propose hosting options before provisioning. This is a later release, not a silent expansion of the 0.1.0 alpha.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Local product | Free, account optional | Existing local behavior stays usable offline |
| Cloud access | Paid entitlement; no trial | Owner instruction |
| Price | Configured externally; proposed USD 5 monthly / 48 annually | Proposal remains unconfirmed |
| Uploads | Explicit opt-in, selected confirmed records only | Never upload an archive or unreviewed suggestions |
| Conflict handling | Server versions with compare-and-swap | No device clock can overwrite another device silently |
| Infrastructure | Portable Python ASGI service; SQLite development backend | Provisioning/provider selection remains open; production scaling needs shared storage |

## Ownership

- `plugins/`, `scripts/plugin/`, `src/varyz/plugin.py`, `tests/test_plugin.py`: plugin worker.
- `src/varyz/cloud/`: cloud worker, except `client.py` and `local_sync.py`.
- `src/varyz/cloud/client.py`, `local_sync.py`, CLI wiring, dependency metadata, integration and release docs: root.
- Shared existing installation changes belong to the owner; preserve them.

## Sync contract [CONTRACT — do not edit]

HTTPS APIs authenticate the account from a bearer credential, never from a model-supplied account ID.

`GET /api/sync?cursor=<integer>&limit=<1..500>` returns `{records: [...], cursor: integer, has_more: boolean}`.

`POST /api/sync` receives `{records: [{kind, id, base_version, payload}]}`. Allowed kinds: `rule`, `answer`, `workflow`. `payload` is a bounded JSON object, or null for a deletion. New records use `base_version: 0`; edits/deletes use the last downloaded server version. Each record response contains `kind`, `id`, `version`, `payload`, `cursor`. Successful response `{records: [...]}`. A conflict is HTTP 409 with the current record; a batch is atomic.

Cloud adapter payloads hold only selected safe columns from existing local tables. Do not accept arbitrary SQL table names or column names. Server validates kinds, sizes and payload schemas. Account isolation applies to every query, OAuth token, sync record and tool call.

`GET /api/account` returns identity and entitlement. `GET /api/export` and `DELETE /api/account` remain accessible without an active subscription. `POST /api/billing/checkout` creates paid checkout without trial options. Entitlements come only from verified billing-provider events, never success redirects or client fields.

Remote MCP at `/mcp` offers compiled confirmed knowledge. Unsupported archive tools return explicit unavailable. OAuth authorization-code flow uses PKCE S256, exact registered redirect URIs, expiring single-use codes and credential hashing. HTTP body limits and rate limits are required. No private text in access logs.

## Local sync contract

`varyz cloud connect --url URL`: device browser authorization; secret stored in OS keyring, opt-in confirmed export selection. `cloud status`, `cloud sync --dry-run`, `cloud sync`, `cloud disconnect`, `cloud serve` (optional dependencies).

Sync state lives beside the local config, not in project files. Dry-run reads selected records and reports counts only. It never contacts cloud or uploads text. Conflicts remain visible; never mark failed batches synced. Remote tombstones are pulled and applied; local retractions produce tombstones.

## Release gate

Offline tests cover tenant isolation, unpaid denial, billing signature and replay checks, OAuth state/PKCE/reuse, sync conflict/deletion/retry and archive exclusion. Provider provisioning, public package availability, store approvals, paid checkout and platform interoperability require separate live verification before public launch.
