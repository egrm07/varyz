# Security

Varyz can index a person's mail, messages with AI agents, files, calendar and code history
on their own machine. Optional Varyz Cloud stores selected preferences and separately approved
history. A vulnerability can expose personal information locally or across hosted accounts.
Please treat it that way, and we will too.

## Reporting a vulnerability

Report privately, not in a public issue.

**Use [GitHub's private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository.** That is deliberately the only channel: it keeps the report private until a
fix exists, it threads discussion in one place, and it means no maintainer's personal mailbox is
the security boundary of a product about privacy.

If you cannot use it — you have no GitHub account, or the repository is unreachable — open a
public issue containing **only** the words "security report, please contact me" and no detail at
all, and a maintainer will arrange a private channel.

Maintainer: **Egor Menshikov**.

Please include what you were running (`varyz --version`, OS, Python), what you did, what happened,
and what you expected. A proof of concept helps enormously.

**Never include personal data in a report.** Not your archive, not a database file, not a log with
message text in it, not a screenshot of the Mirror. If a bug only reproduces with real content,
say so and we will work out a synthetic reproduction together. A report that leaks your own
private life to fix a privacy bug has cost you more than the bug did.

## What we will do

- Acknowledge within **3 working days**.
- Tell you within **10 working days** whether we consider it a vulnerability, and if so our
  assessment of its severity and our intended fix.
- Keep you informed while we work, and credit you when it ships unless you prefer otherwise.
- Disclose publicly once a fix is available. We will agree the timing with you; if we cannot
  agree, we will not sit on it indefinitely — 90 days is our outside limit.

Varyz is currently maintained by one person. These are honest targets, not a commercial SLA.

## What counts

Varyz has local components, an optional hosted Cloud service and connected-agent integrations.
These boundaries are in scope:

- **Anything that gets private content out of the machine unapproved.** The egress gate — secret
  blocking, sensitivity classification, scoped approval, the exact-path preflight and the receipt
  — is the security boundary of this product. A route around any of it is the most serious class
  of bug Varyz has.
- **Fail-open behaviour.** Unclassified material is meant to be treated as the worst case. Any
  path where a failure, a crash, a race or a malformed input results in *more* disclosure rather
  than less.
- **Content that acts.** Text in the archive is quoted from documents the user did not
  necessarily write. Anything that lets that text change what Varyz does — widen a decision, forge
  a label, instruct a connected agent — rather than being carried as data.
- **The MCP surface.** Anything that lets a connected agent obtain material the user did not
  approve for the current question, including through tool arguments.
- **Local privilege and storage.** Secrets recoverable from disk or logs, tokens not encrypted at
  rest, an archive or vault readable by other accounts on a shared machine, path traversal in an
  import or export.
- **The local web interface.** It binds to loopback and includes browser scripts. Injection,
  a route that ignores the display policy, or unintended access from other machines is in scope.
- **The hosted Cloud service.** Authentication, session and OAuth boundaries, cross-account or
  cross-project disclosure, unauthorized synchronization, sharing, exports, deletion and access
  after revocation are in scope. See `docs/PRIVACY.md` for hosting and retention disclosures.
- **Installation and update paths**, including the installers and the managed local-model download.

Out of scope: anything requiring an attacker who already has the user's unlocked machine and
account; the contents of the user's own archive being visible to the user; findings from automated
scanners with no demonstrated impact; and the deliberate design decision that Varyz stores personal
data locally in the clear under the user's own account, which is documented rather than accidental.

## Supported versions

Varyz is pre-release. Only the latest published version is supported.
