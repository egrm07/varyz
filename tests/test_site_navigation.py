"""Navigation survives site rebuilds and remains usable without JavaScript."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/site/build_site.py"
if not SCRIPT.is_file():
    pytest.skip("site builder is not in this checkout", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("navigation_site", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
site = importlib.util.module_from_spec(SPEC)
sys.modules["navigation_site"] = site
SPEC.loader.exec_module(site)


def test_account_navigation_build_is_idempotent_and_keeps_ownership_proofs(tmp_path, monkeypatch):
    monkeypatch.setattr(site, "SITE", tmp_path)
    page = '<html><head></head><body><nav class="top-nav" aria-label="Main">' \
        '<a href="cloud">Cloud</a></nav></body></html>'
    (tmp_path / "index.html").write_text(page)
    nested = tmp_path / "news"
    nested.mkdir()
    (nested / "story.html").write_text(page)
    proof = tmp_path / "google123.html"
    proof.write_text("google-site-verification: google123.html")
    site.build_account_navigation()
    first = (tmp_path / "index.html").read_text()
    site.build_account_navigation()
    assert (tmp_path / "index.html").read_text() == first
    assert first.count("data-account-signin") == 1
    assert 'href="https://cloud.varyzai.com/login">Sign in</a>' in first
    assert 'data-account-menu hidden' in first and 'data-account-admin' in first
    assert '<script defer src="/account.js"></script>' in first
    assert 'data-account-signin' in (nested / "story.html").read_text()
    assert proof.read_text() == "google-site-verification: google123.html"
    assert (tmp_path / "account.js").is_file() and (tmp_path / "account.css").is_file()


def test_website_csp_permits_only_explicit_cloud_session_origin():
    headers = (Path(__file__).parents[1] / "website/_headers").read_text()
    connect = headers.split("connect-src ", 1)[1].split(";", 1)[0]
    assert "https://cloud.varyzai.com" in connect
    assert "*" not in connect
