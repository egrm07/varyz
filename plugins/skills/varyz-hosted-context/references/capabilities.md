# Capabilities and routing

Updated 2026-10-05. Use actual connected tool schemas. A package is not proof of
platform approval or an authenticated device connection.

| Need | Full local Varyz | Hosted package |
| --- | --- | --- |
| Rules and task constraints | `start_task` with local rules and definition of done | `varyz.start_task` with synced confirmed rules/workflows |
| Owner-dependent choice | `ask` with local resolution and evidence controls | `varyz.ask`, exact confirmed lookup and pending questions |
| Save an explicit rule/workflow | Local remember tools | `varyz.remember_owner_rule` / `remember_owner_workflow` |
| Record an actual reply | `answer_owner_question` | Same hosted tool; use returned `question_id`, not a local-only parameter |
| Task-scoped away mode | Local `away` plus local hooks | Hosted `away`; use `owner_words` and stable `conversation_id` |
| Review the result | `finish_check` | `varyz.finish_check` for confirmed instructions |
| Personal/project evidence | Local `context`, `brief`, `recall` | `varyz.context`, then `search` and `fetch` |
| Chronology | Local evidence retrieval | `varyz.timeline` with project/date filters |
| What an AI session did | Local `session_history`, optional approved local mining | `varyz.session_history` over separately approved synced reports |
| Earlier vs current source | Local source evidence | `varyz.compare` for earliest retained vs current synced version |
| Index, sync, repair and rule reading | Local runtime/CLI with consent and hooks | Unavailable through hosted tools |

The dot notation above identifies the server, not literal MCP tool names. Some
hosts prefix tool names differently. Read each tool schema before calling it.
The package uses one explicitly consented plugin grant. History stays read-only;
preference writes cannot upload history or change its approval. Hosted tools cannot
select projects, approve General or synchronize records on the owner's behalf.
Use the account's existing controls for pause/revocation; do not imply a plugin
can operate a local CLI or change sharing through a read-only history tool.

Local retrieval uses current local Varyz, including bounded candidate expansion
for distinct-source coverage and behavioral evidence checks. Cloud search uses
account-scoped lexical retrieval and, when provisioned, cached local MiniLM vectors,
reciprocal-rank fusion and distinct-source results. These are different backends;
do not claim equal ranking, graph reasoning, behavioral reranking or answer quality.
Do not enable experimental title boosts that local evaluation left disabled.

Missing local lifecycle features cannot be reproduced with prose or simulated
success. Hosts decide when to invoke skills/tools. New work is available remotely
only after an approved local synchronization; the computer may then be off.
Only approved eligible documents, AI conversations, Git and GitHub records are
supported. WhatsApp/Telegram remain excluded from agent-facing retrieval.

The optional `varyz-cloud` Claude plugin is a remote MCP package for plugin-capable
Claude hosts. It does not establish availability in claude.ai or phone clients.
On clients using connector settings rather than plugins, use
`https://cloud.varyzai.com/mcp/plugin`. Legacy `/mcp` preference and `/mcp/context`
read-only connections retain narrower grants and need fresh consent for the plugin;
connection/authentication and device support must be verified in that client.
