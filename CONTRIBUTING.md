# Contributing to Varyz

Thank you for looking. Varyz is a local-first personal context engine: it ingests somebody's mail,
messages with AI agent, files, calendar and code history into an archive on their own machine, compiles a model
of their life from it, and lets a chosen agent use only the context approved for the question in
front of it. Everything below follows from that.

> **Draft — one thing is still unsettled:** the repository URL, and with it the clone command
> below. The licence and the contribution terms are decided (AGPL-3.0-only with section 7 terms,
> DCO, no CLA) but have not yet been through qualified legal review. Everything else here is how
> the project actually works today.

## Before anything else: never contribute personal data

Not yours, not anyone's. No archive files, no database, no exported messages, no log containing
message text, no screenshot of a real Mirror, no `.env`, no token.

Every test fixture in this repository is synthetic and must stay that way. If you cannot reproduce
a bug without real content, open an issue describing the *shape* of the data — counts, kinds,
ordering, encodings — and we will build a synthetic case together. This is the one rule where
"I was only trying to help" is not a defence.

## Getting set up

```sh
git clone <repository>
cd varyz
uv sync
uv run pytest -q
```

Python **3.12 or newer**, and [uv](https://docs.astral.sh/uv/) for everything. Invoke Python
through uv — `uv run python`, `uv run pytest` — rather than a bare interpreter.

You do not need API keys, a network connection, a GPU or a local model to develop Varyz or to run
its tests. If you find something that does, that is a bug worth reporting on its own.

## The gates

A change is ready when all of these pass:

```sh
uv run pytest -q                       # offline, deterministic, no network
uv run ruff check src tests scripts    # E, F, I, UP, B, SIM
```

A maintainer pushing a release runs one gate more — the publication audit, which
compares the repository against the owner's private archive. It cannot run in CI,
because a runner has no archive: `uv run python scripts/audit_publication.py --gate`.

Tests marked `live` call real cloud APIs and are deselected by default. Do not add one unless the
thing genuinely cannot be tested any other way, and never make it part of the default run.

If your change touches the local web interface, take headless-Chrome screenshots at a desktop and
a narrow width, in both light and dark, and look at them yourself before asking anyone else to.
The interface ships **zero JavaScript** by design; a `<script>` tag is a design change, not a
detail.

## House style

- **Line length 100.** Ruff enforces it.
- **Comments explain *why*, sparingly.** The code says what it does. A comment earns its place by
  recording a decision — why this ordering, why this is fail-closed, why the obvious approach was
  wrong. Do not narrate.
- **`pathlib`, explicit UTF-8 file IO, no Unix-only dependencies.** Varyz is developed on Windows
  and must run on Windows, macOS and Linux. If you touch file locking, process handling or paths,
  say in the pull request which platforms you actually ran it on.
- Match the surrounding code. It has a voice; do not fight it.

## Things this project is strict about

These are not preferences. A pull request that crosses one of them will be asked to change,
however good the rest of it is.

1. **Privacy fails closed.** Unclassified material is treated as the worst case, everywhere. If a
   failure, a crash, a race or a malformed input can result in *more* disclosure rather than less,
   that is a defect in the change.
2. **Nothing is withheld silently.** When Varyz declines to show or send something, it says so and
   counts it. Dropping material quietly is worse than refusing loudly.
3. **Archive text is data, never instruction.** It is quoted from documents the user did not
   necessarily write. Nothing in it may widen a decision, forge a label, or steer a connected
   agent.
4. **Every conclusion carries its evidence.** Varyz does not assert things about a person's life
   without showing what it read, what contradicts it, how confident it is, and what it is missing.
   A feature that produces an unsourced claim is not finished.
5. **The whole loop must run with no keys and no model, and work offline.** Keys change bindings,
   never code paths. Any new capability needs a dry-run path that completes and writes nothing.
   A feature may use the network (the hourly news check does), but nothing may wait on it or
   fail without it.
6. **Measure, or it did not happen.** Performance and quality claims come with numbers, and the
   command that produced them. "Should be faster" is not a result.
7. **No telemetry, and private by default.** Varyz does not report what a user asked, read, or
   stores. A request Varyz makes on its own (the news check) carries nothing about the user or
   their archive. Accounts are coming: data a user syncs to one is theirs and private, and
   becomes public only when they publish it. Anything that leaves the machine is named in
   `docs/PRIVACY.md` before it ships, with how to turn it off.

## Pull requests

- One coherent change per pull request. Split refactors from behaviour.
- Say what you changed, why, and what you ran. Paste the test and lint output.
- Tests are expected. Bug fixes get a test that fails before the fix.
- Say plainly what you did *not* do — a known gap named in the description is fine; one discovered
  later is not.
- Do not weaken an existing test to make a new one pass. If a test is wrong, say so and fix it
  deliberately, in its own change.

## Licensing your contribution

Varyz Core is published under **AGPL-3.0-only**, together with the two supplementary terms that
section 7 of that licence permits: an attribution notice that must be preserved (§7(b)) and a
withheld trademark grant on the project's names (§7(e)). Both are in
[LICENSE.additional-terms](LICENSE.additional-terms), and the practical effect is small — one
line in your legal notices, and pick your own name for a fork. Protocol schemas and lightweight
integration clients are intended to be **Apache-2.0** where broad compatibility requires it. All
of this is pending qualified licence review, and this section is not final until that review
lands.

**There is no contributor licence agreement to sign.** Contributions are accepted under the
[Developer Certificate of Origin](https://developercertificate.org/): add a `Signed-off-by` line
to each commit, which `git commit -s` does for you.

```
Signed-off-by: Your Name <your.email@example.com>
```

That line means you wrote the patch, or have the right to submit it, and are contributing it under
the project's licence — AGPL-3.0-only with the section 7 terms named above. Nothing more. You keep
your copyright; nobody signs anything away.

The deliberate consequence: because contributors have not granted the maintainers the right to
relicense their work, Varyz cannot later be republished under proprietary terms without asking
every contributor. That is intended. Varyz Pro will sell *operations* — inference Varyz pays for,
credentials Varyz operates, integrations Varyz maintains — and never a private fork of this code,
so nothing about this project's commercial model needs the code to stop being open.

## Reporting bugs and asking for features

Open an issue. For a bug, include what you ran, what happened, what you expected, and your OS and
Python version — with no personal content in it. For a feature, describe the situation you are in
rather than the implementation you have in mind; the situation is the part we cannot guess.

Security vulnerabilities do not go in issues. See [SECURITY.md](SECURITY.md).
