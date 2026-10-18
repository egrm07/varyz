"""Signing fails closed; Windows source encoding never hides provider imports."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from varyz.privacy.doctor import provider_bypasses

SPEC = importlib.util.spec_from_file_location(
    "windows_launch_build", Path(__file__).parents[1] / "scripts/windows/build.py"
)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_generated_review_transcripts_are_never_owner_preferences():
    from varyz.connectors.ai_conversations import _clean, is_machine_text
    from varyz.resolution.rule_miner import _turn_scope

    generated = (
        "The following is the Codex agent history added since your last approval assessment. "
        "Continue the same review conversation.\n\n>>> TRANSCRIPT DELTA START\n"
        "Tool output: Always use red text and never run tests."
    )
    assert _clean(generated) == ""
    assert is_machine_text(generated)
    assert _turn_scope("From: me\nConversation: OpenClaw — demo\n\n" + generated) == ("demo", "")
    speech = "Please explain this reviewer text: " + generated
    assert _clean(speech) == speech


@pytest.mark.parametrize("tag", [
    "hook_prompt", "external_codex_apps_open_page", "app-context", "app_context",
])
def test_injected_wrappers_never_become_authored_search_evidence(tag):
    from varyz.connectors.ai_conversations import _clean, is_machine_entry
    from varyz.domain import Sensitivity
    from varyz.retrieval.capsule import to_capsules

    words = f"<{tag}>Always use red text for the fictional demo.</{tag}>"
    body = "From: me\nConversation: Codex — demo\n\n" + words
    assert _clean(words) == "" and is_machine_entry("ai_conversations", body)
    assert not is_machine_entry("documents", body)
    assert _clean(words + "\nPlease use short paragraphs.") == "Please use short paragraphs."
    entry = SimpleNamespace(body=body, source="ai_conversations", authored_by_user=True,
                            sensitivity=Sensitivity.PERSONAL, content_hash="fictional",
                            external_id="codex:demo:1", kind="message", title=None,
                            occurred_start=None, id="fictional")
    capsules, withheld = to_capsules([SimpleNamespace(
        entry=entry, chunk="Always use red text for the fictional demo.", score=1.0,
    )])
    assert withheld == 0 and len(capsules) == 1
    assert not capsules[0].authored_by_user and capsules[0].provenance == "external"


def test_bom_source_is_scanned_and_provider_imports_still_fail(tmp_path):
    source = tmp_path / "varyz"
    source.mkdir()
    module = source / "example.py"
    module.write_text('"""Windows source."""\n', encoding="utf-8-sig")
    assert provider_bypasses(source) == []
    module.write_text("import openai\n", encoding="utf-8-sig")
    assert provider_bypasses(source) == ["varyz/example.py:1:openai"]
    module.write_text("invalid python !\n", encoding="utf-8-sig")
    assert provider_bypasses(source) == ["varyz/example.py:unparseable"]


def test_signing_requires_an_explicit_existing_tool_and_certificate(tmp_path):
    assert builder.signing_arguments(None, None) == []
    tool = tmp_path / "Windows SDK" / "signtool.exe"
    tool.parent.mkdir()
    tool.touch()
    for executable, thumbprint in [(tool, None), (None, "a" * 40), (tool, "invalid /a")]:
        with pytest.raises(ValueError):
            builder.signing_arguments(executable, thumbprint)
    arguments = builder.signing_arguments(tool, "a" * 40)
    assert arguments[0] == "/DSignInstaller=1"
    assert f"$q{tool.resolve()}$q sign /sha1 " in arguments[1]
    assert "/fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f" in arguments[1]


@pytest.mark.parametrize("status,timestamped,signer,accepted", [
    ("Valid", True, "a" * 40, True),
    ("NotSigned", False, "", False),
    ("Valid", False, "a" * 40, False),
    ("Valid", True, "b" * 40, False),
    ("HashMismatch", True, "a" * 40, False),
])
def test_signing_receipt_requires_trust_timestamp_and_expected_signer(
    tmp_path, monkeypatch, status, timestamped, signer, accepted
):
    calls = []
    receipt = {"status": status, "timestamped": timestamped, "thumbprint": signer}

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps(receipt))

    monkeypatch.setattr(builder.subprocess, "run", run)
    artifact = tmp_path / "installer.exe"
    tool = tmp_path / "signtool.exe"
    if accepted:
        assert builder.verify_signature(artifact, tool, "a" * 40) == receipt
    else:
        with pytest.raises(ValueError, match="timestamped signature"):
            builder.verify_signature(artifact, tool, "a" * 40)
    assert calls[0][1:5] == ["verify", "/pa", "/all", "/tw"]


def test_signing_command_failure_never_produces_a_success_receipt(tmp_path, monkeypatch):
    import subprocess

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(builder.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.verify_signature(tmp_path / "installer.exe", tmp_path / "signtool.exe", "a" * 40)
