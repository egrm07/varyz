# Plugin submission handoff

Updated 2026-10-12: locally prepared packages now use their actual manifest
versions: **ChatGPT 0.1.8** and **Claude Cloud 0.1.6**. Generate into a new folder
with `scripts/plugin/prepare_submission.py --output <candidate-folder>`; existing
handoffs are preserved. Earlier publisher uploads and native acceptance records
below describe older packages and do not certify these candidates. Directory
submission, native reinstall/fresh-chat setup, verified publisher settings and
country targeting remain open for the current packages. Initial-review metadata
now contains exactly five positive and three negative cases; the two additional
setup scenarios remain in `docs/submission/REVIEWER_GUIDE.md`. The existing synthetic
reviewer passed current live OAuth, 17-tool discovery/authentication, eight
preference/history calls, management denial, token refresh and revocation on
2026-10-12. This is protocol evidence, not native-host acceptance.

The handoff also works from a clean source checkout without the private website.
To include policy HTML for a separately authorized deployment, add
`--legal-dir website`; both `privacy.html` and `terms.html` must exist. Without
this option, no policy HTML is copied and reviewers use the configured live URLs.
Policy HTML is never included in the hosted plugin ZIPs.

Updated 2026-10-07: the same Varyz publisher draft now contains the combined
**0.1.7** hosted-server and skills package. Fresh MCP scan passes; metadata reports
No Issues. Publication and review submission are still pending. Current evidence
and remaining platform checks are in
[the single-install report](submission/PLUGIN_SINGLE_INSTALL_2026-10-07.md).
The historical 0.1.1 artifacts below are retained as earlier evidence.

> Prepared 2026-10-05. **Local review candidates are prepared; submission is blocked by the live and platform gates below.** No package has been submitted or published.

Updated 2026-10-06: the ChatGPT review ZIP was uploaded as a draft in the Varyz
organization. Its starter-prompt finding was corrected to three prompts and a
replacement upload was accepted; metadata reports No Issues. MCP connection and
domain verification are complete; the fresh MCP scan reports no issues and all
three skill checks pass. Review information was saved on 2026-10-06, including
the verified public native MCP video URL. The owner reports the Claude upload complete; native behavior has
not yet been verified. Draft uploads are not directory submissions.

The owner subsequently completed ChatGPT OAuth. The publisher shows Authorized and
discovered the MCP tools. The public OpenAI ownership challenge is served at
`https://cloud.varyzai.com/.well-known/openai-apps-challenge`; exact-content, health
and unauthenticated MCP rejection checks pass. After the owner completed Verify
Domain/Continue, the native publisher showed Configured, Authorized and Domain
verified. A fresh scan discovered all 13 tools and reported no issues.

The owner requested personal installation of the exact review ZIP. Desktop control
confirmed the ChatGPT personal account `egrmenshikov@gmail.com`, uploaded the archive
through Plugins / Personal / Add, and verified the resulting Varyz detail page:
version 0.1.1, one MCP server and three skills. Personal test-copy ID:
`Plugin_c29c0a76fec481919ffd25a1b036ddd4`. This proves archive acceptance in the
personal library; that copy's OAuth connection and native skill/tool behavior still need testing.
On 2026-10-07, its exact personal-library entry still exposes only "Open in desktop
app". Browser Work mode lists only the separate MCP test connection. Native desktop
control is unavailable in this session, so this gate remains open.

## Artifacts and implementation

On 2026-10-06 the dedicated synthetic reviewer was connected through ChatGPT
web's personal MCP test flow to the same hosted `/mcp/plugin` endpoint. All five
positive and three negative scenarios were exercised, with a citation follow-up
and a Varyz-specific clarification for checkout. Rule persistence and revoked
fresh access were checked. The narrated, edited native screenshots are available
at the public URL in `docs/submission/review-assets.json`. Anonymous download,
seeking and browser playback pass. This personal MCP connection is distinct from
the full uploaded desktop skill bundle; that bundle and Claude remain unverified.

ChatGPT candidate: `dist/submission-0.1.1/varyz-chatgpt-0.1.1-review.zip`.
Claude hosted candidate: `dist/submission-0.1.1/varyz-claude-cloud-0.1.1-review.zip`.
Claude source folder for a dedicated publisher repository:
`dist/submission-0.1.1/claude-repository`. This contains generated plugin files only;
the private application checkout is excluded. `checksums.json` covers the handoff.

The hosted version is **0.1.1**. ChatGPT uses portable `plugin.json` and `mcp.json`,
with a compatibility overlay. It has one Streamable HTTP MCP at `/mcp/plugin`,
five positive and three negative initial-review cases, listing URLs, release notes,
icon, README and licence terms. Claude declares the same URL with `type: http`,
README, manifest and the shared skills. The full local Claude Code/Codex packages
retain local runtime and hooks; hosted functionality does not reproduce them.

The new `varyz.plugin` scope requires the explicit plugin resource and fresh consent.
Old grants cannot gain history or preference writes through a refresh. History stays
read-only; plugin tokens cannot upload, approve projects, export, bill or delete accounts,
even if issued under the local client identifier. Dashboard computer-connect intent
cannot substitute for consent to this broader plugin scope.

## Completed evidence

- Claude CLI validates both the hosted and local plugin manifests.
- Synthetic in-process account tests cover all 13 tools, old-token denial,
  account/source isolation, revocation, OAuth consent, PKCE and refresh scope.
- Additional isolation checks use Alice and Bob with the same document/rule IDs,
  separate private canaries, retained versions and pending questions. Both ChatGPT
  and Claude plugin configurations reject cross-account reads/writes and ignore or
  reject spoofed account selectors. The maintained Cloud/security run passed 117
  checks on 2026-10-06; package checks passed 17. Live reviewer OAuth/tool checks
  passed again, and the test grant was revoked. This does not replace native testing.
- Reviewer provisioning is implemented and tested against a temporary database;
  it creates isolated synthetic data and a password hash, without reading owner records.
- After explicit owner approval, isolated server validation passed with 19,880 synthetic
  chunks. The candidate was deployed with the existing environment and rollback backup.
  Production health and discovery/access checks pass for all three MCP routes.
- The synthetic reviewer is provisioned. Live sign-in, CSRF consent, PKCE, 13 tools,
  eight preference/history calls, management denial and refresh pass. The test connection
  was revoked; credentials remain in OS keyring. The reviewer script's production
  host-only CSRF cookie mismatch was fixed; HTTP/HTTPS regressions pass (8 checks).
- Final maintained plugin, Cloud and retrieval run: **183 passed** in 64.59 seconds.
  An earlier run had one stale generated archive failure; regeneration fixed it.
  Ruff and generator drift checks pass on the touched implementation/package files.
  The current local companion wheel/source/ZIP were rebuilt offline separately from
  the existing native installer candidates.
- Website, privacy, terms and support URLs returned HTTP 200 on 2026-10-05;
  support exposes a contact route. On 2026-10-07 the live privacy page already contained
  the approved-history section. Its remaining stale deployment details were corrected
  and published from the canonical notice. Cloudflare uploaded only `/privacy.html`;
  the live body matches the reviewed local page. Desktop/mobile screenshots and the
  exact deployment version are in `reports/launch_gates_2026-10-07`. Terms were not
  changed by that privacy-only deployment.

## Remaining release gates

| Gate | Current state | Concrete next step |
| --- | --- | --- |
| Deploy `/mcp/plugin` | Passed isolated staging; deployed and healthy after owner approval | Complete; retain backup and rollback evidence |
| Persistent reviewer | Provisioned; fresh live OAuth and tools pass; test connection revoked; credentials and instructions saved in the authenticated OpenAI review form on 2026-10-06 | Keep credentials out of packages, logs and video |
| Native Claude installation | Owner reports manual upload complete; native verification pending | Inspect installed package, connect and run all cases |
| Native ChatGPT / publisher identity | Publisher configured and authorized; native web MCP scenarios exercised; saved form has 5 positive and 3 negative cases and the verified video URL; full ZIP 0.1.1 exposes all three skills in the personal library | Complete the full installed desktop skill test; browser MCP evidence cannot close this gate |
| Real video walkthrough | 4m14s narrated edit of actual native MCP results; public video verified anonymously, including seeking and browser playback | Complete for MCP evidence; retain citation/clarification and raw-trace limitations |
| Public privacy | Canonical update published 2026-10-07; approved history, current processors, application backups and separate provider disk backups disclosed; live page matches reviewed local body | Fixed security-log expiry and billing-record retention remain public paid-launch checks |
| Public terms | Not changed by the 2026-10-07 privacy-only update | Retain the terms/service release checks before paid launch |
| Claude repository | Isolated folder prepared; not published | Publish only the reviewed plugin folder to a publisher-controlled GitHub repository, then validate in Claude portal |
| Platform review and legal agreements | Not requested or accepted | Submit only after all preceding gates and owner authorization; owner accepts binding directory terms |

## Commands after authorization

```powershell
.venv/Scripts/python.exe scripts/plugin/build.py --zip-hosted
.venv/Scripts/python.exe scripts/plugin/build_archive.py --zip
.venv/Scripts/python.exe scripts/plugin/prepare_submission.py
.venv/Scripts/python.exe deploy/cloud/deploy.py --host 2.31.3.96 --keep-env --archive --stage-only
# Production switch uses the same command without --stage-only, after staging passes.
.venv/Scripts/python.exe scripts/plugin/reviewer.py --seed
.venv/Scripts/python.exe scripts/plugin/reviewer.py --check
.venv/Scripts/python.exe scripts/plugin/probe_public.py
```

Actual submission needs native evidence and secure reviewer access. A valid ZIP,
successful HTTP probe or CLI validation does not prove platform acceptance.

## Platform requirements checked 2026-10-05

[OpenAI submission requirements](https://developers.openai.com/plugins/deploy/submission)
require one connected MCP per plugin, exactly five positive and three negative initial
review cases, a reviewer-accessible video and immediate reviewer account access.
Credentials belong in secure dashboard fields, never the ZIP. Portal:
`https://platform.openai.com/plugins`. Country targeting and publisher legal/identity
settings must be checked in the authenticated dashboard; no targeting is invented.

[Claude directory requirements](https://claude.com/docs/directory/publish) require a
plugin submission and a separate connector submission for the publisher's remote
server, from the same organization. Use the same URL to avoid duplicate tools.
Plugin folders come from GitHub and must be public before listing goes live. The
directory requires a README of at least 40 words and a licence, and native surface
testing remains separate from `claude plugin validate`. Portal:
`https://claude.ai/directory/manage` (the signed-in Pro account exposes it; zero
submissions were present during inspection).

Full local installation, upgrade and clean-device release gates remain in
`docs/PLUGIN_JOURNEY_CHECKS.md`; the hosted candidate does not waive them.
