# Varyz

**Stop repeating how you want things done.**

You open Claude Code, Codex or any MCP agent and give it a task. Varyz briefs the agent on your preferences, learns from existing AI conversations, answers from what you already decided, and checks your definition of done before the agent reports back. Routine implementation choices stay with the agent. Necessary questions reach you together as short choices with quick answers. The goal is output you do not have to correct.

## Quickstart

Local Varyz is free and needs no account. Native downloads are being prepared;
check [download availability](https://varyzai.com/download). To install from source,
with uv and Git installed:

```powershell
uv tool install git+https://github.com/egrm07/varyz
varyz setup
```

Setup opens in your browser: choose where Varyz learns, connect your AI tools,
then select the sources it may read. Wait for setup to finish and restart your
connected agent. Cloud is optional and has a separate sharing approval.

Already installed? Open **Varyz Setup** from the Windows Start menu, or run
`varyz setup` to change your choices. Existing data is kept.
Agent-assisted and double-click installation are described below.

## Availability and capabilities

Shared Varyz has a separate guest consultation resource and connector. An owner
reviews selected preferences before publishing; recipients can consult them and
ask questions using their own verified account (a free Varyz Cloud email sign-in is
enough). People hear about invitations and questions by email and desktop
notification. The implementation is disabled by default; pilot/native acceptance status and setup are in
[Shared Varyz](docs/SHARED_VARYZ.md).

Agents can now leave concise local work checkpoints and retrieve a recorded
session's completed work, checks and blockers with `record_progress` and
`session_history`. These are agent reports, kept separate from your preferences.
For past conversations, `varyz sessions mine varyz --apply` builds cited local
reports in resumable batches; preview by omitting `--apply`. Agents can use
`mine_sessions` directly. No API charges or transcript uploads.
The live Cloud redesign uses **one project chooser** for confirmed preferences,
eligible history and concise work reports. Review uploads and removals before
approving; future eligible context in those projects then syncs automatically.
General preferences are included; general conversations require choosing General
context. Existing CLI category controls remain supported. The dashboard loads
preferences only when a project opens, in pages of 20; see `docs/CLOUD.md`.
Returning computers open the dashboard after checking their saved account link;
setup also has a small **Skip to dashboard** link. **Choose projects** reopens the
chooser when you want to change sharing.
See [session progress](docs/SESSION_PROGRESS.md) for the contract and owner controls.

The Windows native installer bundles Python and dependencies, opens
consent-based setup in your browser, and retains your data on uninstall. See
[Native installer](docs/NATIVE_INSTALLER.md). The main public download entry point
is [varyzai.com/download](https://varyzai.com/download); release files and checksums
are hosted on GitHub Releases. The initial Windows release is unsigned;
Windows may show a security warning or block it under stricter security policies.

Apple Silicon and Intel macOS PKGs are signed, notarized, stapled and
Gatekeeper-verified. Their signed payloads passed offline installation, repeat
installation and preserved-data removal on native runners. Real-Mac browser setup
and Installer GUI tests remain pending. See [macOS installer](docs/MACOS_INSTALLER.md).

Plugin bundles and the optional paid cloud implementation are described in
[Plugin and cloud](docs/CLOUD.md). Full hosted ChatGPT and optional Claude Cloud
packages combine confirmed preferences with approved synced project history,
timelines and retained-version comparisons. Local Claude Code/Codex packages use
the full local engine. See [plugin capabilities](docs/PLUGIN_PARITY.md) for differences.
Cloud has **no free trial**. Its backend is deployed. Hosted marketplace approval
and phone interoperability remain separate from the local release.
The Cloud account page's **Connect this computer** button reuses a running local
installation for browser sign-in. Setup instructions remain available when local
Varyz cannot be reached. Then choose your projects, review a count-only preview,
and **Approve and finish** to sync and check Cloud retrieval in the same flow.

Prepared release bundles, existing-account activation and the native-agent launch
checks are documented in [Local setup](docs/LOCAL_SETUP.md) and
[Journey checks](docs/PLUGIN_JOURNEY_CHECKS.md).

## Other installation methods

Varyz is not on a package index yet. Its source repository is
[egrm07/varyz](https://github.com/egrm07/varyz). Install from a copy of the repository
or directly from GitHub; final native installer publication has separate release checks.
There are three ways to install it, and the first two need no terminal:

- **Ask your agent.** Open Claude Code or Codex in a copy of this repository and say
  "install Varyz for me". The agent follows
  [Installing Varyz for the owner](docs/AGENT_SETUP.md#installing-varyz-for-the-owner):
  it runs the installer, which opens setup as a page in your browser, and asks you to
  restart it when the page says you are done.
- **Double-click.** Open `scripts/Install Varyz.command` on macOS or
  `scripts/Install Varyz.cmd` on Windows. It installs uv, then Varyz from the folder it
  sits in, and opens the same page. The launchers are not signed yet. If macOS refuses to
  open the file, choose **Open Anyway** in System Settings > Privacy & Security. If
  Windows shows "Windows protected your PC", choose **More info**, then **Run anyway**.
- **A terminal.** `sh scripts/install.sh` on macOS or Linux,
  `powershell -ExecutionPolicy Bypass -File scripts\install.ps1` on Windows.

The installers install the repository they sit in. Copied anywhere else, they install
from GitHub. This needs git. By hand, that is:

```bash
uv tool install git+https://github.com/egrm07/varyz
varyz setup
```

`varyz setup` opens as a page in your browser, where the agents and data sources are tick
boxes: AI conversations start ticked, git history and documents unticked. GitHub is
connected there too, with the GitHub CLI's login in one click or a pasted token; the token
goes to your system keychain only if you run setup with GitHub ticked. Leave the
installer's window open (or the agent running it) until the page says you are done. Setup asks in the terminal instead with
`--terminal`, with any of the flags below, or where no browser can open (over SSH).

`setup` is the whole first run, and Varyz is fully working when it ends. It shows each
step as it goes, with a progress bar and the time left for the long ones (indexing, the graph, downloads), asks before it connects anything, and can be run again at any time. A download can be paused (**Pause download** on the page, Ctrl+C in the terminal) and carries on from what has arrived. A
second run keeps everything already there.

| Step | What it does |
|---|---|
| 1/7 Archive | Creates `~/.varyz/config.toml` and the local database, unless they already exist. |
| 2/7 Daemon | Starts the local service that answers agents' questions (on `127.0.0.1`). If another Varyz archive already uses the port, setup gives this archive its own port. |
| 3/7 Agents | Connects Claude Code, Codex and Cursor where they are installed. |
| 4/7 Data sources | Finds your AI conversation histories, git repositories and documents folder. It lists the exact paths, asks about each one, then indexes the ones you approve on this machine. |
| 5/7 Graph | Builds the graph from what was indexed and compiles the personal model from it, which `lookup` and `interpret` answer from. |
| 6/7 Autosync | Turns on the background sync, which keeps the sources, the graph and the personal model current (see `varyz autosync` below). It waits until a data source is connected. |
| 7/7 AI mode | **More privacy** readies `qwen3.5:4b` through Ollama and reads conversations locally. **Lighter on your device** skips Ollama/Qwen and uses the connected AI for conversation learning only when allowed; simple local classifiers remain available. |

Before the seven steps, setup checks total and available memory, free disk, CPU
cores and GPU headroom. It automatically selects **Lighter on your device** on
constrained machines, including 8 GB Macs. Machines with comfortable headroom get
both choices. The conservative local thresholds are 16 GB installed memory
(allowing hardware-reserved memory), 8 GB currently available, 12 GB free disk,
four logical cores, and Apple Silicon or an NVIDIA GPU that can fit the model
while retaining 30% headroom. Unknown capacity selects the lighter mode.
These are admission thresholds, not measured speed guarantees.

Cloud conversation reading has its own permission, separate from data-source
access and anonymous analytics. Only eligible AI conversation excerpts are sent
by that reader; local documents, repositories and older file-derived rules are
excluded. Conversations can contain project data pasted into chats. Sensitive
and unclassified excerpts are withheld; detected credentials are redacted.
Without permission or a connected AI, learning waits and local fallbacks work.
More privacy disables the automatic cloud reader and starts a bounded local
reader after setup and as new conversation messages arrive. Both modes retain
the working agent's existing, separately granted context permissions.

Terminal setup supports `--ai-mode local` or `--ai-mode conversations`.
`--cloud-conversations` explicitly permits the conversation reader; `--yes`
alone does not. The selected mode and permission survive setup reruns.

It ends with the **Data sources** table: every source, whether it is connected, how many
items it holds and when it last synced. After that comes **GO** (agents are briefed
from this archive) or **NO-GO** with the reason. When an agent was connected and your
past messages to AI agents are indexed, the page ends with one last step: restart the
agent and give it any task. On its first use of Varyz the agent tells you what happens
next and starts a background reader on a smaller model (on by default; say "not now" and
it asks when, and Varyz reads then), which reads those messages for each project's aim, your taste, how you
like work done and what you already decided. A rule it found with strong evidence (you
corrected an agent over it, or said it in two conversations or projects) goes into
effect at once as a *trial rule*, and agents are told you have not confirmed it yet; it
holds for good after 3 tasks over 7 days with no correction. The rest waits for your
review: ask your agent to open it, or run `varyz rules review`, which also shows a
sample of 10 trial rules to keep or drop. New trial rules pause when you keep fewer
than 90% of that sample; `varyz rules trials` shows them, `--off` stops new ones and
`--undo-since DATE` withdraws them. In Claude Code that subagent is `varyz-rule-miner`, which connecting
installs: Sonnet, and only the two Varyz tools that hand it the messages and take what
it found. Nothing needs pasting.

After that first reading, what you say to an agent is read as you say it. Each prompt
nudges the daemon, which reads the AI chats' newest messages into the archive a couple
of seconds later. When a prompt holds an instruction or a preference (not a question),
the briefing asks the agent to start the same subagent on just the last day. It asks at
most every 5 minutes, but at once when you say a rule holds from now on ("from now on",
"always", "never", "every time"). Such a rule goes into effect on trial for your next
task. `[workflows] live_reading = false` turns this off; `live_reading_min_minutes` and
`live_reading_hours` set how often it asks and how far back it reads.

You do not have to rely on the agent to start that subagent. By default
(`reader_runner = "auto"`) varyz starts the reader itself, headless, through Claude Code
(`claude -p`, Haiku) or Codex (`codex exec`, its newest luna model): whichever has more
of your plan left, with only the two reading tools and no saved transcript. It watches
your five-hour and weekly limits while it reads. The full backlog runs only while more
than 30% of the five hours and 25% of the week are left; a live reading while more than
10% and 5% are. A reader that crosses its limit is stopped, keeps what it submitted, and
resumes on its own once the window resets. `varyz rules read --status` shows where it is,
and `varyz rules read --stop` stops it. `varyz rules mine` through a Claude or ChatGPT
sign-in obeys the same large-reading limits.

When varyz cannot run the reader itself, your agent starts it. Connecting adds a short
marked section to Claude Code's `~/.claude/CLAUDE.md` and Codex's `AGENTS.md` that allows
the agent to start that subagent without asking. Everything else in those files stays as
you wrote it, the file is backed up the first time, and `varyz uninstall` takes the
section out again.

Reading is quiet. The daily and live readings run without a word in your chat. Only a
major one, the first or a backlog of 300 or more messages, is mentioned, once and in one
plain line: what varyz is learning, that it runs on your own plan, skips anything
sensitive and removes passwords and keys, and that `varyz rules review` shows what it
learned. It is mentioned again at most once a month.

Before anything runs, preview it with `varyz setup --dry-run`. `--yes` connects every
agent and source it finds without asking. `--source git` (repeatable) offers only the
sources you name, and `--no-sources` skips step 4. The first indexing downloads the local
models it uses: about 1.4 GB, once per archive. Only More privacy downloads the
classifier model once (about 3.4 GB) and, where Ollama is missing or too old,
Ollama itself (about 160 MB on a Mac; 1.57 GB on Windows). Lighter on your device
does not download or start Ollama/Qwen, including when Ollama is already installed.

After setup you rarely need a terminal. Ask your agent for what a command does ("update
Varyz", "review my rules", "connect GitHub", "is Varyz working?"), and it runs the
command for you. Connected agents are told which command fits each request, and which
steps only you can take, such as allowing folder access on a Mac. The commands, if you
would rather type them:

```powershell
varyz task "build a landing page"   # what an agent is told
varyz sources                       # which data sources are connected
varyz connect github                # add GitHub (asks for a token)
varyz update apply                  # install the latest Varyz
```

Every command reads `--config`, then `$VARYZ_CONFIG`, then a `config.toml` in the current
folder, and otherwise the `~/.varyz/config.toml` that setup created, so after setup no
command needs `--config`. Restart your agents once, and the next task you give them
arrives briefed. `varyz hooks doctor` repeats the GO check, and `varyz hooks pause` turns
the briefing off.

The installer scripts in `scripts/` do the same thing: they run `uv tool install` (or
`varyz update apply`, over an installed Varyz) and then `varyz setup`. On Windows with an NVIDIA card, `install.ps1` also installs onnxruntime's
DirectML build, which checks `ask` answers on the card (a median 2.7 s per question
instead of 5.7 s). By hand:

```powershell
"onnxruntime ; sys_platform != 'win32'" | Out-File -Encoding ascii ort-overrides.txt
uv tool install git+https://github.com/egrm07/varyz --with onnxruntime-directml --overrides ort-overrides.txt
```

### Updating

Ask your agent to update Varyz, or run `varyz update apply`. A copy installed from
GitHub gets the latest commit. A copy installed from a folder gets that folder, pulled
first when it is a git clone. Before a database migration, the update backs up the
database. It checks that the new version starts, then refreshes the Varyz files of
every agent you connected: the routing skill, the hooks and the reader. There is no
`varyz connect` to run again. Restart the agents to load the new version. The first
agent session on a new version also refreshes the connected agents, so an update made
some other way (for example `uv tool upgrade varyzai`) needs nothing more. With
`[update] mode = "auto"`, Varyz checks once a day on first use and updates itself. On
Windows it waits for `varyz update apply`, because a running Varyz holds its own files.
With `notify`, or when `auto` is waiting, your agent tells you once per new version.

### News

While you use Varyz, it checks [varyzai.com/news](https://varyzai.com/news/) once an
hour, and your agent tells you about each new post once, in one line at the end of a
reply. The check is a plain request for the news feed and sends nothing about you or your
archive. Feed readers can follow [varyzai.com/news.xml](https://varyzai.com/news.xml).
News is on by default:

```bash
varyz news         # on or off, the last check and the latest posts
varyz news off     # stop checking and passing news on
varyz news on      # start again
varyz news check   # check now
```

### From a checkout

The checked-in lockfile is authoritative. `setup` copies `config.example.toml` to the
config it creates:

```powershell
uv sync --frozen
uv run varyz setup --config config.toml
```

### On a Mac

macOS lets a background program into Desktop, Documents, Downloads and iCloud Drive
only with the owner's permission. Autosync runs through a small app named Varyz, built
on your Mac when autosync is installed (`~/.varyz/data/autosync/Varyz.app`), so the
first time it reads one of those folders macOS asks "Varyz would like to access files in
your Documents folder". Allow opens that folder and nothing else; nothing leaves the Mac.
If you chose Don't Allow, switch it on under Varyz in System Settings > Privacy &
Security > Files and Folders, or skip it and run `varyz sync` yourself. Where the app
cannot be built, the only switch macOS offers is Full Disk Access for the program
`varyz autosync doctor` names, which opens more than those folders; keeping the archive
and sources outside them (for example in `~/varyz`) needs neither. Setup checks this
once autosync is on and, only if macOS kept a folder from it, names the folder and its
source and opens the right settings. The alpha installs no language model beyond the
classifier: `qwen3.5:4b` through Ollama.

### What connecting an agent does

Step 3 runs `varyz connect <agent>` for each agent it connects. That registers a named
caller with a bounded capability, writes the MCP server entry, and installs the routing
skill and the briefing hooks (`SessionStart` starts the daemon if it is down,
`UserPromptSubmit` briefs, `Stop` checks). Other hooks in the client's settings are kept
in place, and the previous file is saved as `.varyz-backup`. Codex asks you to trust new
hooks the first time they run. Turn hooks off with `[agent] briefing_hooks = false` (or
only the stop check with `done_gate = false`); `varyz uninstall` removes them.
`varyz doctor` checks operational state; `varyz doctor --fix` tries a stopped local
daemon and reconnects broken agent credentials/hooks, then checks again. It does
not override pauses, replace another archive's daemon or repair damaged archives.
Reconnected clients need a restart to load fresh credentials.

During AI sessions,
the installed hooks check at session start, on prompts and at turn end; MCP calls
also check the active session credential. Checks run at most once a minute per
session, show a short failure note in the working chat and suppress unchanged
failures. A failure also offers one background repair subagent per archive every
20 minutes through the managed `AGENTS.md` / `CLAUDE.md` instructions. The agent
runs the local fixer, investigates remaining reversible defects and reports a
verified result or required owner action. The main agent answers and continues
its work without waiting for repair, and leaves the repair subagent running
after its own turn ends. Repairs have no ten-minute or single-pass cutoff;
they continue until verified recovery or a concrete owner-action blocker.
The repair agent renews its lease every five minutes through
`varyz doctor --repair-job ID --repair-state running --config PATH`, preventing
duplicate repair offers during long jobs. `fixed` requires a passing doctor
check; `blocked` records that owner action is needed. The client must support
background subagents surviving the parent's response; Varyz's instructions
cannot override a client that forcibly terminates them.
The handoff works without a valid MCP token and avoids recursive repairs in the
rule reader. Varyz automatically installs both mining and repair instructions
in the connected clients' `AGENTS.md` / `CLAUDE.md`, refreshes them at session
launch and MCP startup, and preserves your text outside its managed block.
A paused hook or a client that does not run hooks cannot deliver hook
notes; MCP notes require the agent to call Varyz. No model inference or archive
text is used for these checks.

`varyz hooks doctor` is the go/no-go preflight (daemon, database, rules, data
sources, launchers, the installed clients' hooks, errors) and `varyz hooks pause` /
`resume` is the kill switch.

### Try it without an agent

These examples run from a checkout. With the installed tool, drop `uv run` and
`--config config.toml`. See what an agent would be told:

```powershell
uv run varyz task "Build a landing page for the waitlist" --project lumen --config config.toml
uv run varyz task "Build a landing page for the waitlist" --finish --config config.toml
uv run varyz rules checklist website --config config.toml
```

Teach and review directly:

```powershell
uv run varyz know "Which analytics?" "Cookie-free only." --config config.toml            # general
uv run varyz know "Which accent colour?" "#6d5efc" --project lumen --config config.toml  # one-off
uv run varyz questions --config config.toml
uv run varyz answer <id> "Use short-lived feature branches." --scope general --config config.toml
uv run varyz dismiss --project scratch --config config.toml
uv run varyz knowledge list --config config.toml
uv run varyz rules list --config config.toml
```

Unregistered callers are not trusted implicitly because they use local stdio. They receive
a low-trust identity and strict item/character limits. Registered callers can be revoked.

### Local daemon availability

`varyz daemon on-demand` removes this archive's Windows login watchdog and allows
the first local agent or CLI request to start the daemon. It starts no process
immediately. The first request may take a few seconds; scheduled source sync is
configured separately. Doctor reports an idle on-demand daemon as standby, with
first-use startup still unverified until a real request succeeds.

On Windows, `varyz daemon install --dry-run` previews the login registration;
`varyz daemon install` starts a hidden supervisor now and at each login. It checks
the local daemon every five seconds, recovers refused connections, and retries a crashed
worker. Local CLI requests also start a stopped daemon on demand. The default
address is `127.0.0.1:1207`; an archive's explicit port is preserved. Login recovery
does not enable autosync, change source consent, unpause reading, or add cloud sync.
The computer must be awake and signed in.

`varyz daemon status` shows startup and pause state. Health checks append timestamps
and outcomes to `data/daemon/availability.jsonl`; `data/daemon/status.json` holds the
latest result. `varyz daemon pause` writes `data/daemon/PAUSED` and stops the service;
`varyz daemon resume` removes it and checks immediately. `varyz daemon stop` is a
one-time stop: automatic recovery can start it again. `varyz daemon uninstall`
removes this archive's login entry, pauses recovery, and stops the service without
removing its archive. Add `--config <path>` when managing a separate archive.

## The workflow

```text
 you: "build a landing page for lumen"
   │
   ▼  hook (UserPromptSubmit) ── start_task ──────────────────────────────┐
 agent gets the briefing:                                                 │
   · how you always work          (general rules, every project)          │
   · this project's vision, rules and settled one-off choices             │
   · your general preferences that touch the task                         │
   · definition of done for each kind of work (website: favicon, SEO,     │
     social cards, sitemap, legal pages, phone width, ...)                │
   · the choices it should ask about instead of guessing                  │
   │                                                                      │
   ▼  while working, at every owner-dependent choice ── ask ──────────────┤
   known ─────────► answered at once (from what you told it before)       │
   unknown ───────► ask_owner: the agent puts it to you with its own      │
                    question tool + "always, all projects / just this     │
                    project?"  ─► answer_owner_question stores it forever │
   │                                                                      │
   ▼  before "done" ── finish_check (and the Stop hook, once per task) ───┘
 every item: done and how it was verified, or not applicable and why
```

| Step | MCP tool | Automatic in Claude Code / Codex |
|---|---|---|
| 1. Brief | `start_task` | `UserPromptSubmit` hook injects it with the first task prompt |
| 2. Ask | `ask` → `answer_owner_question` | the routing skill makes the agent ask often |
| 3. Check | `finish_check` | `Stop` hook blocks once per task after edits, with the checklist |
| Learn | `remember_owner_rule` | when you correct how the agent works, it offers to remember |
| Learn | `remember_owner_workflow` | when you steer it through steps in your usual order, it offers to remember the order |
| Learn | `record_workflow_nudge` → `answer_workflow_review` | records whether you took or skipped a step it raised; after 2 skips in a week that cost nothing, asks you once whether to keep the step |

### A way of working vs a one-off choice

Continuation requests include a bounded, attributed snapshot of recorded work:
what was done, checked and still blocked. Named clients and project boundaries apply;
paused or withheld history is never replaced with an older apparently current answer.
Cloud reads only separately approved synced reports. An absent report remains unknown.

`finish_check` returns completion receipts. Straightforward local favicon standards can
be checked against `index.html` and the referenced file; failed or unchecked standards
remain incomplete. For other standards, the agent submits concise `evidence` per rule ID
(passed, failed, or not applicable with a reason). These reports are labeled separately
from independent artifact inspection. Subjective review remains the agent's responsibility.

Learned rules stay on trial until an explicit keep or correction. Seeing a rule in several
briefings, or hearing no objection, no longer confirms it. Historical automatic promotions
remain auditable and are labeled `learned_unconfirmed`; stored owner confirmations stay intact.

Every answer and rule has a scope. `*` is a **general preference** — how you do things,
applied in every project ("every site gets a favicon", "dry-run first"). A project name
is a **one-off choice** for that product only ("lumen's accent colour is #6d5efc"), and
never reaches another project. When the agent relays a question it asks you which one it
is; if you do not say, Varyz guesses and fails narrow (an unclear answer stays with its
project). Measured on 30 labelled answers: 97% right, 0 one-off choices leaked into other
projects (`reports/VARYZ_WORKFLOW_2026-09-24.md`).

### Choices made in any session

When a Claude Code agent asks you a multiple-choice question in any session, the option
you pick is recorded as that project's decision. You don't have to repeat it to varyz. A
later pick to the same question replaces the older answer. An older session never
overrides a newer answer. Autosync does this on every cycle, reading only the question
and your pick, never the rest of the transcript. It skips a question varyz relayed
itself, a question you declined or skipped, and a question about where an answer
applies. A withdrawn pick (`varyz knowledge retract <id>`) is never recorded again.
`varyz knowledge capture` lists what it would record; `[autosync] capture_decisions =
false` turns it off. Codex transcripts have no equivalent and are not read.

### Definitions of done

Varyz ships no rules of its own: every rule is yours. A rule can belong to a kind of
work (website, python-tool, mobile-app, docs, evaluation, automation, ai-integration,
data-connector, dashboard, release, writing, job-application, travel, shopping, cooking,
social-post, events, study), and together they are that work's definition of done.
Kinds of work are detected from the task's wording, and from the project folder only
when the wording names none. You can also file a rule under a kind of your own, such
as `gardening`; it applies when the task names it. Keep the ones the reading found in
`varyz rules review`, add your own with `varyz rules add "..." --domain website`, or
confirm one when an agent offers to remember a correction. `varyz rules checklist`
shows every kind of work, yours included, with its definition of done.

### Workflows

A workflow is the order you guide an agent through a kind of work, and how you guide
each step: a website is colour, then logo, then the page; a product is vision, design,
monetization. `start_task` hands the matching workflow to the agent, marks the steps
this project already decided, and names the step the task is at. When a task starts at
a later step while an earlier one is still open (you asked for the hero before picking
colours), the agent does what you asked, says once that you usually settle colour
first, and offers to do that first. It never blocks.

Add one with `varyz workflows add website colour "logo: three options side by side"
"page content" --domain website`, or confirm one when an agent offers. Autosync also
learns them from your own non-sensitive turns to coding agents and uses them straight
away, marked *learned*. Learning does not copy a slip: when a later step was started
first and then redone ("now redo the hero with the new colours"), or you said you
should have done something first, the step you went back to counts as first. A product
order is learned only from what you said or corrected, never from what a project happened
to discuss first. Every pair of steps needs 3 sessions (or projects) agreeing at least
3/4 of the time. Your own order always beats a learned one, and a withdrawn learned
order is never learned back.
`varyz workflows learn` shows what it would learn (counts and step names only).

When a task starts **new** work at a later step while an earlier one is open, the agent
does what you asked, says once that you usually settle that step first, and records
your answer. A small change ("fix the padding", "the CTA is cut off on mobile") is just
done. Which is which is decided on your machine, never elsewhere: a local model through
Ollama (`qwen3.5:4b`, 68/69 on a held-out set), falling back to the local embedding
(62/69) when Ollama is down or loading it would leave the GPU under 30% free, then to a
verb list (42/69); `[workflows]` in the config picks it. A skip
followed by the skipped-to step being redone once the skipped step was settled is
recorded as rework, and the next nudge gives it as the reason. A step you skip twice
within 7 days, with no rework after any skip of it, is put to you once: keep it, make it
optional (agents stop raising it), or drop it. Older skips expire, and the step stays.
Varyz never changes the order without your answer.
`varyz workflows review` shows how often each step was raised, taken, skipped and
redone, and records your decision (`varyz workflows review <id> logo optional`).

## Keep working while you are away

Tell the working agent **“Keep working while I'm away; collect questions for my return.”**
The agent recognises the instruction and calls Varyz's `away` tool; no local model
classifies absence or chooses your implementation. A vague “I'm busy”, silence, a
deadline, or discussing this feature does not activate it. Say **“I'm back”** to end
the absence and review the decisions together.

`varyz connect claude-code` and `varyz connect codex` install an `away` skill alongside
the routing skill. Reload your client's skills after connecting:

| Client | Activate | Return / inspect |
|---|---|---|
| Claude Code | `/away` | `/away off`, `/away status` |
| Codex | `$away`, or `/skills` → `away` | `$away off`, `$away status`, or natural language |

Claude exposes skills as slash commands. Codex's supported CLI entry point is the
skill picker; Varyz cannot register an arbitrary bare `/away` built-in in every Codex
version. Where the composer exposes skills through `/`, select `away` there. Natural
language uses the same mode in both clients. See the official
[Claude skill commands](https://code.claude.com/docs/en/skills) and
[Codex skill picker](https://developers.openai.com/codex/cli/slash-commands).

**Default scope is the current task.** Reuse its `conversation_id` in Varyz calls;
hooks supply the client session id, and an initial `away` on call without one issues
an id for the agent to retain. A new unrelated task should use a new id. Use project
or all-project scope only when explicitly requested. State is shared by connected
agents using the same archive on this device and survives restarts. A status request
does not end it. Actual task completion ends task scope; a blocked turn does not.

While away, agents check recorded constraints, decide delegated engineering details,
and keep doing useful authorised work. Unknown taste choices use coherent, replaceable
defaults. Questions are queued without notifications, with proposed choices and reasons
kept **separate from confirmed preferences**. Missing goals or permissions block only
dependent work. Agents avoid speculative alternative implementations and stop when
useful work is exhausted. Away mode changes neither client permissions nor budgets,
and does not schedule or keep a stopped agent running.

**Approval prompts stay deferred until return, including the final upload.** Agents
reuse explicit consent for the same action and destination, and check client approval
requirements separately. They prepare blocked actions and artifacts, finish independent
implementation and offline verification, then leave a handoff if nothing useful remains.
They do not invoke an interactive approval while away or mark blocked work complete.
The hosted service returns this policy with away state, task briefings, questions and
completion checks; a full question queue reports an unstored deferred blocker rather
than telling the agent to interrupt the owner. Varyz supplies agent instructions and
queues; it cannot intercept another tool's approval dialog or change client security.

Owner controls from a terminal (use the same config as the connected agents):

```powershell
varyz away on --task <conversation-id> --project lumen --config config.toml
varyz away status --task <conversation-id> --project lumen --config config.toml
varyz away off --task <conversation-id> --project lumen --config config.toml
varyz away on --scope all --minutes 120 --config config.toml
varyz away off --scope all --config config.toml
```

`on`, `off`, `status` and `complete` include a bounded review of pending decisions,
blockers first. Use `varyz answer` or the agent's `answer_owner_question` to record
your answer; `varyz dismiss` closes a question without learning. Provisional choices
and silence never become your preferences. The local mode and review are free.

## What is core, and what supports it

| Layer | What | Surfaces |
|---|---|---|
| **Core: the workflow** | briefing, ask/relay, definition of done, scoped owner knowledge and rules, hooks | `start_task`, `ask`, `answer_owner_question`, `owner_questions`, `finish_check`, `remember_owner_rule`; `varyz task/know/answer/questions/dismiss/rules/connect` |
| **Evidence** | the owner's history, when the task depends on it | `context`, `recall`, `brief`, `lookup`, `interpret`; `varyz search/sync` |
| **Engine** | resolution ladder, daemon, privacy gate, archive | `varyz daemon` |
| **Legacy names** | kept so older agent configs keep working | `resolve` (= `ask`), `advise` (= `start_task`) |

## How `ask` answers: the resolution ladder

| Tier | Source | Result |
|---:|---|---|
| 1 | Append-only explicit owner knowledge | `EXPLICIT` or `USER_CONFIRMED` |
| 2 | Local archive retrieval | Source-addressed `REPORTED`/`OBSERVED` answer |
| 3 | Optional compiled life model | Bounded `INFERRED` answer with provenance |
| 4 | Existing disclosure policy | Release or fail-closed withholding |
| 5 | Durable owner escalation | `ESCALATED`, or `NOT_KNOWN` when disabled |

Every result includes disposition, basis, tier, confidence, timestamp, and monotonic
sequence. It may also include a structured life-model result, withheld markers, an egress
receipt, or an escalation id.

## Main commands

| Command | What it does |
|---|---|
| `varyz away on/off/status/complete` | Set scoped away mode and review deferred questions; `--task` identifies the task, or explicitly choose `--scope project/all`. |
| `varyz doctor [--json] [--fix]` | Operational checks for archive, daemon, agent credentials/hooks, sync and reader; `--fix` tries bounded local repairs and verifies. Exits 1 while checks fail. |
| `varyz setup [--terminal] [--dry-run] [--yes] [--source ...]` | First run, and safe to repeat: archive, daemon, agents, data sources, graph, autosync and the local classifier, then GO or NO-GO. Opens as a page in your browser; `--terminal` or any other flag asks in the terminal |
| `varyz setup --web` | Serve the setup page even where setup finds no browser to open; open the printed link yourself |
| `varyz sources` | Which data sources are connected, how many items each holds, when it last synced, and how to connect the rest |
| `varyz task "..." [--finish]` | Preview the briefing (or finish checklist) an agent gets |
| `varyz rules review/add/list/checklist/retract` | The owner's rules and definitions of done |
| `varyz rules trials [--off/--on] [--undo-since DATE]` / `rules activate [--apply]` | Rules in effect on trial; sort the queue into trial and review (dry run by default) |
| `varyz workflows list/add/retract/learn [--apply]` | The order the owner guides work in (website, product, ...) |
| `varyz know "question" "answer" [-p project]` | Append owner knowledge, general or one project's |
| `varyz knowledge list/retract/capture [--apply]` | What varyz knows, withdrawing an answer, and the picks it would record from agents' questions |
| `varyz autosync install/status/pause/resume/uninstall` | Keep varyz current in the background: a hidden process at login (a Startup entry on Windows, a launchd agent on macOS, a systemd user service on Linux) syncs sources and records decisions every 15 minutes, starting right away; the latest run is in `data/reports/AUTOSYNC_LATEST.md`, failures push to `notify_topic` (ntfy) when set; `pause` is the kill switch |
| `varyz update status/check/apply/pause/resume` | Bring Varyz up to date: a git checkout to the latest commit of its branch (fast-forward only), an installed copy reinstalled from where it came from (GitHub or its folder). `apply` works any time; automatic checks are off unless `[update] mode` is `notify` or `auto`, and then run once a day on first use. The database is backed up before a migration, new code that fails to import is rolled back or reported, and the connected agents' Varyz files are refreshed. Every attempt is logged in `data/autosync/updates.jsonl` |
| `varyz news [off/on/check]` | News from varyzai.com/news, checked once an hour while Varyz is in use and passed on by your agent, each post once. On by default; `off` stops the check and the notices |
| `varyz cloud promotions [status/off/on]` | Inspect, disable or reset local Cloud-offer dismissal. Offers remain inactive until service launch and integration review; resetting does not enable those gates |
| `varyz wrapped [--handle @you] [--dry-run] [--exclude id] [--include-project id]` | "My Way": a shareable card (SVG + PNG) made from your own general rules, previewed before anything is written |
| `varyz questions` / `answer [--scope]` / `dismiss` | The durable owner queue |
| `varyz hooks doctor/pause/resume` | Workflow preflight and kill switch |
| `varyz resolve "..."` | Resolve an owner-specific question through the daemon |
| `varyz daemon start/status/stop/run` | Operate the loopback resolution service (default `127.0.0.1:1207`) |
| `varyz daemon check` | Check the local service once and recover a stopped daemon |
| `varyz daemon install --dry-run` / `varyz daemon install` | Preview or register Windows login startup and a health check every five seconds; independent of autosync |
| `varyz daemon pause` / `varyz daemon resume` | Stop automatic recovery and the service, or resume and check immediately |
| `varyz daemon uninstall` | Remove login startup, pause recovery and stop the service; retain the archive |
| `varyz daemon on-demand` | Remove login recovery and allow first-request startup; start no process immediately |
| `varyz agent ...` | Register, grant, inspect, and revoke callers |
| `varyz connect codex\|cursor\|claude-code` | Wire an agent: MCP entry, routing skill, briefing hooks |
| `varyz connect ai-conversations\|github` | Connect a data source that needs a choice or a token (`setup` finds the others) |
| `varyz mcp` | Serve the workflow tools and the evidence tools over stdio |
| `varyz search "..."` | Inspect the local archive directly |
| `varyz sync [connector]` | Resume configured sources idempotently |

The Mirror, Mirror benchmark, RAG `ask`, `understand`, the local web inbox (`varyz serve`, removed),
the Gmail, Google Calendar and Drive, calendar-file and personal-archive connectors (in the
source tree, left out of the package), and the semantic graph are not alpha surfaces. The
semantic graph is read by a 7.4 GB local model (gemma) that the alpha never downloads:
`varyz local-model install` is left out of the package, and `varyz graph rebuild` builds the
deterministic graph instead, from explicit statements only, with no model. The tier-3
personal model compiles from it with `varyz model compile --literal-only --run`, which is
what `lookup` and `interpret` answer from. Setup builds both, and autosync compiles the
model again after each graph update, so neither needs running by hand. varyz does not read WhatsApp,
Telegram, Instagram, Google Contacts or browser history at all: those connectors were removed
on 2026-09-27. The alpha is the CLI above and the MCP server
(`varyz mcp`), reading AI conversations, git, GitHub, documents and manual notes.

The alpha's automated context boundary is the same everywhere: sources listed in
`graph.excluded_sources` do not enter the graph, personal-model compile, `resolve`, MCP
`context`, MCP `recall`, or MCP `brief`. The raw rows remain local and `varyz search` can inspect them
deliberately; exclusion avoids spending inference on them and prevents agents from using
them implicitly. Conversation analysis reads only your sessions with coding agents.

## Local data and model assets

`config.toml` points at `data/`. To make an older home config use an existing
archive, back it up and replace its contents with
`redirect_config = "C:/path/to/the/canonical/config.toml"`. The CLI, environment
override and agent connections then resolve the same config; the old archive
stays on disk. Broken or cyclic redirects stop with an error.

`varyz doctor` reports overdue reader quota checks and orphaned queues.
`varyz doctor --fix` resumes those queues. Unchanged owner-action blockers do
not repeatedly spawn repair agents; abandoned repairs retry with backoff.
An explicit `varyz connect` pins the runtime that maintains the integration,
so a different checkout cannot overwrite it during automatic refresh.

The pivot snapshot keeps archive ground truth and
paid-for extraction/annotation caches, while dropping old Mirror and compiled-model
generations. `data/models` and `data/runtime` may be directory junctions to existing
read-only assets. If those links are unavailable, `varyz local-model install` (source tree
only; the alpha leaves it out) can download assets into this tree.

The alpha needs no API key and no network at runtime. On its own it makes one request:
the hourly news check (`varyz news off` stops it), plus the update check if you turn that
on. The daemon binds to `127.0.0.1` by default. Notifications carry only an escalation id and urgency; question text stays inside
an authenticated local surface. Sensitive and unclassified content fails closed.
The personal archive is sensitive by source, and so is anything an older archive still holds
from the chat apps varyz no longer reads; your turns to coding agents are not, and any entry naming a therapist, a diagnosis, a
medication, a salary or an account number is. A sensitive label never drops on sync, so
after changing those rules run `varyz privacy release` (a count) and then `--apply`.
Documents never go to a cloud model for Varyz's own processing (extraction, annotation,
enrichment), whatever their label; only your past messages to AI agents are read in the
cloud to learn your rules. An agent that looks something up still gets the excerpt it asked for.

## Verification and evaluation

```powershell
uv run pytest --basetemp=.pytest-tmp/final
uv run ruff check .
uv run python scripts/evaluate_gbrain_techniques.py
uv run python scripts/evaluate_title_boost.py --model hash --split dev
uv run python scripts/run_trr_fixture.py --split dev --out reports/trr_dev_answers.json
uv run python scripts/trr.py reports/trr_dev_answers.json --split dev
```

Trusted Resolution Rate (TRR) reports correct answers, correct abstentions, appropriate
escalations, fabricated answers, mis-sourced answers, wrongly withheld answers, and the
abstention rate separately. The held-out split may be scored exactly once at the end of a
build and must never become tuning data. The dated report in `reports/` records the actual
gates and comparison with the frozen A/B/C/D baseline.

The title-boost experiment uses public synthetic fixtures and keeps adaptive scanning
in both arms. The default command above runs only development cases without a model
download. `--model cached` uses an existing local MiniLM; `--challenge` runs the separate
counterexample audit. General title boosting remains disabled after that audit exposed
wrong-first-source regressions. Reports stay local under `reports/`.

## Scope

The local engine runs on one device over loopback and requires no hosted service.
The optional Cloud service has accounts, billing and hosted MCP access to selected
confirmed preferences; see [Plugin and cloud](docs/CLOUD.md). The optional
[approved Cloud history](docs/CLOUD_ARCHIVE.md) build adds project and General
snapshots with read-only search/fetch; deployment and real phone verification
remain pending. Push and multi-device archive editing remain unbuilt. The contracts
preserve device identity, transport-independent callers, append-only supersession,
a content-free notifier and a self-contained ledger.

Varyz is private software for authorized personal context. Retrieved text is untrusted
data, not agent instruction. Review [`NOTICE`](NOTICE) and [`LICENSE`](LICENSE) before
redistributing it.

Varyz and the Varyz name identify this project. The AGPL license covers the source code;
it does not grant trademark rights. Forks may use the code under the license but must use
a different product name and branding; see `LICENSE.additional-terms` and `NOTICE`.

## What is free, and what Cloud supplies

Every local capability is free and open. Nothing in the connectors, archive, resolver,
privacy gate, retired Mirror code, installers or local MCP integration is metered.
Future local capabilities are absent because they are unbuilt, not because they are
locked, and they will be free when built.

Cloud charges for managed hosting and account access; its source remains open under
the same licence. **No answer is ever improved by paying.** The same archive and
question use the same resolution rules regardless of who operates the hardware.

**Commercial and company enquiries** are welcome at hello@varyzai.com.

To remove the installed package after unregistering integrations, run
`uv tool uninstall varyzai`.

After editing `scripts/install.ps1` or `scripts/install.sh`, regenerate their
double-click launchers and local website copies with `python scripts/sync_installers.py`.
The installer tests compare these copies with their source scripts byte for byte.

## The Varyz network (opt-in, 2026-10-18)

One popup, **Join the Varyz network**, governs everything Varyz sends. Its one button,
**Share anonymized data**, shares daily usage counts and anonymized examples of how you
correct your agents (questions, answers, corrections and kept or rejected rules) created
after you opt in. The small x, or Escape, means no. Setup starts with sharing off and does
not show it: the popup appears when you first save a rules review after your first 3
conversations and 24 hours with Varyz, and after a no it is offered again at most once,
30 days later. Terminal setup asks the same question with a default-no prompt, and
`setup --yes` never enables it.

Examples are scrubbed on your computer first: people, projects, emails, links, paths,
numbers and handles become placeholders, and anything holding a credential or withheld
text is dropped. Cleaning can miss something. Anonymized examples may be licensed to AI
companies to improve their models; that is how the network is funded. Your chats, files
and archive never leave. Turning sharing off deletes what you shared from the server.
See `docs/ANALYTICS.md` and `docs/PRIVACY.md`.

```powershell
varyz data status
varyz data preview
varyz data off
varyz analytics doctor
```

## Rule-system shadow experiment (2026-10-04)

A's explicit state projection and B's conservative compiler run beside the current
system. Candidates, trials, confirmed rules, visions, workflows and revision history
remain distinct. **Current output still governs agents.** Collection is local and
opt-in; it does not change rules, promote trials, ask new questions or upload records.

```powershell
varyz rules shadow probe --dry-run
varyz rules shadow control enable
varyz rules shadow status
varyz rules shadow comparisons --limit 10
varyz rules shadow doctor
varyz rules shadow control pause
varyz rules shadow control resume
varyz rules shadow control disable
```

Registered MCP briefings, hook briefings/finish checks and registered daemon answers
produce paired observations under `data/rules/shadow/shadow.sqlite3`. Prompts, questions,
rule text and answers are not copied into the journal. Comparison IDs refer to the
existing immutable archive. `probe --no-dry-run` records an explicitly marked probe,
excluded from prospective task counts. `review <comparison-seq> <better|worse|equivalent|unclear>
[development|held_out|replication]` appends a judgment; it never activates B.

The answer experiment covers exact recorded preferences only. Permissions always need
fresh review in the proposal; paraphrases and model answers are outside its coverage.
Unknown topic/strength metadata remains visible. Packet differences and shorter text
are diagnostics, not proof of better decisions. See `docs/RULE_SHADOW.md` for the
sampling limits, evaluation gate and persistent-process refresh requirement.
