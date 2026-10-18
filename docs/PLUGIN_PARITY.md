# Plugin capabilities

> Updated 2026-10-05. The hosted packages target the new single-endpoint plugin candidate. Live deployment and native checks remain pending.
> Native account connections, phone behavior and platform approval remain release gates.

## Packages

| Package | Runtime | Capabilities |
| --- | --- | --- |
| `plugins/claude-code` | Current installed local Varyz through its launcher | Full local preference resolution, evidence retrieval, hooks, source setup and consent-based operations |
| `plugins/codex` | Same local Varyz runtime | Same local capabilities through Codex tools/hooks |
| `plugins/chatgpt` | One remote OAuth MCP connection | Confirmed preferences and workflows, questions/replies, rules, away mode, result checks, approved history, context, timelines and retained comparisons |
| `plugins/claude-cloud` | Same connection with Claude HTTP transport | Same hosted tools and shared skills; optional `varyz-cloud` marketplace entry |
| `plugins/chatgpt-archive` | Read-only `/mcp/context` | History/context-only alternative; no preference writes |

The full hosted packages connect `varyz` to `/mcp/plugin` on
`https://cloud.varyzai.com` with fresh `varyz.plugin` consent. The grant permits
confirmed preference tools plus read-only approved history. Old `/mcp` and
`/mcp/context` connections retain their original narrower permissions. Plugin
writes cannot approve or upload history, export, manage billing or delete accounts.
Local installation, project/General sharing and remembering rules are separate
permissions. Installing these packages does not select or upload personal records.

`varyz-cloud` is a Claude plugin package, not evidence of claude.ai/mobile
installation support. Those clients must connect the actual endpoints through
their supported connector interfaces and pass their own device/account tests.
Claude HTTP entries explicitly use `type: http`, as required by the
[official MCP configuration documentation](https://code.claude.com/docs/en/mcp).

## Retrieval

Local packages use current `varyz.retrieval.search`, including the accepted bounded
candidate expansion that protects distinct-source coverage, behavioral query
understanding and privacy checks. They do not bundle a frozen separate ranker.
Updating the generated skills alone does not update a user's installed executable;
the refreshed local bundle includes a newly built wheel/source from this checkout.

Cloud uses its existing encrypted account-scoped lexical index and optional cached
MiniLM vectors, reciprocal-rank fusion and distinct-source results. It scans the
bounded eligible snapshot before source deduplication rather than truncating a
chunk pool to the requested number of sources. The plugin routes semantic questions
to `search`, fetches supporting records, uses `context` for scope, `timeline` for
chronology and `compare` for changes. Thin evidence gets at most two narrower
follow-ups. Source dates, provenance and retained-version limits remain visible.

No new ranking model, title boost or retrieval equivalence claim is introduced.
The general title boost was left disabled after the 2026-10-04 counterexample
audit. See `reports/TITLE_BOOST_FURTHER_2026-10-04.md`.

## Remaining differences

Local/hosted coexistence now has an explicit `route_task` handshake and runtime
workflow guards (2026-10-07). The agent selects one backend on both connections;
local hooks respect that selection. Approved history may supplement the workflow.
This is not automatic host discovery, an atomic cross-server lock or project
identity reconciliation. Older clients remain dependent on agent routing.
See `docs/LOCAL_HOSTED_COORDINATION.md` for handoff boundaries and verification.

- Hosted `ask` uses exact normalized confirmed-answer lookup, not local semantic
  owner resolution, graph reasoning or behavioral evidence reranking.
- Cloud sees eligible approved synced records only. Local sources, new unsynced
  work, excluded messaging sources and sensitive/unclassified data are unavailable.
- Hosted plugins cannot index files, run the local daemon, repair installations,
  mine rules in the background or reproduce local lifecycle hooks.
- The host chooses when to invoke tools. A skill cannot guarantee automatic use in
  every conversation or access another ChatGPT/Claude chat directly.
- Timeline/fetch output is bounded. Compare uses earliest retained and current
  synced versions, not every historical local revision.

## Build and verification

```powershell
.venv/Scripts/python.exe scripts/plugin/build.py --zip-hosted
.venv/Scripts/python.exe scripts/plugin/build_archive.py --zip
.venv/Scripts/python.exe scripts/plugin/build.py --check
.venv/Scripts/python.exe scripts/plugin/build_archive.py --check
uv build --offline --out-dir dist/plugin-refresh
.venv/Scripts/python.exe scripts/plugin/bundle_local.py --package-dir dist/plugin-refresh --output dist/plugin-refresh/varyz-local-0.1.0.zip
.venv/Scripts/python.exe -m pytest tests/test_plugin.py tests/test_local_release_bundle.py -q
```

Hosted ZIPs use only generated files; loose credentials/configs are excluded.
The local bundle includes wheel, corresponding source, licences, generated local
plugins, the optional remote Claude package and checksums. Its separate output
directory preserves existing installer candidates. No personal archive sync or platform publication occurred.
