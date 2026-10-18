# Varyz imports

Varyz keeps source ingestion local, explicit, resumable, and idempotent. It does not read
WhatsApp, Telegram, Instagram, Google Contacts or browser history: those connectors were
removed from this tree (2026-09-27), and there is no way to connect them.

## Configure sources

`varyz setup` offers the local sources it finds (AI conversations, Git repositories,
document folders), asks before each, indexes what was approved and prints which sources are
connected. `varyz sources` shows the same list at any time.

To configure them by hand, create the archive, then add only the connector sections whose
paths or accounts the owner has approved:

```powershell
uv run varyz init --config config.toml
uv run varyz connect ai-conversations --config config.toml
uv run varyz connect github --config config.toml
uv run varyz sync --config config.toml
```

Documents and Git repositories use explicit approved
paths in `config.toml`. Run `varyz sync <connector>` to limit a cycle to one configured
source, or omit the connector to resume all configured sources.

## Shipped source matrix

| Source | Configuration | Incremental identity |
|---|---|---|
| AI conversations | `varyz connect ai-conversations` | Adapter/file position and content identity |
| Documents | Approved paths | Absolute path and content hash |
| Git repositories | Approved roots | Repository plus commit SHA |
| GitHub | `varyz connect github` | Repository/stream update watermark |
| Manual | `varyz add "..."` | Locally generated immutable id |

Gmail, Google Calendar and Drive, calendar files and personal archives are not in the
package. Their connectors stay in the source tree and
run from a checkout.

Connector secrets stay in the operating-system credential store. Configuration contains
only source labels, options, approved paths, and account identifiers. A cursor advances
only with the item it covers; an empty incremental response is not authoritative deletion
unless the connector explicitly declares full enumeration.

## Snapshot used by the pivot

The development tree contains a compact snapshot under `data/`. It retains archive ground
truth, the content-addressed vault, and the paid-for graph/annotation caches. It excludes
old Mirror and personal-model generations. `varyz sync --no-embed --config config.toml` is
a clean no-op when no connector is configured.

Model weights and llama.cpp runtime files contain no personal source data and may be shared
through read-only directory junctions. If a shared location is unavailable, use the
verified installer, `varyz local-model install` (source tree only; the alpha leaves it out),
to place fresh copies below this tree.

## Safety rules

- never scan a broad home directory without explicit roots;
- never write to the sibling source archive from the pivot tree;
- keep connector secrets and bearer URLs out of TOML, logs, ids, and exceptions;
- classify unknown content fail-closed;
- preserve upstream files when deleting local indexed data;
- keep default tests synthetic, deterministic, and offline.
