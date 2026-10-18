"""Candidate selection and immutable, versioned public review packages."""

import json
import runpy
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_handoff_names_match_embedded_versions_and_cannot_overwrite(tmp_path, monkeypatch):
    output = tmp_path / "candidate"
    monkeypatch.setattr(sys, "argv", ["prepare_submission", "--output", str(output)])
    prepare = runpy.run_path(str(ROOT / "scripts/plugin/prepare_submission.py"))["main"]
    prepare()
    checksums = json.loads((output / "checksums.json").read_text())
    for client, manifest in (
        ("chatgpt", "plugin.json"),
        ("claude-cloud", ".claude-plugin/plugin.json"),
    ):
        package, = output.glob(f"varyz-{client}-*-review.zip")
        with zipfile.ZipFile(package) as archive:
            version = json.loads(archive.read(manifest))["version"]
            assert package.name == f"varyz-{client}-{version}-review.zip"
            assert ".app.json" not in archive.namelist()
            assert "LICENSE" in archive.namelist()
        assert package.name in checksums
    before = (output / "checksums.json").read_bytes()
    assert not (output / "legal-pages").exists()
    with pytest.raises(SystemExit, match="immutable"):
        prepare()
    assert (output / "checksums.json").read_bytes() == before


def test_handoff_policy_inputs_are_explicit_and_validated_before_writing(tmp_path, monkeypatch):
    output, legal = tmp_path / "candidate", tmp_path / "policies"
    legal.mkdir()
    monkeypatch.setattr(sys, "argv", [
        "prepare_submission", "--output", str(output), "--legal-dir", str(legal),
    ])
    prepare = runpy.run_path(str(ROOT / "scripts/plugin/prepare_submission.py"))["main"]
    with pytest.raises(SystemExit, match="privacy.html and terms.html"):
        prepare()
    assert not output.exists()
    for name in ("privacy.html", "terms.html"):
        (legal / name).write_text("<p>Synthetic policy fixture</p>")
    prepare()
    for source in legal.iterdir():
        assert (output / "legal-pages" / source.name).read_bytes() == source.read_bytes()


def test_smoke_does_not_fall_back_to_stale_default_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ci_release_smoke", "--package-dir", str(tmp_path)])
    smoke = runpy.run_path(str(ROOT / "scripts/ci_release_smoke.py"))["main"]
    with pytest.raises(SystemExit, match="exactly one"):
        smoke()
