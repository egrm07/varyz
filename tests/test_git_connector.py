"""Git connector: parsing, ranking, repo discovery, and incremental cursors.

These build real repositories with the real git binary — the parsing contract
is against git's actual output, not a fixture of what we assume it prints.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from varyz.connectors.git_repos import (
    GitConnector,
    commit_significance,
    find_repos,
    render_commit,
)
from varyz.domain import Cursor, Kind


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not on PATH")


def make_repo(path: Path, commits: list[tuple[str, str, str]]) -> Path:
    """commits: (filename, contents, message) — message may contain a body."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    for filename, contents, message in commits:
        (path / filename).write_text(contents, encoding="utf-8")
        _git(path, "add", filename)
        _git(path, "commit", "-q", "-m", message)
    return path


def discover_all(repo_root: Path, cursor: Cursor | None = None) -> list:
    connector = GitConnector()
    return list(
        connector.discover(
            {"approved": True, "paths": [str(repo_root)]},
            cursor,
        )
    )


def test_requires_approval(tmp_path: Path) -> None:
    connector = GitConnector()
    with pytest.raises(ValueError, match="approval"):
        list(connector.discover({"paths": [str(tmp_path)]}, None))


def test_reads_commits_oldest_first(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path / "proj",
        [
            ("a.txt", "one", "first commit"),
            ("b.txt", "two", "second commit"),
        ],
    )
    items = discover_all(repo)
    assert len(items) == 2
    subjects = [item.metadata["subject"] for item, _ in items]
    assert subjects == ["first commit", "second commit"]
    assert all(item.external_id.startswith(str(repo)) for item, _ in items)


def test_parses_body_and_numstat(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path / "proj",
        [("a.txt", "line1\nline2\nline3\n", "Add thing\n\nBecause the old way was slow.")],
    )
    (item, _) = discover_all(repo)[0]
    body = item.payload
    assert "Because the old way was slow." in body
    assert "a.txt (+3 -0)" in body
    assert "Changed 1 file(s), +3 -0" in body


def test_normalize_produces_dated_document_entry(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "proj", [("a.txt", "x", "Initial import")])
    item, _ = discover_all(repo)[0]
    entry = GitConnector().normalize(item)
    assert entry.kind is Kind.DOCUMENT
    assert entry.title == "proj: Initial import"
    assert entry.occurred_start is not None
    assert entry.thread_id == "git:proj"
    assert entry.significance is not None


def test_normalize_carries_the_commit_author_as_a_speaker(tmp_path: Path) -> None:
    """Without this the observation layer reads a commit as words nobody said."""

    repo = make_repo(tmp_path / "proj", [("a.txt", "x", "Initial import")])
    item, _ = discover_all(repo)[0]
    entry = GitConnector().normalize(item)
    assert entry.conversation.sender_id == "test@example.com"
    assert entry.conversation.sender_label == "Test"
    assert entry.conversation.conversation_id == str(repo)
    assert entry.authored_by_user is True


def test_a_commit_by_somebody_else_is_not_the_users_own_words(tmp_path: Path) -> None:
    """A folder of clones holds other people's history under their address."""

    repo = make_repo(tmp_path / "proj", [("a.txt", "x", "Initial import")])
    (repo / "b.txt").write_text("y", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "--author=Other <other@example.com>", "-m", "Their work")
    entries = [GitConnector().normalize(item) for item, _ in discover_all(repo)]
    theirs = entries[-1]
    assert theirs.conversation.sender_id == "other@example.com"
    assert theirs.conversation.sender_label == "Other"
    assert theirs.authored_by_user is False
    assert entries[0].authored_by_user is True


def test_incremental_cursor_returns_only_new_commits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "proj", [("a.txt", "one", "first")])
    first = discover_all(repo)
    assert len(first) == 1
    cursor = first[-1][1]

    (repo / "b.txt").write_text("two", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "second")

    second = discover_all(repo, cursor)
    assert [item.metadata["subject"] for item, _ in second] == ["second"]


def test_unchanged_repo_yields_nothing_on_resync(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "proj", [("a.txt", "one", "first")])
    cursor = discover_all(repo)[-1][1]
    assert discover_all(repo, cursor) == []


def test_rewritten_history_falls_back_to_full_read(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "proj", [("a.txt", "one", "first")])
    cursor = Cursor(data={"repos": {str(repo.resolve()): "0" * 40}})
    items = discover_all(repo, cursor)
    assert [item.metadata["subject"] for item, _ in items] == ["first"]


def test_finds_repos_below_a_root_without_descending(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    make_repo(root / "alpha", [("a.txt", "a", "alpha init")])
    make_repo(root / "nested" / "beta", [("b.txt", "b", "beta init")])
    (root / "plain").mkdir(parents=True)

    found = find_repos(root)
    names = sorted(p.name for p in found)
    assert names == ["alpha", "beta"]


def test_a_root_that_is_a_repo_still_yields_the_repos_inside_it(tmp_path: Path) -> None:
    """A projects folder under version control must not hide every project in it."""
    root = make_repo(tmp_path / "projects", [("notes.txt", "n", "root init")])
    make_repo(root / "alpha", [("a.txt", "a", "alpha init")])
    make_repo(root / "group" / "beta", [("b.txt", "b", "beta init")])

    assert sorted(p.name for p in find_repos(root)) == ["alpha", "beta", "projects"]
    items = discover_all(root)
    assert sorted(item.metadata["subject"] for item, _ in items) == [
        "alpha init",
        "beta init",
        "root init",
    ]


def test_scanning_a_root_ingests_every_repo(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    make_repo(root / "alpha", [("a.txt", "a", "alpha init")])
    make_repo(root / "beta", [("b.txt", "b", "beta init")])
    items = discover_all(root)
    assert sorted(item.metadata["repo_name"] for item, _ in items) == ["alpha", "beta"]


def test_unreadable_repo_does_not_abort_sync(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    make_repo(root / "good", [("a.txt", "a", "good init")])
    broken = root / "broken"
    (broken / ".git").mkdir(parents=True)  # looks like a repo, is not one
    items = discover_all(root)
    assert [item.metadata["repo_name"] for item, _ in items] == ["good"]


def test_a_broken_repo_inside_a_checkout_does_not_import_its_parents_history(
    tmp_path: Path,
) -> None:
    """`git -C` searches upward, and an empty `.git` lets it.

    The fixture that found this ran below the Varyz checkout, so git answered
    about Varyz: its commits came back named after the nested folder, keyed to
    a path the owner never approved.  The same fixture in a temporary
    directory outside any repository passes without the fix, which is why the
    enclosing repository here is built rather than assumed.
    """

    enclosing = make_repo(tmp_path / "enclosing", [("secret.txt", "s", "enclosing history")])
    root = enclosing / "projects"
    make_repo(root / "good", [("a.txt", "a", "good init")])
    (root / "broken" / ".git").mkdir(parents=True)

    items = discover_all(root)

    assert [item.metadata["repo_name"] for item, _ in items] == ["good"]
    assert not any("enclosing history" in item.payload for item, _ in items)


def test_a_linked_worktree_is_read_as_itself(tmp_path: Path) -> None:
    """The check must not mistake a legitimate worktree for a stray folder."""

    root = tmp_path / "projects"
    main = make_repo(root / "main", [("a.txt", "a", "main init")])
    _git(main, "worktree", "add", "-q", "-b", "side", str(root / "side"))

    names = sorted(item.metadata["repo_name"] for item, _ in discover_all(root))

    assert names == ["main", "side"]


def test_an_inherited_git_dir_does_not_redirect_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync started from inside a git hook inherits `GIT_DIR`."""

    root = tmp_path / "projects"
    make_repo(root / "good", [("a.txt", "a", "good init")])
    elsewhere = make_repo(tmp_path / "elsewhere", [("b.txt", "b", "elsewhere history")])
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))

    items = discover_all(root)

    assert [item.metadata["repo_name"] for item, _ in items] == ["good"]
    assert not any("elsewhere history" in item.payload for item, _ in items)


def test_repos_that_are_all_unreadable_say_so_rather_than_syncing_nothing(
    tmp_path: Path,
) -> None:
    """`git init` with no commit yet is the common shape here, and the sync
    would otherwise report "0 new, 0 unchanged, 0 errors" — the same clean sync
    of nothing that the "no repositories found" check already replaced, one
    step further along.
    """
    root = tmp_path / "projects"
    (root / "fresh").mkdir(parents=True)
    _git(root / "fresh", "init", "-q", "-b", "main")
    with pytest.raises(ValueError, match="none of the 1 repositories") as raised:
        discover_all(root)
    # Git's own sentence, not the --pretty=format: string full of control
    # characters that the full command line would put in front of it.
    assert "does not have any commits yet" in str(raised.value)
    assert "\x1f" not in str(raised.value)


def test_one_readable_repo_is_enough_to_stay_quiet(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    make_repo(root / "good", [("a.txt", "a", "good init")])
    (root / "fresh").mkdir(parents=True)
    _git(root / "fresh", "init", "-q", "-b", "main")
    items = discover_all(root)
    assert [item.metadata["repo_name"] for item, _ in items] == ["good"]


def test_merge_commits_rank_below_explained_commits() -> None:
    merge = {"parents": ["a", "b"], "subject": "Merge branch 'x'", "body": "", "files": []}
    terse = {"parents": ["a"], "subject": "wip", "body": "", "files": []}
    explained = {
        "parents": ["a"],
        "subject": "Replace queue backend",
        "body": "Switched to SQLite instead of Redis because deployment needed no daemon.",
        "files": [],
    }
    assert commit_significance(merge) < commit_significance(terse)
    assert commit_significance(terse) < commit_significance(explained)
    assert commit_significance(explained) > 0.55  # above the default timeline threshold


def test_render_bounds_a_huge_file_list() -> None:
    commit = {
        "subject": "Bulk change",
        "body": "",
        "files": [(f"f{i}.py", 1, 1) for i in range(100)],
        "insertions": 100,
        "deletions": 100,
    }
    rendered = render_commit(commit, "repo")
    assert "... 60 more" in rendered
    assert rendered.count("(+1 -1)") == 40


def test_binary_file_stats_do_not_crash(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "img.png").write_bytes(bytes(range(256)) * 4)
    _git(repo, "add", "img.png")
    _git(repo, "commit", "-q", "-m", "Add binary asset")
    (item, _) = discover_all(repo)[0]
    assert "img.png" in item.payload
