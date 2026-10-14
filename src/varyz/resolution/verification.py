"""Completion receipts: distinguish inspected artifacts from agent assertions."""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    rule_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    status: Literal["passed", "failed", "not_applicable"]
    evidence: Text


EVIDENCE_SCHEMA = {
    "type": "array",
    "maxItems": 100,
    "items": Evidence.model_json_schema(),
    "description": "Actual check results per rule ID, with concise supporting evidence. "
    "Agent reports are labeled separately from independently inspected artifacts.",
}


def _safe_file(root: Path, relative: str) -> Path | None:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or ":" in relative or "\\" in relative:
        return None
    path = root / raw
    # Reject symlinks/reparse points even if their target happens to stay inside root.
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or part.is_junction():
            return None
    try:
        if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 1_000_000:
            return None
    except OSError:
        return None
    return path if path.is_file() else None


_SKIP_DIRS = {"node_modules", "dist", "build", "out", "vendor", "site-packages"}
_ICON_SUFFIXES = {".svg", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
# A standard that also asks for judgment (a look, a template, the copy) is only
# partly inspectable: its inspected parts can fail it, never pass it alone.
_JUDGMENT = re.compile(
    r"\b(?:screenshots?|render|look|contrast|spacing|scroll|claims?|copy|template|legal|"
    r"iterate|mobile|desktop|wordmark|logo|animat\w*|smooth|visual|review|readable)\b",
    re.I,
)


def site_root(cwd: Path | None, artifact: str | None = None) -> Path | None:
    """The folder holding the built page: the named artifact folder, the working
    directory, or its only subfolder with an index.html."""
    if cwd is None:
        return None
    if artifact:
        relative = artifact.replace("\\", "/").strip().strip("/")
        if relative.endswith(".html"):
            relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if relative in {"", "."}:
            return cwd if _safe_file(cwd, "index.html") else None
        page = _safe_file(cwd, f"{relative}/index.html")
        return page.parent if page else None
    if _safe_file(cwd, "index.html"):
        return cwd
    try:
        found = [
            child
            for child in sorted(cwd.iterdir())[:200]
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name not in _SKIP_DIRS
            and _safe_file(cwd, f"{child.name}/index.html")
        ]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.title: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {key: value or "" for key, value in attrs}))
        self._in_title = tag == "title"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)

    def rel(self, name: str) -> list[str]:
        return [
            attrs.get("href", "")
            for tag, attrs in self.tags
            if tag == "link" and name in attrs.get("rel", "").casefold().split()
        ]

    def meta(self, key: str) -> str:
        return next(
            (
                attrs.get("content", "")
                for tag, attrs in self.tags
                if tag == "meta" and key in {attrs.get("name"), attrs.get("property")}
            ),
            "",
        )


def _external(href: str) -> bool:
    return bool(re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", href.strip(), re.I))


def _local(root: Path, href: str, suffixes: set[str]) -> Path | None:
    """A same-site reference resolved inside the site folder; never fetched.
    An absolute URL counts by its path, as a deployed og:image must be absolute."""
    try:
        url = urlsplit(href)
    except ValueError:
        return None
    if url.scheme not in {"", "http", "https"} or not url.path:
        return None
    relative = unquote(url.path).lstrip("/")
    if Path(relative).suffix.casefold() not in suffixes:
        return None
    return _safe_file(root, relative)


def _icon(root: Path, hrefs: list[str], svg: bool) -> str:
    """'ok', 'external' (unverifiable without a network call) or 'missing'."""
    external = False
    for href in hrefs:
        try:
            url = urlsplit(href)
        except ValueError:
            continue
        if url.scheme or url.netloc:
            external = external or url.scheme in {"http", "https"} or bool(url.netloc)
            continue  # No network requests and no proof of external icon availability.
        icon = _local(root, href, {".svg"} if svg else _ICON_SUFFIXES)
        if icon is None:
            continue
        if icon.suffix.casefold() == ".svg":
            try:
                if ElementTree.fromstring(icon.read_bytes()).tag.split("}")[-1] != "svg":
                    continue
            except (OSError, ElementTree.ParseError):
                continue
        return "ok"
    return "external" if external else "missing"


def _wants(statement: str) -> dict[str, bool]:
    text = statement.casefold()
    return {
        "favicon": "favicon" in text,
        "apple-touch-icon": "apple-touch" in text,
        "meta description": "meta description" in text,
        "canonical URL": "canonical" in text,
        "one h1": bool(re.search(r"\bh1\b", text)),
        "alt text": bool(re.search(r"\balt\b", text)),
        "robots.txt": "robots.txt" in text,
        "sitemap.xml": "sitemap" in text,
        "title format": "<title>" in text or bool(re.search(r"\btitle\b.*\btagline\b", text)),
        "Open Graph tags with a local image": bool(re.search(r"open graph|\bog[:-]", text)),
        "Twitter card": "twitter" in text,
        "no hotlinked fonts": "font" in text
        and bool(re.search(r"self-hosted|google fonts|hotlink", text)),
        "no CDN or remote scripts and styles": bool(re.search(r"self-contained|\bcdn\b", text)),
        "inline CSS and JS": "inline css" in text,
    }


def _inspect_site(root: Path, statement: str) -> dict[str, Any] | None:
    """Check the parts of a website standard that the files can prove, without a
    browser or network. None when the standard names nothing inspectable."""
    wants = _wants(statement)
    if not any(wants.values()):
        return None
    index = _safe_file(root, "index.html")
    if index is None:
        return None
    digest = hashlib.sha256()
    pages: list[_Page] = []
    names: list[str] = []
    for path in [index, *sorted(root.glob("*.html"))[:20]]:
        if path.name == "index.html" and pages:
            continue
        if _safe_file(root, path.name) is None:
            continue
        try:
            body = path.read_bytes()
            page = _Page()
            page.feed(body.decode("utf-8"))
        except (OSError, UnicodeError):
            continue
        digest.update(body)
        if pages and "noindex" in page.meta("robots").casefold():
            continue  # A helper page (an og-image source, say) is not part of the site.
        pages.append(page)
        names.append(path.name)
    if not pages:
        return None
    main = pages[0]
    styles = ""
    for path in sorted(root.glob("*.css"))[:10]:
        if _safe_file(root, path.name):
            styles += path.read_text(encoding="utf-8", errors="ignore")
    source = index.read_text(encoding="utf-8", errors="ignore")
    results: dict[str, str] = {}
    where: dict[str, list[str]] = {}

    def check(name: str, ok: bool | None) -> None:
        if wants[name]:
            results[name] = "unverifiable" if ok is None else "ok" if ok else "missing"

    if wants["favicon"]:
        states = [_icon(root, page.rel("icon"), "svg" in statement.casefold()) for page in pages]
        check("favicon", None if "missing" not in states and "external" in states
              else all(state == "ok" for state in states))
        where["favicon"] = [
            name for name, state in zip(names, states, strict=True) if state == "missing"
        ]
    touch = main.rel("apple-touch-icon")
    check("apple-touch-icon", bool(touch) and _local(root, touch[0], _ICON_SUFFIXES) is not None)
    check("meta description", bool(main.meta("description").strip()))
    check("canonical URL", bool(main.rel("canonical")))
    check("one h1", sum(tag == "h1" for tag, _ in main.tags) == 1)
    check("alt text", all("alt" in attrs for tag, attrs in main.tags if tag == "img"))
    check("robots.txt", _safe_file(root, "robots.txt") is not None)
    check("sitemap.xml", _safe_file(root, "sitemap.xml") is not None)
    title = " ".join("".join(main.title).split())
    check("title format", bool(re.match(r"\S.*?(?:\s[—–·|-]|:)\s+\S", title)))
    image = main.meta("og:image")
    check(
        "Open Graph tags with a local image",
        bool(main.meta("og:title").strip() and main.meta("og:description").strip())
        and bool(image)
        and _local(root, image, _ICON_SUFFIXES) is not None,
    )
    check("Twitter card", bool(main.meta("twitter:card").strip()))
    check(
        "no hotlinked fonts",
        not re.search(
            r"fonts\.(?:googleapis|gstatic)\.com|use\.typekit\.net", source + styles, re.I
        ),
    )
    assets = [
        attrs.get("src", "") if tag == "script" else attrs.get("href", "")
        for tag, attrs in main.tags
        if (tag == "script" and attrs.get("src"))
        or (tag == "link" and attrs.get("rel", "").casefold() in {"stylesheet", "modulepreload"})
    ]
    check("no CDN or remote scripts and styles", not any(_external(href) for href in assets))
    check("inline CSS and JS", not assets)
    return {
        "method": "artifact_inspection",
        "artifact": index.name if root == index.parent else index.as_posix(),
        "sha256": digest.hexdigest(),
        "checks": results,
        "where": {name: pages for name, pages in where.items() if pages},
        "needs_review": bool(_JUDGMENT.search(statement)),
    }


def verify(
    items: list[dict[str, Any]],
    *,
    cwd: Path | None = None,
    evidence: list[dict[str, Any]] | None = None,
    artifact: str | None = None,
) -> dict[str, Any]:
    if evidence is not None and (not isinstance(evidence, list) or len(evidence) > 100):
        raise ValueError("evidence must be a bounded list of check results")
    supplied = [Evidence.model_validate(value) for value in evidence or []]
    ids = {item["id"] for item in items}
    if any(value.rule_id not in ids for value in supplied):
        raise ValueError("evidence must reference an applicable rule ID")
    if len({value.rule_id for value in supplied}) != len(supplied):
        raise ValueError("supply one result per applicable rule")
    reported = {value.rule_id: value for value in supplied}
    root = site_root(cwd, artifact)
    receipts = []
    for item in {item["id"]: item for item in items}.values():
        inspected = (
            _inspect_site(root, item["item"]) if root and item.get("domain") == "website" else None
        )
        result = reported.get(item["id"])
        agent = None
        if result is not None:
            from varyz.privacy.secrets_scan import redact_tree

            agent, _ = redact_tree(result.model_dump())
        if inspected:
            checks = inspected.pop("checks")
            where = inspected.pop("where")
            missing = [
                name + (f" (on {', '.join(where[name])})" if name in where else "")
                for name, state in checks.items()
                if state == "missing"
            ]
            open_ = [name for name, state in checks.items() if state == "unverifiable"]
            receipt = {"rule_id": item["id"], **inspected, "checks": checks}
            if missing:
                # A file the agent can open settles it; an agent's word does not.
                receipt.update(status="failed", independently_verified=True,
                               evidence="Missing in the built site: " + ", ".join(missing) + ".")
            elif open_ or inspected["needs_review"]:
                rest = (", ".join(open_) + " " if open_ else "") + "the parts a file cannot show"
                if agent:
                    receipt.update(
                        status=agent["status"],
                        method="artifact_inspection+agent_report",
                        independently_verified=False,
                        evidence=agent["evidence"] + f" (Inspected: {', '.join(checks)}.)",
                    )
                else:
                    receipt.update(status="unchecked", independently_verified=False,
                                   evidence=f"Inspected parts pass; still check {rest}.")
            else:
                receipt.update(status="passed", independently_verified=True,
                               evidence="Verified in the built site: " + ", ".join(checks) + ".")
        elif agent:
            receipt = {**agent, "method": "agent_report", "independently_verified": False}
        else:
            receipt = {
                "rule_id": item["id"],
                "status": "unchecked",
                "method": "none",
                "independently_verified": False,
            }
        receipts.append(receipt)
    failed = [r["rule_id"] for r in receipts if r["status"] == "failed"]
    unchecked = [r["rule_id"] for r in receipts if r["status"] == "unchecked"]
    return {
        "status": "incomplete"
        if failed or unchecked
        else "verified"
        if receipts and all(r["independently_verified"] for r in receipts)
        else "reported",
        "ready_to_report": not failed and not unchecked,
        "independently_verified": bool(receipts)
        and all(r["independently_verified"] for r in receipts),
        "failed": failed,
        "unchecked": unchecked,
        "receipts": receipts,
        "how": "Repair failed checks and review standards lacking evidence. Supply "
        "the complete set of results on each call, identified by the rule_id values "
        "shown here; previous evidence is not carried into a new call. An agent's "
        "assertion does not establish independent verification. Distinguish subjective "
        "assessment from measured checks and disclose unresolved limitations.",
    }
