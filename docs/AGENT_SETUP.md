# Connect an agent to Varyz

Varyz exposes a local stdio MCP server. Agents call `start_task` first, `ask` at every
owner-dependent choice, and `finish_check` before they report done; in Claude Code and
Codex the first and last happen through hooks.

## Installing Varyz for the owner

This section is for an agent (Claude Code, Codex) asked to install Varyz. The owner
should not need a terminal: run every step yourself and tell them in plain words what is
happening.

1. **Check first.** If `varyz --version` works (or `~/.local/bin/varyz`, on Windows
   `%USERPROFILE%\.local\bin\varyz.exe`), Varyz is installed. Run `varyz update apply`
   instead of installing, then continue at step 4 if they wanted setup again.
2. **Find the source.** When you are in a copy of this repository (its `pyproject.toml`
   says `name = "varyzai"`), install from it. Otherwise install from GitHub,
   `https://github.com/egrm07/varyz`. The repository is private, so git must be signed
   in to GitHub. `git ls-remote https://github.com/egrm07/varyz HEAD` tells you. If it
   fails, ask the owner to sign in (`gh auth login` opens their browser); do not ask for
   a token in the chat.
3. **Install.** Run the installer in the background with the page: from a copy,
   `sh scripts/install.sh --web` (Windows: `powershell -ExecutionPolicy Bypass -File
   scripts\install.ps1 -Web`). With no copy, `uv tool install
   git+https://github.com/egrm07/varyz`, and then step 4. The installer installs uv if it
   is missing, installs Varyz and starts setup.
4. **Setup.** The installer starts it; otherwise run `varyz setup` in the background. It
   opens a page in the owner's browser, where they tick the agents and data sources and
   can connect GitHub. Tell them the page is open. It runs until they finish there, which
   can take a while on a first run because the local models download. Do not use
   `--yes` unless they asked you to connect everything without asking.
5. **Restart.** When the page says setup is done, ask the owner to restart you (and any
   other agent that was connected) so the new Varyz server and hooks load.

Only the owner can do some things: allow folder access when macOS asks, sign in to
GitHub, trust new hooks in Codex, and click through an unsigned-launcher warning. Say
what they will see and wait; do not work around them.

## Running Varyz for the owner

Once connected, the routing skill and the MCP server's instructions tell an agent which
command fits each request ("update Varyz", "review my rules", "is Varyz working?"). The
agent runs it and reports what happened. `varyz setup` and `varyz rules review` open
pages and run until the owner finishes there, so they run in the background.
`varyz update apply` reinstalls Varyz from where it came from (GitHub, or the folder it
was installed from, pulled first when that is a git clone). It then refreshes every
connected agent's Varyz files: the skill, the hooks, and in Claude Code the reader agent.
Nobody runs `varyz connect` after an update. The first agent session on a new version
does the same refresh for an update made any other way.

## One command

`varyz setup` connects every supported agent it finds, together with the archive, the
daemon and the data sources (README, "Quickstart"):

```powershell
varyz setup
```

It asks before each agent; `--yes` connects them all. From a checkout, use
`uv run varyz setup --config config.toml`.

To connect one client by hand, start the daemon first:

```powershell
uv run varyz daemon start --config config.toml
uv run varyz connect codex --config config.toml
uv run varyz connect cursor --config config.toml
uv run varyz connect claude-code --config config.toml
```

Each connect command is idempotent, and so is `setup`. It registers a named principal, mints a token, grants a
bounded owner-context capability, writes the client's MCP configuration, installs the
Varyz routing skill and, for Claude Code and Codex, the briefing hooks. The skill's
central rule is: when you are about to assume something about the owner, call `ask`.

Restart the client after its configuration changes. An update refreshes what connect
installed for every client already connected, so connect is run once per client, not
once per version.

## Hooks

| Event | What varyz does |
|---|---|
| `SessionStart` | starts the loopback daemon if it is down; reminds the agent of the workflow |
| `UserPromptSubmit` | first task of a session: injects the full `start_task` briefing; later prompts: only the definition of done for kinds of work not yet briefed |
| `Stop` | silent unless `[agent] done_gate = true`: then once per task, only after files were edited and the agent did not call `finish_check` after its last edit, it sends the agent back with a short note to call `finish_check` |

Hooks are appended to `~/.claude/settings.json` or `~/.codex/hooks.json`; other groups
(for example Orca's) are kept in place, and the previous file is saved next to it as
`.varyz-backup`. The commands run launchers in `data/hooks/`. Codex asks the owner to
trust new hooks the first time. `[agent] briefing_hooks = false` skips installing and
running them. The stop check (`done_gate`) and the writing check (`writing_check`) are
off by default, because the client shows their note under the finished reply. Every hook
fails open. Preview the briefing without an agent: `varyz task "..." [--finish]`.

```powershell
uv run varyz hooks doctor --config config.toml   # go/no-go and data sources; exit 1 on no-go
uv run varyz hooks pause --config config.toml    # kill switch: hooks do nothing
uv run varyz hooks resume --config config.toml
```

## MCP surface

| Tool | Behavior |
|---|---|
| `start_task` | First, on every task: the owner briefing and definition of done |
| `ask` | Any owner-dependent choice; relays to the owner (`ask_owner`) when unknown |
| `answer_owner_question` | Record the owner's relayed answer, with `scope` general/project |
| `owner_questions` | What varyz is still waiting on |
| `finish_check` | Last, before reporting done: every item to verify |
| `remember_owner_rule` | Record a standing rule the owner just confirmed (optional `domain`) |
| `resolve`, `advise` | Older names of `ask` and `start_task` |
| `context` | Task preflight joining archive evidence, owner preferences, and explicit unknown dimensions |
| `recall` | Bounded, source-addressed archive evidence |
| `brief` | Bounded evidence fan-out for a task |
| `lookup` | Read a stored compiled life-model record |
| `interpret` | Read one interpretation Varyz already offered |

`resolve` is not read-only because an unanswered question can enter the owner queue. Its
description tells agents that gaps return `NOT_KNOWN` or `ESCALATED`, never a plausible
answer. Mirror and `understand` are retired surfaces.

Use `context` once before substantial personalised work such as design, planning, review,
or product writing. Pass only the dimensions that can change the output. The response
separates typed preferences, dimension-specific evidence, and `missing_dimensions`; an
unrelated known preference never makes another dimension known. Use `resolve` for one
consequential missing choice, and use `recall` only to expand evidence rather than to
select the first plausible excerpt as the owner's answer.

## Identity and capability

A token resolves to a `Caller` containing principal, display name, purpose, capability,
device, and transport. Tokens are stored as hashes and can be revoked. A capability bounds
purpose, tools, items, characters, background use, and expiry. Local stdio does not confer
trust by itself; a missing token receives an explicit low-trust identity.

Use `varyz agent --help` to inspect the registration, grant, list, and revocation verbs.
Use `varyz daemon status` to confirm the local service and pending question count.

## Owner interaction

When `resolve` escalates, the owner answers from the CLI, or the agent relays the question
and records the reply with `answer_owner_question`:

```powershell
uv run varyz questions --config config.toml
uv run varyz answer <escalation-id> "The answer to remember" --scope general --config config.toml
uv run varyz dismiss <escalation-id> --config config.toml
```

The answer becomes append-only `USER_CONFIRMED` knowledge. The next equivalent request is
served from tier 1 without asking again. Varyz shows no notifications: a waiting question
reaches you through the agent that asked it or through `varyz questions`.

Review and withdraw what agents can be told. A retraction is append-only: the answer stops
resolving but stays in the audit history.

```powershell
uv run varyz knowledge list --config config.toml
uv run varyz knowledge retract <knowledge-id> --reason "No longer true" --config config.toml
```

## Direct registration

For clients managed by the older installer path, preview and apply the MCP configuration:

```powershell
uv run varyz install --client codex --dry-run --config config.toml
uv run varyz install --client codex --config config.toml
uv run varyz install --client cursor --config config.toml
```

`varyz install` pins an absolute executable and config path. It does not replace the named
principal/capability work performed by `varyz connect`.

## Remove the integration

```powershell
uv run varyz uninstall --dry-run --config config.toml
uv run varyz uninstall --config config.toml
uv tool uninstall varyzai
```

Uninstalling integration does not delete the owner's archive, config, model assets, or
backups. Review the paths printed by the command before removing retained state manually.

## Troubleshooting

- `resolution daemon unavailable`: MCP `resolve` starts a loopback daemon once by itself
  (`[resolution] autostart = false` disables that). If the error persists, run
  `varyz daemon start --config <path>`. Without `--config`, `VARYZ_CONFIG` or
  `./config.toml` chooses the archive.
- `port ... is served by another Varyz archive`: two configs share one port. The daemon
  names its archive, and the client refuses to use another archive's daemon rather than
  read and write the wrong rules. `varyz setup` or `varyz init` on the second config moves
  it to a free port; or set `[resolution] port` by hand.
- Agent answers look empty: run `varyz sources --config <path>`. It lists each data source
  as connected or not, with its item count and last sync.
- `NOT_KNOWN`: no admissible tier answered and escalation was disabled.
- `ESCALATED`: use `varyz questions`, `varyz answer`, or the local inbox.
- revoked token: reconnect the client or grant a new non-revoked capability.
- missing model asset: `varyz local-model status`; shared junctions are supported, and a
  local redownload is safe when sharing is unavailable.
