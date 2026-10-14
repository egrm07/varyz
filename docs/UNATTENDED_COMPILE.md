# Running the compile unattended

An eight-hour personal-model compile is a long single-process write on a laptop.
The compiler is already built to survive being stopped; what follows is what
stops it, and what now catches each one.

Start it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1
```

Watch it from another shell with `Get-Content data\compile-run.state.json -Raw`,
or the run's `heartbeat.jsonl`. Stop it with `New-Item data\compile-run.stop`;
the supervisor kills the attempt at its next poll and writes its report.

## What is already durable

- **Every annotation is committed as it is validated.** A cached annotation is
  keyed by the episode, the exact source revisions it read, the model, the
  prompt version and the schema version, so a later run reuses it only where
  reusing it is still correct. Killing the process loses at most the group in
  flight.
- **Every candidate window that has been read is kept.** Reading the candidate
  windows is the second long inference stage and used to be bought again on
  every attempt: three attempts of the 2026-08-31 run each read all 324 windows
  and then failed downstream, discarding about ninety minutes of reading each
  time. A reply is now keyed by the instruction it was asked under, the request
  payload, the schema, the adapter and the model, so a re-run after a downstream
  failure picks the windows back up and a changed prompt or model does not. The
  synthesis stage's proposal windows are kept the same way and in the same
  table.
- **The reading plan is deterministic.** The same archive and the same budget
  choose the same episodes in the same order, so a restart replays the cache
  and continues where the last attempt stopped rather than reading elsewhere.
- **A generation is activated only when it is whole.** A run that stops early
  records itself `partial`, stores its progress, and leaves the previously
  active model in place. There is no state in which a half-read archive becomes
  the model being answered from.
- **The locks are held by the kernel, not by a file's contents.** A killed
  compile leaves no lock to clear; the byte lock dies with the process.

## What used to end a run, and what catches it now

| Failure | What happened | What catches it |
|---|---|---|
| The machine sleeps | The run stops for the night | `SetThreadExecutionState`, reasserted every poll |
| The model server dies or hangs | Every group fails, each failure is one warning, and the compile reads the rest of the archive producing nothing | Twelve consecutive failed groups stop the run as `provider_unavailable`; the supervisor kills the server, clears its lease and restarts |
| The compile process dies | The run is over | The supervisor restarts it against the same deadline; the cache makes the replay nearly free |
| A sync starts underneath it | Ingestion takes the writer lock the compiler needs, and a graph rebuild changes the plan the cache is keyed to | Autosync is paused for the duration and restored afterwards |
| The disk fills | SQLite discovers it mid-write | The supervisor stops the run while several gigabytes remain |
| Nothing moves | Nothing says so | No new annotation, record, database growth or log line for 45 minutes restarts the attempt and the server |
| Something else takes the GPU | Another project's model host loads onto the same 12 GB card; every candidate window times out at 600 seconds and comes back empty, and the run keeps going because the disk, the page file and the commit charge are all fine | The supervisor refuses to start with less than 9 GB free on the card. After that the floor is learned rather than configured: our own 12B at six slots leaves about 1.7 GB free, so the lowest reading of the first ten minutes, less a gigabyte, is what the card looks like with only us on it, and the log records a drop below *that*. Whatever the card says, the compile stops itself after twelve consecutive empty windows rather than persisting a Mirror built on the fraction that answered |
| A failed run's rows stay forever | Every dead generation keeps its records, and nineteen of them held 120,608 records between them; the database grew until the disk guard stopped the next run | `varyz model prune` discards generations that never activated, and the pages come back for the next compile to write into |
| A stage sends the whole archive in one prompt | The candidate and synthesis stages were each handed every observation at once; the server refuses the request, and the compile either dies at the stage or activates an empty Mirror | Both stages read the pinned set a slot-sized window at a time, one window failing is one stretch unread, and tests assert the window still fits the slot |
| An agent has Varyz connected | `uv run` re-syncs the project before each attempt, the sync rewrites `.venv\Scripts\varyz.exe`, and a running MCP server holds that file open, so every attempt dies with `os error 32` before the compile starts | Every `uv` call in the supervisor passes `--no-sync`. The project is installed editable, so the working tree's source still runs; what is skipped is a reinstall nobody asked for |

That last row cost four attempts in nine minutes on 2026-08-30 and is worth
stating plainly, because it looks like a compile failure and is not one: the
compile never ran. `uv run` syncs first, a sync replaces the console script,
and Windows will not replace a file another process has open. The process
holding it is `varyz.exe mcp` — which is to say, the product working normally
in somebody's editor. Two earlier overnight runs failed on the same machine
for reasons recorded as `provider_unavailable` and `low-disk`; this one would
have been recorded as `attempts-exhausted`, and none of the three was the
reading plan being too large.

The window row is the one that had never been reached before. Annotation is
the eight hours, so it is the part that had been measured; every stage after
it had only ever run on a few hundred episodes of test archive, where the
whole pinned set does fit one prompt. A generation on 2026-08-15 died at the
candidate stage with `400 Bad Request ... request (52593 tokens) exceeds the
av`, and the checkpoint table shows no generation has ever reached `persist`.
Synthesis would have failed more quietly: its refusal is caught as a
malformed proposal, which returns no candidates, which activates a model that
understands nothing. Sizes measured on this archive: a candidate observation
is 458 characters on the wire and a synthesis observation 561, against a
16,384-token slot that owes 4,096 of them to the reply.

The stall clock is armed only after the first new annotation of an attempt.
Identity resolution and episode reconstruction legitimately run for a long time
and write nothing to watch, and killing them would repeat them.

## Which silence you are in

On a warm archive the annotation stage is a cache replay of a few minutes and
every hour after it belongs to one of three stages that ask the model. Each
prints its own progress line, so a silence is only worth worrying about when
nothing has printed for longer than one window takes:

| stage | how many | what caps it | grep for |
|---|---|---|---|
| candidate windows | follows the observations — 799 on this archive | nothing; the annotation budget does not count it | `candidate window N of` |
| proposal windows | up to 120, sampled by an even stride | `max_proposal_windows` | `proposal window N of` |
| evidence search | concepts x lanes, 12 x 4 by default | `max_concepts`, `retrieval_windows` | `searching evidence for` |

All three read on the same slots as the compile: `proposal_concurrency`
defaults to 1, but the runtime hands the compile's own concurrency to the
synthesis policy, so the proposal stage is not serial.

## Doing the eight hours in parts

The run is resumable across processes, not only across restarts, so the eight
hours can be spent in several sittings:

```powershell
# a part: read 5000 new episodes, then stop
powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1 -PartCalls 5000
# ...repeat whenever it suits...
# the last part: no cap, so it finishes the plan and activates
powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1
```

Each part reads that many *new* episodes and stops. Cache hits are free and do
not count against the cap, so a part never re-pays for what an earlier part
read; coverage grows strictly and the parts converge on the same model an
uninterrupted run would have produced. Every part before the last one ends
`partial` and activates nothing — the previously active model keeps answering
until a part finishes the plan.

`-PartCalls` is preferred over `-DeadlineHours` for this. A call cap is
deterministic: the same number of parts reads the same episodes whatever the
machine was doing at the time.

What a part does pay for is the work before annotation — identity resolution
and episode reconstruction are recomputed each time, and neither is cached.
Fewer, longer parts are cheaper than many short ones.

## Two clocks, and why only one of them is set

The compile has its own annotation budget — eight hours of reading at the
measured rate — and it stops there of its own accord. It also accepts a wall
clock deadline, and passing one guarantees the run ends `partial`: progress
stored, nothing activated. So the supervisor does not pass one. Its `-Hours 8`
is the budget it reports and watches against; its own hard stop is `-Hours`
plus `-GraceHours 6`, fourteen hours, and it only fires if the compile has
overrun its budget by six hours.

Pass `-DeadlineHours <n>` if you want the compile itself cut off at a wall
clock time. That is a deliberate choice to end with a partial generation, worth
making only when the annotations matter more than the model.

## Exit codes

`0` the generation compiled and activated. `3` the compile stopped short:
progress stored, nothing activated, every annotation kept. Anything else is a
crash, and the supervisor retries it after a backoff.

`3` is not one outcome, and the supervisor reads `budget_exhausted_by` to tell
them apart. `deadline` and `call_budget` are the compile stopping where it was
told to, so the supervisor stops too and reports `budget-reached` — run it again
to carry on. `provider_unavailable` is twelve consecutive failed groups, which
is the model server being gone rather than the reading being done, so the
supervisor restarts the server and continues into the next attempt. A run that
looks stuck in a retry loop at exit 3 is that third case, and its cause is the
server, not the plan.

The supervisor itself exits `0` for a completed or budget-reached run and `1`
for anything else, and writes `reports\COMPILE_RUN_<stamp>.md` either way.

## What is still manual

A reboot ends the run. Restarting the script afterwards resumes from the cache,
but nothing restarts it for you, so an eight-hour window wants Windows Update
out of the way first.
