"""GitHub: the projects a person has published, and the argument around them.

Two things live here that no other connector can reach.

The first is the *project list*. `git` reads commits out of repositories that
happen to be cloned on this machine, so it only knows what is on this disk
today. Enumerating from the API instead answers "what has this person
published" — including a repo never cloned here, one cloned and since deleted,
and one pushed from another machine entirely.

The second is discussion. A decision gets argued out in a pull request and
lands in the commit message as one line, so the reasoning exists in the review
thread and nowhere a `git log` can see it. Issues, PR descriptions, issue
comments and inline review comments are all pulled for that reason.

Auth is a personal access token in the OS credential store: one paste, no app
registration, no verification review, no consent screen. Every stream is
watermarked on the server's own `updated_at`, which is why
`enumerates_corpus` stays False — absence from a run means "nothing changed",
never "deleted".
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from varyz import secrets
from varyz.connectors.base import SkipItem, register
from varyz.domain import Cursor, Kind, NormalizedEntry, RawItem

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
DEFAULT_MAX_README_BYTES = 200_000
DEFAULT_TIMEOUT = 30.0

_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
# ".../repos/{owner}/{repo}/issues/{number}" — the only place a comment record
# names the thread it belongs to.
_ISSUE_NUMBER_RE = re.compile(r"/(?:issues|pulls)/(\d+)(?:$|[?#])")


def token_secret_name(account: str) -> str:
    return f"github_token:{account.lower()}"


def token_from_gh(timeout: float = 10.0) -> str | None:
    """Reuse an existing ``gh auth login`` session when the CLI is available.

    The token is returned to the guided connect flow, which verifies it and
    copies it into Varyz's own OS-credential entry. Environment tokens are
    removed here: Typer already accepts ``GITHUB_TOKEN`` as ``--token``, while
    this fallback is specifically meant to query gh's encrypted login store.
    """
    executable = shutil.which("gh")
    if executable is None:
        return None
    environment = os.environ.copy()
    environment.pop("GH_TOKEN", None)
    environment.pop("GITHUB_TOKEN", None)
    try:
        completed = subprocess.run(
            [executable, "auth", "token"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = completed.stdout.strip()
    return token if completed.returncode == 0 and token else None


class RateLimited(RuntimeError):
    """The hourly quota is gone. Not an error — a reason to stop for now.

    Discovery returns cleanly when this is raised so the checkpoint survives
    and the next run resumes; counting it as a failure would both alarm the
    user and discard `connector.checkpoint`.
    """

    def __init__(self, reset_at: datetime | None = None):
        self.reset_at = reset_at
        when = f" until {reset_at.isoformat(timespec='seconds')}" if reset_at else ""
        super().__init__(f"GitHub rate limit reached{when}")


class GitHubAPI:
    """Thin authenticated REST client — pagination and rate limits, no models."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_url = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "varyz",
        }
        # trust_env=False for the same reason google_auth sets it: an ambient
        # HTTPS_PROXY turns a local-first tool into one that routes a token
        # through someone else's box without ever saying so.
        self._client = httpx.Client(
            timeout=timeout, transport=transport, trust_env=False, follow_redirects=True
        )

    def __enter__(self) -> GitHubAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        response = self._client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0" or "retry-after" in response.headers:
                raise RateLimited(_reset_at(response.headers))
        if response.status_code == 401:
            # The token is bad or revoked. That is the user's to fix, and
            # ValueError is how a connector says so without a stack trace.
            raise ValueError(
                "GitHub rejected the token (401). Run: varyz connect github"
            )
        return response

    def json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.get(self._url(path), params=params)
        response.raise_for_status()
        return response.json()

    def paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Walk `Link: rel="next"` to exhaustion.

        The next URL already carries the query, so params go on the first
        request only — re-sending them would override the server's own cursor.
        """
        url = self._url(path)
        query: dict[str, Any] | None = {**(params or {}), "per_page": PER_PAGE}
        while url:
            response = self.get(url, params=query)
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                return
            yield from (record for record in page if isinstance(record, dict))
            url = _next_link(response.headers.get("link", ""))
            query = None

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.api_url}{path}"


def verify_token(
    token: str,
    *,
    api_url: str = DEFAULT_API_URL,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return the login the token belongs to, so setup fails at `connect` time."""
    with GitHubAPI(token, api_url=api_url, transport=transport) as api:
        response = api.get(f"{api.api_url}/user")
        response.raise_for_status()
        login = str(response.json().get("login", "")).strip()
    if not login:
        raise ValueError("GitHub did not return a login for this token")
    return login


@register
class GitHubConnector:
    connector_id: ClassVar[str] = "github"
    # Watermarked on updated_at: a run yields only what changed since the last
    # checkpoint, so "in the index but not in this run" means "unchanged". A
    # True here would read that same silence as deletion and prune the corpus.
    enumerates_corpus: ClassVar[bool] = False
    checkpoint: Cursor | None = None

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        account = str(options.get("account", "")).strip()
        if not account:
            raise ValueError("github connector needs 'account' in [connectors.github]")
        token = secrets.get_secret(token_secret_name(account))
        if not token:
            raise ValueError(
                f"No GitHub token stored for {account}. Run: varyz connect github"
            )

        state = dict(cursor.data) if cursor else {}
        repo_state: dict[str, Any] = {
            key: dict(value)
            for key, value in dict(state.get("repos", {})).items()
            if isinstance(value, dict)
        }
        include_threads = bool(options.get("include_threads", True))
        # int("lots") raises about literals and bases, naming neither the
        # setting nor the file it was typed into.
        try:
            max_readme = int(options.get("max_readme_bytes", DEFAULT_MAX_README_BYTES))
        except (TypeError, ValueError):
            raise ValueError(
                f"max_readme_bytes = {options['max_readme_bytes']!r} in "
                "[connectors.github] is not a whole number of bytes."
            ) from None
        first_since = _iso_since(options.get("since"))

        api = GitHubAPI(
            token,
            api_url=str(options.get("api_url", DEFAULT_API_URL)),
            transport=options.get("_transport"),
        )
        try:
            repos = list(self._repositories(api, options))
            for repo in repos:
                full_name = str(repo.get("full_name", "")).strip()
                if not full_name:
                    continue
                rstate = repo_state.setdefault(full_name, {})

                # The project itself. Re-fetching a README on every sync for a
                # repo nobody has touched is a request per repo per run for no
                # new bytes, so the repo record is re-emitted only when GitHub
                # says something moved. `pushed_at` catches a README edit that
                # leaves `updated_at` alone.
                stamp = f"{repo.get('updated_at')}|{repo.get('pushed_at')}"
                if rstate.get("repo_stamp") != stamp:
                    item = self._repo_item(api, repo, max_readme)
                    rstate["repo_stamp"] = stamp
                    yield item, _snapshot(repo_state)

                if not include_threads:
                    continue
                for item, watermark_key, updated in self._threads(
                    api, full_name, rstate, first_since
                ):
                    # Written before the yield: the ingest layer persists the
                    # cursor only after the item is durably stored, so a crash
                    # between the two re-offers this record rather than
                    # skipping it.
                    if updated:
                        rstate[watermark_key] = updated
                    yield item, _snapshot(repo_state)
        except RateLimited as exc:
            # A clean stop, not a failure: everything ingested so far keeps its
            # checkpoint and the next run picks up from the watermark.
            log.warning("%s: %s — stopping this run", self.connector_id, exc)
            self.checkpoint = _snapshot(repo_state)
            return
        finally:
            api.close()
        self.checkpoint = _snapshot(repo_state)

    def normalize(self, item: RawItem) -> NormalizedEntry:
        record = item.metadata or json.loads(item.payload_bytes().decode("utf-8"))
        record_type = record.get("record_type")
        if record_type == "repo":
            return _normalize_repo(item, record)
        if record_type == "issue":
            return _normalize_issue(item, record)
        return _normalize_comment(item, record)

    # -- discovery helpers -------------------------------------------------

    def _repositories(
        self, api: GitHubAPI, options: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """The repos to index, newest activity last so a bounded run is stable."""
        # repos = "egor/varyz" is the obvious way to write one repository and
        # the wrong one: a string iterates character by character, so it asked
        # GitHub for ten repositories named "e", "g", "o" ... and indexed
        # nothing, warning once per letter.
        configured = options.get("repos", [])
        if isinstance(configured, str):
            configured = [configured]
        explicit = [str(name).strip() for name in configured if str(name).strip()]
        if explicit:
            for full_name in explicit:
                response = api.get(f"{api.api_url}/repos/{full_name}")
                if response.status_code == 404:
                    log.warning("github: %s not found or not visible to this token", full_name)
                    continue
                response.raise_for_status()
                yield response.json()
            return

        include_forks = bool(options.get("include_forks", False))
        include_archived = bool(options.get("include_archived", True))
        params = {
            # "owner" is the literal reading of "projects posted by the user".
            # Widen to "owner,collaborator,organization_member" for work repos.
            "affiliation": str(options.get("affiliation", "owner")),
            "visibility": str(options.get("visibility", "all")),
            "sort": "full_name",
            "direction": "asc",
        }
        for repo in api.paginate("/user/repos", params):
            if repo.get("fork") and not include_forks:
                continue
            if repo.get("archived") and not include_archived:
                continue
            yield repo

    def _threads(
        self,
        api: GitHubAPI,
        full_name: str,
        rstate: dict[str, Any],
        first_since: str | None,
    ) -> Iterator[tuple[RawItem, str, str | None]]:
        """Issues, PR bodies, and both flavours of comment for one repo.

        Three repo-wide endpoints rather than a walk per issue: the comment
        collections can be filtered by `since` directly, which turns what would
        be one request per thread into one request per hundred comments.
        """
        streams = (
            # The issues endpoint returns pull requests too — a PR *is* an
            # issue with a `pull_request` key — so PR descriptions arrive here.
            ("issues_since", f"/repos/{full_name}/issues", {"state": "all"}, "issue"),
            ("issue_comments_since", f"/repos/{full_name}/issues/comments", {}, "issue_comment"),
            ("review_comments_since", f"/repos/{full_name}/pulls/comments", {}, "review_comment"),
        )
        for watermark_key, path, extra, record_type in streams:
            since = rstate.get(watermark_key) or first_since
            params: dict[str, Any] = {"sort": "updated", "direction": "asc", **extra}
            if since:
                params["since"] = since
            try:
                records = api.paginate(path, params)
                for record in records:
                    item = _thread_item(full_name, record, record_type)
                    if item is None:
                        continue
                    yield item, watermark_key, _updated_at(record)
            except RateLimited:
                raise
            except httpx.HTTPStatusError as exc:
                # 403 without rate-limit headers is usually an org that requires
                # SAML authorisation for this token; 404 is a repo with issues
                # disabled. Neither is worth failing the other repos over.
                log.warning(
                    "github: %s %s returned %s; skipping that stream",
                    full_name,
                    record_type,
                    exc.response.status_code,
                )
                continue

    def _repo_item(
        self, api: GitHubAPI, repo: dict[str, Any], max_readme: int
    ) -> RawItem:
        full_name = str(repo["full_name"])
        record = {
            "record_type": "repo",
            "full_name": full_name,
            "name": repo.get("name"),
            "owner": (repo.get("owner") or {}).get("login"),
            "description": repo.get("description"),
            "homepage": repo.get("homepage"),
            "language": repo.get("language"),
            "topics": repo.get("topics") or [],
            "visibility": repo.get("visibility")
            or ("private" if repo.get("private") else "public"),
            "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "open_issues": repo.get("open_issues_count"),
            "license": (repo.get("license") or {}).get("spdx_id"),
            "default_branch": repo.get("default_branch"),
            "created_at": repo.get("created_at"),
            "updated_at": repo.get("updated_at"),
            "pushed_at": repo.get("pushed_at"),
            "html_url": repo.get("html_url"),
            "readme": self._readme(api, full_name, max_readme),
        }
        return RawItem(
            external_id=f"github:repo:{full_name}",
            payload=json.dumps(record, ensure_ascii=False),
            payload_ext=".json",
            metadata=record,
        )

    def _readme(self, api: GitHubAPI, full_name: str, max_bytes: int) -> str:
        """The project's own description of itself — usually the best answer
        varyz can give to "what is X"."""
        try:
            response = api.get(
                f"{api.api_url}/repos/{full_name}/readme",
                accept="application/vnd.github.raw",
            )
        except RateLimited:
            raise
        except httpx.HTTPError as exc:
            log.warning("github: README fetch failed for %s: %s", full_name, exc)
            return ""
        if response.status_code == 404:
            return ""
        if response.status_code >= 400:
            log.warning(
                "github: README for %s returned %s", full_name, response.status_code
            )
            return ""
        return response.content[:max_bytes].decode("utf-8", errors="replace")


# -- record → item ---------------------------------------------------------


def _thread_item(
    full_name: str, record: dict[str, Any], record_type: str
) -> RawItem | None:
    number = _thread_number(record, record_type)
    if number is None:
        return None
    common = {
        "record_type": record_type,
        "repo": full_name,
        "number": number,
        "author": (record.get("user") or {}).get("login"),
        "body": record.get("body") or "",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "html_url": record.get("html_url"),
    }
    if record_type == "issue":
        record_id = f"github:issue:{full_name}#{number}"
        common.update(
            {
                "title": record.get("title"),
                "state": record.get("state"),
                "is_pull_request": "pull_request" in record,
                "labels": [
                    label.get("name")
                    for label in record.get("labels") or []
                    if isinstance(label, dict) and label.get("name")
                ],
                "closed_at": record.get("closed_at"),
                "comments": record.get("comments"),
            }
        )
    else:
        record_id = f"github:{record_type}:{record.get('id')}"
        if record_type == "review_comment":
            common.update(
                {
                    "path": record.get("path"),
                    "line": record.get("line") or record.get("original_line"),
                    "diff_hunk": record.get("diff_hunk"),
                }
            )
    return RawItem(
        external_id=record_id,
        payload=json.dumps(common, ensure_ascii=False),
        payload_ext=".json",
        metadata=common,
    )


def _normalize_repo(item: RawItem, record: dict[str, Any]) -> NormalizedEntry:
    full_name = str(record.get("full_name", "unknown"))
    facts = [
        f"Repository: {full_name}",
        f"URL: {record['html_url']}" if record.get("html_url") else "",
        f"Visibility: {record.get('visibility')}"
        + (" (fork)" if record.get("fork") else "")
        + (" (archived)" if record.get("archived") else ""),
        f"Language: {record['language']}" if record.get("language") else "",
        f"Topics: {', '.join(record['topics'])}" if record.get("topics") else "",
        f"Homepage: {record['homepage']}" if record.get("homepage") else "",
        f"License: {record['license']}" if record.get("license") else "",
        f"Stars: {record['stars']}" if record.get("stars") else "",
        f"Created: {record['created_at']}" if record.get("created_at") else "",
        f"Last push: {record['pushed_at']}" if record.get("pushed_at") else "",
    ]
    parts = ["\n".join(line for line in facts if line)]
    if record.get("description"):
        parts.append(str(record["description"]))
    if record.get("readme"):
        parts.append(f"README\n\n{record['readme']}")
    return NormalizedEntry(
        kind=Kind.DOCUMENT,
        external_id=item.external_id,
        title=full_name,
        body="\n\n".join(parts).strip(),
        occurred_start=_parse_time(record.get("created_at")),
        captured_at=_parse_time(record.get("pushed_at") or record.get("updated_at")),
        thread_id=f"gh:{full_name}",
        significance=0.55,
    )


def _normalize_issue(item: RawItem, record: dict[str, Any]) -> NormalizedEntry:
    full_name = record.get("repo", "")
    number = record.get("number")
    label = "PR" if record.get("is_pull_request") else "Issue"
    subject = str(record.get("title") or "").strip()
    header = [
        f"{label} {full_name}#{number}",
        f"Author: {record['author']}" if record.get("author") else "",
        f"State: {record.get('state')}"
        + (f", closed {record['closed_at']}" if record.get("closed_at") else ""),
        f"Labels: {', '.join(record['labels'])}" if record.get("labels") else "",
        f"URL: {record['html_url']}" if record.get("html_url") else "",
    ]
    body = "\n".join(line for line in header if line)
    if subject:
        body += f"\n\n{subject}"
    if record.get("body"):
        body += f"\n\n{record['body']}"
    reference = f"{label} {full_name}#{number}"
    return NormalizedEntry(
        kind=Kind.NOTE,
        external_id=item.external_id,
        title=f"{reference}: {subject}" if subject else reference,
        body=body.strip(),
        occurred_start=_parse_time(record.get("created_at")),
        captured_at=_parse_time(record.get("updated_at")),
        thread_id=f"gh:{full_name}#{number}",
        significance=0.45,
    )


def _normalize_comment(item: RawItem, record: dict[str, Any]) -> NormalizedEntry:
    text = str(record.get("body") or "").strip()
    if not text:
        # A comment with no text is a reaction or an attachment-only post. It
        # cannot be evidence, and an entry holding only its own header would
        # take a rank away from something that can.
        raise SkipItem(f"{item.external_id} has no comment body")
    full_name = record.get("repo", "")
    number = record.get("number")
    is_review = record.get("record_type") == "review_comment"
    header = [
        f"Comment on {full_name}#{number}",
        f"Author: {record['author']}" if record.get("author") else "",
    ]
    if is_review and record.get("path"):
        location = str(record["path"])
        if record.get("line"):
            location += f":{record['line']}"
        header.append(f"Reviewing: {location}")
    if record.get("html_url"):
        header.append(f"URL: {record['html_url']}")
    body = "\n".join(line for line in header if line)
    if is_review and record.get("diff_hunk"):
        body += f"\n\nDiff under discussion:\n{record['diff_hunk']}"
    body += f"\n\n{text}"
    kind_label = "Review comment" if is_review else "Comment"
    return NormalizedEntry(
        kind=Kind.NOTE,
        external_id=item.external_id,
        title=f"{kind_label} on {full_name}#{number}",
        body=body.strip(),
        occurred_start=_parse_time(record.get("created_at")),
        captured_at=_parse_time(record.get("updated_at")),
        thread_id=f"gh:{full_name}#{number}",
        significance=0.25,
    )


# -- small helpers ---------------------------------------------------------


def _snapshot(repo_state: dict[str, Any]) -> Cursor:
    """A cursor that cannot be mutated by the rest of the run.

    `Cursor(data=repo_state)` would hand out a live reference, so every cursor
    yielded before the next repo advanced would silently gain that repo's
    watermark — and a crash would then resume past records never ingested.
    """
    return Cursor(data={"repos": {key: dict(value) for key, value in repo_state.items()}})


def _thread_number(record: dict[str, Any], record_type: str) -> int | None:
    if record_type == "issue":
        number = record.get("number")
        return int(number) if isinstance(number, int) else None
    # Comments name their thread only through a URL back to it.
    for key in ("issue_url", "pull_request_url"):
        match = _ISSUE_NUMBER_RE.search(str(record.get(key) or ""))
        if match:
            return int(match.group(1))
    return None


def _updated_at(record: dict[str, Any]) -> str | None:
    """The watermark for a record.

    GitHub reads `since` as `>=`, so the newest record comes back once more on
    the next run. That is deliberate: ingest is idempotent and re-reading one
    record costs a request, while advancing past it by a second would drop
    anything that shared the same timestamp.
    """
    value = record.get("updated_at") or record.get("created_at")
    return str(value) if value else None


def _next_link(link_header: str) -> str | None:
    match = _NEXT_LINK_RE.search(link_header or "")
    return match.group(1) if match else None


def _reset_at(headers: httpx.Headers) -> datetime | None:
    retry_after = headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return datetime.now(UTC) + timedelta(seconds=int(retry_after))
    reset = headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return datetime.fromtimestamp(int(reset), tz=UTC)
    return None


def _iso_since(value: Any) -> str | None:
    """`since = "2024-01-01"` is what people type; the API wants ISO 8601.

    A date this cannot read used to come back as None, which is also how it
    says "no bound was asked for": the run then pulled the entire history the
    user had just written a line of config to avoid, and said nothing.
    """

    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_time(text if "T" in text else f"{text}T00:00:00Z")
    if parsed is None:
        raise ValueError(
            f"since = {text!r} in [connectors.github] is not a date. "
            "Write it as YYYY-MM-DD."
        )
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
