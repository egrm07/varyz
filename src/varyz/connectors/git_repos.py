"""Local git history import.

Commits are the highest-signal record of technical decisions a developer
leaves behind: what was tried, what replaced it, and what was abandoned.
This connector reads them straight from local clones — no forge account, no
network, no credentials.

Incremental sync stores the last ingested commit per repository and asks git
only for `<sha>..HEAD` on subsequent runs. A rewritten history (rebase, amend,
force-push) makes the stored sha unreachable; that repository then falls back
to a full re-read, which is idempotent because `external_id` is
`<repo path>@<sha>`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from varyz.connectors.base import register, resolve_paths
from varyz.domain import (
    ConversationMetadata,
    Cursor,
    Kind,
    NormalizedEntry,
    RawItem,
    TimePrecision,
)

# Record and field separators that cannot occur in git metadata.
_RS = "\x1e"
_FS = "\x1f"
_FORMAT = _RS + _FS.join(["%H", "%an", "%ae", "%aI", "%P", "%s", "%b"]) + _FS

DEFAULT_MAX_COMMITS = 2000
DEFAULT_SCAN_DEPTH = 3
_GIT_TIMEOUT = 120

# Words that mark a commit message as carrying a decision or a reversal, not
# just a change. Used only to rank, never to filter.
_DECISION_CUES = (
    "because",
    "instead of",
    "rather than",
    "revert",
    "rewrite",
    "replace",
    "drop ",
    "remove ",
    "migrate",
    "switch to",
    "decided",
    "rationale",
    "why",
    "fixes #",
    "closes #",
    "breaking",
    "deprecat",
)


class GitCommandError(RuntimeError):
    """git exited non-zero or was not found.

    `reason` is git's own stderr, without the command line. The full message is
    what a log needs; the reason is the part a user can act on — the command
    line here is a `--pretty=format:` string full of control characters, and
    putting it in front of somebody trying to work out why their repository was
    skipped only hides the sentence that tells them.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason or message


# Git reads these before it reads `-C`, and any one of them can make every
# call answer about a repository the owner never approved.  Varyz sets none of
# them; it inherits whatever launched it, and a sync started from inside a git
# hook inherits all of them.
_GIT_DISCOVERY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _git_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _GIT_DISCOVERY_ENV}


def _run_git(repo: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=_git_environment(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitCommandError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(f"git timed out in {repo}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitCommandError(f"git {' '.join(args)} failed in {repo}: {detail}", detail)
    return result.stdout.decode("utf-8", "replace")


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _same_path(left: Path, right: Path) -> bool:
    try:
        resolved = (left.resolve(strict=True), right.resolve(strict=True))
    except OSError:
        return False
    return os.path.normcase(str(resolved[0])) == os.path.normcase(str(resolved[1]))


def confirm_root(repo: Path) -> None:
    """Refuse a folder whose `.git` git does not treat as its own repository.

    `git -C <path>` does not mean "read the repository at this path".  If
    `<path>/.git` is not a usable repository -- an empty directory left by a
    failed clone is the common shape, and `is_repo` above is satisfied by it --
    git searches *upward* and answers about whichever repository it finds
    first.  Where the scan root sits inside a checkout, that is the enclosing
    checkout, and its commits come back under the nested folder's name, keyed
    `<nested path>@<sha>`, from outside the paths the owner approved.

    The attribution and the natural key are both wrong, but the source scope is
    the one that matters: approval here is per-path, so a read that quietly
    changes which path it read is a read nobody approved.  Asking git which
    working tree it actually resolved, and insisting it is the one we meant, is
    the whole fix.

    A linked worktree answers with its own path and passes.  A folder git
    resolves elsewhere is reported unreadable, like any other repository that
    cannot be read, and the sync carries on with the rest.
    """

    resolved = _run_git(repo, ["rev-parse", "--show-toplevel"]).strip()
    if not resolved:
        raise GitCommandError(
            f"git resolved no working tree in {repo}", "not a git repository"
        )
    if not _same_path(Path(resolved), repo):
        raise GitCommandError(
            f"git resolves {repo} to the repository at {resolved}",
            f"not a repository of its own: git resolves it to {resolved}, "
            "which is outside the approved path",
        )


def committer_identity(repo: Path) -> str | None:
    """The email git would sign a new commit with in this repository.

    This is the only owner signal a clone carries.  A folder of repositories
    holds the user's own work next to code they cloned, and the author line
    cannot tell them apart on its own — but a commit whose author email is the
    identity git is configured to commit with here was written by whoever owns
    this checkout.  `git config` already resolves repository-over-global for us.

    An unset identity returns exit 1, which is not an error worth failing a
    sync over: it means the question has no answer and every commit in this
    repository stays unattributed to the owner.
    """

    try:
        return _run_git(repo, ["config", "--get", "user.email"]).strip() or None
    except GitCommandError:
        return None


def find_repos(root: Path, max_depth: int = DEFAULT_SCAN_DEPTH) -> list[Path]:
    """Repositories at or below `root`, not descending into one once found.

    The approved root is the exception: a projects folder that is itself a
    repository still holds the owner's other repositories, so it is read and
    then searched like any other folder.
    """
    root = root.expanduser()
    if not root.is_dir():
        return []
    found: list[Path] = [root.resolve()] if is_repo(root) else []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in {"node_modules", "venv"}:
                continue
            if is_repo(child):
                found.append(child.resolve())
                continue  # do not descend into a repository
            walk(child, depth + 1)

    walk(root, 1)
    return found


def _sha_present(repo: Path, sha: str) -> bool:
    try:
        _run_git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
    except GitCommandError:
        return False
    return True


def _parse_log(output: str) -> Iterator[dict[str, Any]]:
    """Yield one dict per commit from `git log --numstat` in _FORMAT."""
    for block in output.split(_RS):
        if not block.strip():
            continue
        # Split at most 6 times: the commit body itself contains both newlines
        # and, after the format's trailing _FS, the numstat block.
        fields = block.split(_FS, 6)
        if len(fields) < 7:
            continue
        sha, author, email, authored, parents, subject, rest = fields
        body, _, numstat = rest.partition(_FS)
        files: list[tuple[str, int, int]] = []
        insertions = deletions = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added_raw, removed_raw, path = parts
            # Binary files report "-".
            added = int(added_raw) if added_raw.isdigit() else 0
            removed = int(removed_raw) if removed_raw.isdigit() else 0
            insertions += added
            deletions += removed
            files.append((path, added, removed))
        yield {
            "sha": sha.strip(),
            "author": author,
            "email": email,
            "authored": authored,
            "parents": parents.split() if parents.strip() else [],
            "subject": subject.strip(),
            "body": body.strip(),
            "files": files,
            "insertions": insertions,
            "deletions": deletions,
        }


def commit_significance(commit: dict[str, Any]) -> float:
    """Deterministic rank so routine commits never crowd out decisions.

    Merges and terse one-liners sit below the timeline threshold; commits that
    explain themselves rank above it.
    """
    if len(commit["parents"]) > 1:
        return 0.15
    subject = commit["subject"]
    body = commit["body"]
    if len(subject) < 12 and not body:
        return 0.2
    score = 0.35
    if body:
        score += 0.2
    text = f"{subject}\n{body}".lower()
    if any(cue in text for cue in _DECISION_CUES):
        score += 0.2
    if len(body) > 300:
        score += 0.1
    return min(score, 0.95)


def render_commit(commit: dict[str, Any], repo_name: str) -> str:
    lines = [commit["subject"]]
    if commit["body"]:
        lines += ["", commit["body"]]
    files = commit["files"]
    if files:
        lines += [
            "",
            f"Changed {len(files)} file(s), "
            f"+{commit['insertions']} -{commit['deletions']} in {repo_name}:",
        ]
        # Bound the file list: a 900-file merge should not dominate the entry.
        for path, added, removed in files[:40]:
            lines.append(f"  {path} (+{added} -{removed})")
        if len(files) > 40:
            lines.append(f"  ... {len(files) - 40} more")
    return "\n".join(lines)


@register
class GitConnector:
    connector_id: ClassVar[str] = "git"

    def __init__(self) -> None:
        # How far through its repositories this discovery is, for the sync's
        # bar: each repository an equal share, split by its new commits.
        self.read_fraction: float | None = None

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        if not options.get("approved", False):
            raise ValueError("git repositories require explicit approval in Varyz Imports")
        max_commits = int(options.get("max_commits", DEFAULT_MAX_COMMITS))
        all_refs = bool(options.get("all_refs", False))
        scan_depth = int(options.get("scan_depth", DEFAULT_SCAN_DEPTH))

        state: dict[str, str] = dict((cursor.data if cursor else {}).get("repos", {}))

        repos: list[Path] = []
        for root in resolve_paths(options, self.connector_id):
            for repo in find_repos(root, scan_depth):
                if repo not in repos:
                    repos.append(repo)
        if not repos:
            # Distinct from "no paths": the folders are there, they just hold
            # no repository. Silence here would read as "your repos have no
            # commits", which is a very different thing to go looking for.
            raise ValueError(
                "no git repositories found under the configured paths "
                f"(searched {scan_depth} levels deep)"
            )

        unreadable: list[str] = []
        for number, repo in enumerate(repos):
            self.read_fraction = number / len(repos)
            key = str(repo)
            try:
                confirm_root(repo)
            except GitCommandError as exc:
                unreadable.append(f"{repo.name}: {exc.reason}")
                continue
            last_sha = state.get(key)
            args = ["log", "--numstat", "--no-color", f"--pretty=format:{_FORMAT}"]
            if all_refs:
                args.append("--all")
            args.append(f"-n{max_commits}")
            if last_sha and _sha_present(repo, last_sha):
                args.append(f"{last_sha}..HEAD")
            try:
                output = _run_git(repo, args)
            except GitCommandError as exc:
                # An unreadable repo must not abort the whole sync — but if
                # every one of them is unreadable the sync reports "0 new, 0
                # unchanged, 0 errors", which reads as "your repositories have
                # nothing new" rather than "varyz could not run git".
                unreadable.append(f"{repo.name}: {exc.reason}")
                continue

            commits = list(_parse_log(output))
            if not commits:
                continue
            viewer = committer_identity(repo)
            # git log is newest-first; ingest oldest-first so the cursor only
            # ever advances to a commit whose ancestors are already stored.
            for done, commit in enumerate(reversed(commits)):
                self.read_fraction = (number + done / len(commits)) / len(repos)
                state[key] = commit["sha"]
                yield (
                    RawItem(
                        external_id=f"{key}@{commit['sha']}",
                        payload=render_commit(commit, repo.name),
                        payload_ext=".txt",
                        metadata={
                            "repo": key,
                            "repo_name": repo.name,
                            "sha": commit["sha"],
                            "author": commit["author"],
                            "email": commit["email"],
                            "authored": commit["authored"],
                            "subject": commit["subject"],
                            "significance": commit_significance(commit),
                            "viewer_email": viewer,
                        },
                    ),
                    Cursor(data={"repos": dict(state)}),
                )

        if len(unreadable) == len(repos):
            # A freshly `git init`-ed repository with no commits lands here
            # too, and that is the point: "0 new" would have the user looking
            # for a bug in varyz.
            detail = "; ".join(unreadable[:3])
            more = f" (and {len(unreadable) - 3} more)" if len(unreadable) > 3 else ""
            raise ValueError(
                f"none of the {len(repos)} repositories found could be read by git — {detail}{more}"
            )

    def normalize(self, item: RawItem) -> NormalizedEntry:
        meta = item.metadata
        try:
            occurred = datetime.fromisoformat(meta["authored"])
        except (KeyError, ValueError):
            occurred = None
        subject = meta.get("subject") or "(no commit subject)"
        # A commit names its author, and that is a speaker in exactly the sense
        # the personal model means: an identity the source established rather
        # than one varyz guessed. Without it every commit reads as words nobody
        # said, and the observation layer declines to read anything from them.
        email = str(meta.get("email") or "") or None
        author = str(meta.get("author") or "") or None
        viewer = str(meta.get("viewer_email") or "") or None
        return NormalizedEntry(
            kind=Kind.DOCUMENT,
            external_id=item.external_id,
            title=f"{meta.get('repo_name', 'repo')}: {subject}"[:200],
            body=item.payload if isinstance(item.payload, str) else item.payload_bytes().decode(),
            occurred_start=occurred,
            time_precision=TimePrecision.EXACT if occurred else TimePrecision.UNKNOWN,
            thread_id=f"git:{meta.get('repo_name', '')}",
            conversation=ConversationMetadata(
                conversation_id=str(meta.get("repo") or "") or None,
                sender_id=email,
                sender_label=author,
            ),
            # Author email against the identity git commits with in this
            # checkout. A clone of somebody else's project carries their
            # commits under their address, and this is what keeps those from
            # being read as the user's own words.
            authored_by_user=bool(
                email and viewer and email.casefold() == viewer.casefold()
            ),
            significance=meta.get("significance"),
        )
