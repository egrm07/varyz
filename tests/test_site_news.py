"""docs/NEWS.md becomes the feed every Varyz install reads: the two must agree."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from varyz import news

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "site" / "build_site.py"
if not SCRIPT.is_file():  # scripts/site/ stays on the owner's machine (.gitignore)
    pytest.skip("the site builder is not in this checkout", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("build_site", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
site = importlib.util.module_from_spec(SPEC)
sys.modules["build_site"] = site  # dataclasses look the module up
SPEC.loader.exec_module(site)

SOURCE = """# News

The intro.

## 2026-09-28 First post

The **first** summary, with [a link](https://example.com) and `code`.

- a list in the body

## 2026-10-02 Scheduled post

Not out yet.

## 2026-09-29 Second post

The second summary.
"""


def test_unpublished_downloads_have_no_dead_github_buttons():
    body = site.download_body({})
    assert body.count('class="download-status"') == 3
    assert 'class="download-btn"' not in body
    assert "Downloads are not available yet" in body


def test_native_release_links_only_enable_the_available_windows_platform():
    body = site.download_body(
        {
            "available": True,
            "version": "0.1.0",
            "tag": "v0.1.0",
            "sha256": "a" * 64,
        }
    )
    assert body.count('class="download-btn"') == 1
    assert "Download for Windows" in body
    assert "VaryzSetup-0.1.0-windows-x64.exe" in body
    assert body.count('class="download-status"') == 2
    assert "Download for macOS" not in body and "Download for Linux" not in body
    assert "native-release-checksums.json" in body


def test_download_metadata_cannot_inject_an_external_destination():
    with pytest.raises(ValueError, match="Invalid download"):
        site.download_body(
            {
                "available": True,
                "version": "0.1.0",
                "tag": 'v0.1.0" href="https://evil',
                "sha256": "a" * 64,
            }
        )


def _public_release(monkeypatch, tmp_path):
    import hashlib
    import io
    import json

    prefix = site.REPOSITORY + "/releases/download/v0.1.0/"
    content = {name: ("synthetic " + name).encode() for name in (
        "VaryzSetup-0.1.0-windows-x64.exe",
        "VaryzSetup-0.1.0-macos-arm64.pkg",
        "VaryzSetup-0.1.0-macos-x86_64.pkg",
        "varyzai-0.1.0-py3-none-any.whl",
    )}
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    release = {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "assets": [
        {"name": name, "browser_download_url": prefix + name, "size": len(data)}
        for name, data in content.items()
    ] + [{"name": "native-release-checksums.json",
          "browser_download_url": prefix + "native-release-checksums.json"}]}
    requests = []

    def response(request, timeout):
        assert "Authorization" not in request.headers
        requests.append(request.full_url)
        if request.full_url.endswith("/releases/latest"):
            data = json.dumps(release).encode()
        elif request.full_url.endswith("/native-release-checksums.json"):
            data = json.dumps(checksums).encode()
        else:
            data = content[request.full_url.removeprefix(prefix)]
        return io.BytesIO(data)

    monkeypatch.setattr(site.urllib.request, "urlopen", response)
    target = tmp_path / "download-release.json"
    target.write_text('{"available": false}', encoding="utf-8")
    monkeypatch.setattr(site, "DOWNLOAD_RELEASE", target)
    return content, release, target, requests


def test_all_download_platforms_require_anonymous_byte_verification(monkeypatch, tmp_path):
    import json

    content, _, target, requests = _public_release(monkeypatch, tmp_path)
    site.refresh_download_release()
    snapshot = json.loads(target.read_text())
    assert set(snapshot["assets"]) == set(content)
    assert len(requests) == len(content) + 2
    body = site.download_body(snapshot)
    assert "macos-arm64.pkg" in body and "macos-x86_64.pkg" in body
    assert 'href="#linux-installation"' in body
    assert "Windows installer is unsigned" in body
    assert "Windows may show a security warning or block it" in body
    assert "--package " + site.REPOSITORY + "/releases/download/v0.1.0/" in body


def test_corrupt_mac_download_does_not_replace_a_safe_snapshot(monkeypatch, tmp_path):
    content, _, target, _ = _public_release(monkeypatch, tmp_path)
    before = target.read_bytes()
    content["VaryzSetup-0.1.0-macos-arm64.pkg"] = b"corrupt package"
    with pytest.raises(ValueError, match="does not match"):
        site.refresh_download_release()
    assert target.read_bytes() == before


def test_release_asset_cannot_redirect_downloads_elsewhere(monkeypatch, tmp_path):
    _, release, target, _ = _public_release(monkeypatch, tmp_path)
    release["assets"][1]["browser_download_url"] = "https://unrelated.example/package.pkg"
    with pytest.raises(ValueError, match="Unexpected public release destination"):
        site.refresh_download_release()
    assert target.read_text() == '{"available": false}'


def test_every_post_in_docs_news_reaches_an_install_whole():
    _, posts = site.parse_news((ROOT / "docs" / "NEWS.md").read_text(encoding="utf-8"), date.max)
    assert posts, "docs/NEWS.md has no posts"
    feed = site.news_feed(posts)
    assert [asdict(item) for item in news.parse_feed(feed)] == feed["items"]


def test_posts_are_newest_first_summarised_and_held_back_until_their_day():
    intro, posts = site.parse_news(SOURCE, date(2026, 9, 30))
    assert "The intro." in intro
    assert [post.id for post in posts] == ["2026-09-29-second-post", "2026-09-28-first-post"]
    assert posts[1].summary == "The first summary, with a link and code."
    assert "<li>a list in the body</li>" in posts[1].body
    assert posts[1].url == "https://varyzai.com/news/#2026-09-28-first-post"
    _, later = site.parse_news(SOURCE, date(2026, 10, 2))
    assert later[0].id == "2026-10-02-scheduled-post"


@pytest.mark.parametrize(
    "source",
    [
        "# News\n\n## Yesterday Post\n\nText.\n",
        "# News\n\n## 2026-09-31 Post\n\nText.\n",
        "# News\n\n## 2026-09-29 Post\n\n- only a list\n",
        "# News\n\n## 2026-09-29 Post\n\n" + "word " * 100 + "\n",
        "# News\n\n## 2026-09-29 Post\n\nOne.\n\n## 2026-09-29 Post\n\nTwo.\n",
    ],
)
def test_a_post_the_feed_would_cut_or_misread_fails_the_build(source: str):
    with pytest.raises(SystemExit):
        site.parse_news(source, date(2026, 9, 30))


def test_the_atom_feed_is_valid_xml_with_every_post():
    import xml.etree.ElementTree as ElementTree

    _, posts = site.parse_news(SOURCE, date(2026, 9, 30))
    root = ElementTree.fromstring(site.news_atom(posts))
    atom = "{http://www.w3.org/2005/Atom}"
    assert root.findtext(f"{atom}updated") == "2026-09-29T00:00:00Z"
    entries = root.findall(f"{atom}entry")
    assert [entry.findtext(f"{atom}id") for entry in entries] == [post.url for post in posts]
    assert entries[1].findtext(f"{atom}summary") == "The first summary, with a link and code."
    assert "<li>a list in the body</li>" in (entries[1].findtext(f"{atom}content") or "")
