# Cloud project selection

**Sync projects you recognize; keep imported repositories and incidental folders under review.**

Updated 2026-10-09. Local imports and Cloud approval are separate.

## Review projects

```powershell
varyz cloud projects
varyz cloud exclude-projects --project site --project session-workspace --project kronos --project codex
```

The audit reports source types, repository locations, authored repository records,
conversation counts and whether the project needs review. It reads metadata and
conversation headers locally; it does not upload message text. Older imported
conversations may lack their original working directory. Their labels remain
under review rather than gaining an invented location.

An authored conversation establishes activity, not repository ownership or a wish
to sync. Projects supported only by conversations, generic folder names, or
unattributed imported records need review. Bulk selection skips them. Existing
approvals stay visible until deliberately removed. Authored repository activity,
or an identified repository with confirmed preferences, supplies clearer evidence;
the owner still chooses whether to sync it.

New AI imports use the enclosing checkout for sessions in subfolders such as
`deploy/site`. Agent state folders and temporary session workspaces do not become
project names. Project scopes otherwise retain the existing normalization rules.

## Exclude unwanted projects

```powershell
varyz cloud exclude-projects --project kronos --apply
```

The default is a preview. `--apply` saves a persistent veto in the config's
`.cloud-project-policy.json` sidecar, removes matching approvals from all three
sync journals, and queues Cloud removals for subsequent sync. Journal backups use
the suffix `.before-project-cleanup`. The exclusion covers legacy all-project
approvals and individual history approvals as well as the unified picker.

**Local evidence and local confirmed preferences remain intact.** Exclusion is
not a request to withdraw the owner's local rule. Cloud removal is not complete
until the sync has acknowledged it. Sync conflicts stop for review. An old
installed runtime must be updated to enforce the policy, especially when it has
legacy all-project approvals.

The policy is a local per-config control. Other computers with separate approvals
require their own selection review. To reconsider an exclusion, remove that exact
name from the policy and approve a fresh project preview; removing the veto alone
does not restore the removed explicit approvals.
