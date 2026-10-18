"""GitHub connector: enumeration, thread streams, watermarks, and failure modes.

Everything runs against an httpx MockTransport — no network, no token, no
`gh`. The point of these is the half `test_gmail_telegram.py` never covered for
Gmail: `discover`, not just `normalize`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from varyz.connectors.base import SkipItem
from varyz.connectors.github import GitHubConnector, token_from_gh, token_secret_name
from varyz.domain import Cursor, Kind

REPO = {
    "full_name": "egor/varyz",
    "name": "varyz",
    "owner": {"login": "egor"},
    "description": "Local-first memory for agents",
    "language": "Python",
    "topics": ["memory", "rag"],
    "visibility": "public",
    "private": False,
    "fork": False,
    "archived": False,
    "stargazers_count": 12,
    "license": {"spdx_id": "MIT"},
    "html_url": "https://github.com/egor/varyz",
    "created_at": "2026-05-01T09:00:00Z",
    "updated_at": "2026-07-30T10:00:00Z",
    "pushed_at": "2026-07-31T18:00:00Z",
}

FORK = {**REPO, "full_name": "egor/somebody-elses", "name": "somebody-elses", "fork": True}

ISSUE = {
    "number": 7,
    "title": "Abstention threshold encodes corpus size",
    "state": "closed",
    "body": "On a fresh archive it refuses 60% of answerable questions.",
    "user": {"login": "egor"},
    "labels": [{"name": "bug"}],
    "created_at": "2026-07-20T08:00:00Z",
    "updated_at": "2026-07-21T09:00:00Z",
    "closed_at": "2026-07-21T09:00:00Z",
    "html_url": "https://github.com/egor/varyz/issues/7",
}

PULL = {
    "number": 8,
    "title": "Weighted RRF",
    "state": "open",
    "body": "Fusion is still unweighted.",
    "user": {"login": "egor"},
    "labels": [],
    "pull_request": {"url": "https://api.github.com/repos/egor/varyz/pulls/8"},
    "created_at": "2026-07-25T08:00:00Z",
    "updated_at": "2026-07-26T08:00:00Z",
    "html_url": "https://github.com/egor/varyz/pull/8",
}

ISSUE_COMMENT = {
    "id": 5001,
    "issue_url": "https://api.github.com/repos/egor/varyz/issues/7",
    "user": {"login": "reviewer"},
    "body": "Agreed — measure it on a second corpus before tuning.",
    "created_at": "2026-07-21T08:30:00Z",
    "updated_at": "2026-07-21T08:30:00Z",
    "html_url": "https://github.com/egor/varyz/issues/7#issuecomment-5001",
}

REVIEW_COMMENT = {
    "id": 6001,
    "pull_request_url": "https://api.github.com/repos/egor/varyz/pulls/8",
    "user": {"login": "reviewer"},
    "body": "This weight should come from the golden set, not a constant.",
    "path": "src/varyz/retrieval/fuse.py",
    "line": 42,
    "diff_hunk": "@@ -40,3 +40,4 @@\n+    weight = 1.0",
    "created_at": "2026-07-26T07:00:00Z",
    "updated_at": "2026-07-26T07:00:00Z",
    "html_url": "https://github.com/egor/varyz/pull/8#discussion_r6001",
}


def _fake_github(
    *,
    repos=(REPO,),
    issues=(),
    issue_comments=(),
    review_comments=(),
    readme: str | None = "# varyz\n\nRetrieval over your own archive.",
    rate_limit_on: str | None = None,
    paginate_repos: bool = False,
):
    """A GitHub that answers from fixtures and records what was asked."""
    calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        path = request.url.path
        if rate_limit_on and rate_limit_on in path:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1785000000"},
                json={"message": "API rate limit exceeded"},
            )
        if path.endswith("/readme"):
            if readme is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, content=readme.encode("utf-8"))
        if path == "/user/repos":
            if paginate_repos and "page=2" not in str(request.url):
                return httpx.Response(
                    200,
                    json=[repos[0]],
                    headers={
                        "link": '<https://api.github.com/user/repos?page=2>; rel="next"'
                    },
                )
            body = list(repos[1:]) if paginate_repos else list(repos)
            return httpx.Response(200, json=body)
        if path.endswith("/issues/comments"):
            return httpx.Response(200, json=list(issue_comments))
        if path.endswith("/pulls/comments"):
            return httpx.Response(200, json=list(review_comments))
        if path.endswith("/issues"):
            return httpx.Response(200, json=list(issues))
        if path.startswith("/repos/"):
            return httpx.Response(200, json=dict(repos[0]))
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    return httpx.MockTransport(handler), calls


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(
        "varyz.connectors.github.secrets.get_secret",
        lambda name: "ghp_test" if name == token_secret_name("egor") else None,
    )


def _options(transport, **overrides):
    return {"account": "egor", "_transport": transport, **overrides}


def _run(connector, options, cursor=None):
    return list(connector.discover(options, cursor))


def test_repos_become_project_entries_with_readme(token):
    transport, _ = _fake_github()
    connector = GitHubConnector()
    items = _run(connector, _options(transport))

    assert len(items) == 1
    entry = connector.normalize(items[0][0])
    assert entry.kind == Kind.DOCUMENT
    assert entry.external_id == "github:repo:egor/varyz"
    assert entry.title == "egor/varyz"
    assert "Local-first memory for agents" in entry.body
    assert "Retrieval over your own archive" in entry.body  # the README
    assert "Language: Python" in entry.body
    assert "Topics: memory, rag" in entry.body
    assert entry.thread_id == "gh:egor/varyz"


def test_missing_readme_is_not_an_error(token):
    transport, _ = _fake_github(readme=None)
    entry = GitHubConnector().normalize(_run(GitHubConnector(), _options(transport))[0][0])
    assert entry.title == "egor/varyz"
    assert "README" not in entry.body


def test_forks_excluded_by_default_and_included_on_request(token):
    transport, _ = _fake_github(repos=(REPO, FORK))
    assert len(_run(GitHubConnector(), _options(transport))) == 1

    transport, _ = _fake_github(repos=(REPO, FORK))
    items = _run(GitHubConnector(), _options(transport, include_forks=True))
    assert len(items) == 2


def test_repo_pagination_follows_link_header(token):
    transport, _ = _fake_github(repos=(REPO, {**REPO, "full_name": "egor/second"}),
                                paginate_repos=True)
    items = _run(GitHubConnector(), _options(transport))
    assert {item.external_id for item, _ in items} == {
        "github:repo:egor/varyz",
        "github:repo:egor/second",
    }


def test_issues_pull_requests_and_comments_share_a_thread(token):
    transport, _ = _fake_github(
        issues=(ISSUE, PULL),
        issue_comments=(ISSUE_COMMENT,),
        review_comments=(REVIEW_COMMENT,),
    )
    connector = GitHubConnector()
    entries = [connector.normalize(item) for item, _ in _run(connector, _options(transport))]
    by_id = {entry.external_id: entry for entry in entries}

    issue = by_id["github:issue:egor/varyz#7"]
    assert issue.kind == Kind.NOTE
    assert issue.title.startswith("Issue egor/varyz#7:")
    assert "refuses 60%" in issue.body
    assert "Labels: bug" in issue.body

    pull = by_id["github:issue:egor/varyz#8"]
    assert pull.title.startswith("PR egor/varyz#8:")  # a PR is an issue with a pull_request key

    comment = by_id["github:issue_comment:5001"]
    review = by_id["github:review_comment:6001"]
    assert comment.thread_id == issue.thread_id == "gh:egor/varyz#7"
    assert review.thread_id == pull.thread_id == "gh:egor/varyz#8"
    assert "src/varyz/retrieval/fuse.py:42" in review.body
    assert "weight = 1.0" in review.body  # the diff hunk under discussion


def test_empty_comment_is_skipped_not_stored(token):
    transport, _ = _fake_github(issue_comments=({**ISSUE_COMMENT, "body": ""},))
    connector = GitHubConnector()
    items = _run(connector, _options(transport))
    empty = next(item for item, _ in items if item.external_id == "github:issue_comment:5001")
    with pytest.raises(SkipItem):
        connector.normalize(empty)


def test_second_run_asks_only_for_what_changed(token):
    transport, _ = _fake_github(issues=(ISSUE,))
    connector = GitHubConnector()
    _run(connector, _options(transport))
    final = connector.checkpoint
    assert final is not None
    repo_state = final.data["repos"]["egor/varyz"]
    assert repo_state["issues_since"] == "2026-07-21T09:00:00Z"

    transport2, calls = _fake_github(issues=(ISSUE,))
    second = GitHubConnector()
    _run(second, _options(transport2), final)
    issue_calls = [url for url in calls if url.path.endswith("/issues")]
    assert issue_calls, "the issues stream must still be polled"
    assert issue_calls[0].params["since"] == "2026-07-21T09:00:00Z"
    # The repo record did not move, so nothing re-fetched its README.
    assert not [url for url in calls if url.path.endswith("/readme")]
    assert not [item for item, _ in _run(second, _options(transport2), final)
                if item.external_id.startswith("github:repo:")]


def test_repo_re_emitted_when_it_moves(token):
    transport, _ = _fake_github()
    connector = GitHubConnector()
    _run(connector, _options(transport))
    stale = connector.checkpoint

    pushed = {**REPO, "pushed_at": "2026-08-01T12:00:00Z"}
    transport2, _ = _fake_github(repos=(pushed,))
    items = _run(GitHubConnector(), _options(transport2), stale)
    assert [item.external_id for item, _ in items] == ["github:repo:egor/varyz"]


def test_since_option_bounds_the_first_backfill(token):
    transport, calls = _fake_github(issues=(ISSUE,))
    _run(GitHubConnector(), _options(transport, since="2026-01-01"))
    issue_call = next(url for url in calls if url.path.endswith("/issues"))
    assert issue_call.params["since"] == "2026-01-01T00:00:00Z"


def test_a_since_github_cannot_read_is_refused_not_ignored(token):
    """An unreadable date read back as "no bound asked for".

    The run then pulled the entire history the line of config had been
    written to avoid, and reported a clean sync of all of it.
    """
    for bad in ("01/01/2026", "last year"):
        transport, _ = _fake_github(issues=(ISSUE,))
        with pytest.raises(ValueError, match="since"):
            _run(GitHubConnector(), _options(transport, since=bad))


def test_one_repository_may_be_written_as_one_string(token):
    """repos = "egor/varyz" asked GitHub for ten repositories named e, g, o...

    A string is iterable, so the obvious way to name a single repository
    enumerated its letters, warned once each, and indexed nothing.
    """
    transport, calls = _fake_github(issues=(ISSUE,))
    items = _run(GitHubConnector(), _options(transport, repos="egor/varyz"))

    assert [item.external_id for item, _ in items][0] == "github:repo:egor/varyz"
    assert [str(url.path) for url in calls].count("/repos/egor/varyz") == 1


def test_a_readme_bound_that_is_not_a_number_names_itself(token):
    transport, _ = _fake_github()
    with pytest.raises(ValueError, match="max_readme_bytes"):
        _run(GitHubConnector(), _options(transport, max_readme_bytes="lots"))


def test_each_yielded_cursor_is_an_independent_snapshot(token):
    """A shared dict would let a later item's watermark leak into an earlier
    cursor — and a crash would then resume past records never ingested."""
    transport, _ = _fake_github(issues=(ISSUE, PULL))
    items = _run(GitHubConnector(), _options(transport))
    cursors = [cursor for _, cursor in items]
    seen = [json.dumps(cursor.data, sort_keys=True) for cursor in cursors]
    assert len(set(seen)) == len(seen), "cursors must not all share one dict"


def test_rate_limit_stops_cleanly_and_keeps_the_checkpoint(token):
    transport, _ = _fake_github(issues=(ISSUE,), rate_limit_on="/issues")
    connector = GitHubConnector()
    items = _run(connector, _options(transport))

    # The repo entry survived; the thread stream stopped without raising.
    assert [item.external_id for item, _ in items] == ["github:repo:egor/varyz"]
    assert connector.checkpoint is not None
    assert "egor/varyz" in connector.checkpoint.data["repos"]


def test_missing_token_names_the_command_that_fixes_it(monkeypatch):
    monkeypatch.setattr("varyz.connectors.github.secrets.get_secret", lambda name: None)
    with pytest.raises(ValueError, match="varyz connect github"):
        list(GitHubConnector().discover({"account": "egor"}, None))


def test_missing_account_is_a_configuration_error(token):
    with pytest.raises(ValueError, match="connectors.github"):
        list(GitHubConnector().discover({}, None))


def test_connector_does_not_enumerate_its_corpus():
    """Cursor-based: absence from a run means "unchanged", never "deleted"."""
    assert GitHubConnector.enumerates_corpus is False


def test_explicit_repo_list_skips_enumeration(token):
    transport, calls = _fake_github()
    items = _run(GitHubConnector(), _options(transport, repos=["egor/varyz"]))
    assert [item.external_id for item, _ in items] == ["github:repo:egor/varyz"]
    assert not [url for url in calls if url.path == "/user/repos"]


def test_registered_under_the_expected_id():
    from varyz.connectors import all_connector_ids, get_connector

    assert "github" in all_connector_ids()
    assert isinstance(get_connector("github"), GitHubConnector)


def test_cursor_round_trips_through_the_domain_model(token):
    transport, _ = _fake_github(issues=(ISSUE,))
    connector = GitHubConnector()
    _run(connector, _options(transport))
    restored = Cursor(**json.loads(connector.checkpoint.model_dump_json()))
    assert restored.data["repos"]["egor/varyz"]["issues_since"] == "2026-07-21T09:00:00Z"


def test_gh_auth_token_is_a_guided_setup_fallback(monkeypatch):
    captured = {}
    monkeypatch.setenv("GH_TOKEN", "ambient-must-not-win")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-must-not-win-either")
    monkeypatch.setattr("varyz.connectors.github.shutil.which", lambda name: "gh.exe")

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="gho_from-login\n")

    monkeypatch.setattr("varyz.connectors.github.subprocess.run", fake_run)
    assert token_from_gh() == "gho_from-login"
    assert captured["command"] == ["gh.exe", "auth", "token"]
    assert "GH_TOKEN" not in captured["kwargs"]["env"]
    assert "GITHUB_TOKEN" not in captured["kwargs"]["env"]


def test_gh_auth_fallback_is_optional(monkeypatch):
    monkeypatch.setattr("varyz.connectors.github.shutil.which", lambda name: None)
    assert token_from_gh() is None
