"""Manual capture must not turn a product decision into a global preference."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from varyz import cli


@pytest.mark.parametrize(
    ("question", "answer", "options", "expected"),
    [
        ("What should Varyz prioritize as its primary data sources?",
         "Local AI conversations, Git and GitHub.", [], "varyz"),
        ("Which sources?", "Local AI conversations.", [], "varyz"),
        ("Which sources?", "Local AI conversations.", ["--project", "memoir"], "memoir"),
        ("How should tests work?", "Always run offline.", ["--project", "varyz"], "varyz"),
        ("How should tests work?", "Always run offline.", [], "*"),
        ("Which sources?", "Local AI conversations.", ["--general"], "*"),
    ],
)
def test_know_keeps_product_choices_local(
    monkeypatch, tmp_path, question, answer, options, expected
):
    project = tmp_path / "varyz"
    project.mkdir()
    monkeypatch.chdir(project)
    recorded = []
    client = SimpleNamespace(remember=lambda **kw: recorded.append(kw) or "entry")
    monkeypatch.setattr(cli, "_cfg", lambda config: SimpleNamespace(
        resolution=SimpleNamespace(device_id="test")))
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: client)
    monkeypatch.setattr(cli, "_local_owner_caller", lambda cfg, purpose: None)
    result = CliRunner().invoke(cli.app, ["know", question, answer, *options])
    assert result.exit_code == 0, result.output
    assert recorded[0]["scope"] == expected
    assert ("general preference" in result.output) == (expected == "*")


def test_know_rejects_conflicting_reach_before_saving(monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda config: pytest.fail("must not save"))
    result = CliRunner().invoke(cli.app, ["know", "Which sources?", "Git",
                                         "--project", "varyz", "--general"])
    assert result.exit_code == 2
    assert "not both" in result.output
