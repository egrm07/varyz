import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "notary_candidate", Path(__file__).resolve().parents[1] / "scripts/macos/notarize_candidate.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def prepare(tmp_path, monkeypatch):
    source = tmp_path / "signed"
    source.mkdir()
    pkg = source / "VaryzSetup-test.pkg"
    pkg.write_bytes(b"fake signed installer")
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    (source / "VaryzSetup-test.signing.json").write_text(
        json.dumps(
            {
                "sha256": sha,
                "signed_runtime_binaries": 12,
                "signed_wheel_libraries": 66,
                "distribution_ready": False,
            }
        )
    )
    (source / "native-release-provenance.json").write_text(
        json.dumps(
            {
                "revision": "expected",
                "working_tree_dirty": False,
                "artifacts": {"signed-candidate/" + pkg.name: sha},
            }
        )
    )
    for key, value in {
        "SIGNED_ARTIFACT": str(source),
        "NOTARIZED_OUTPUT": str(tmp_path / "out"),
        "EXPECTED_SOURCE_COMMIT": "expected",
        "RUNNER_TEMP": str(tmp_path),
        "APPLE_ID": "synthetic@example.invalid",
        "APPLE_APP_SPECIFIC_PASSWORD": "synthetic-password",
        "APPLE_TEAM_ID": "SYNTHETIC",
        "SIGNING_RUN_ID": "123",
        "GITHUB_SHA": "workflow-revision",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    return pkg, tmp_path / "out"


def test_rejects_modified_input_before_credentials(tmp_path, monkeypatch):
    pkg, output = prepare(tmp_path, monkeypatch)
    pkg.write_bytes(b"tampered")
    monkeypatch.setattr(
        module, "run", lambda *a, **k: pytest.fail("must reject before external calls")
    )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        module.main()
    assert not output.exists()


def test_accepted_stapled_bytes_get_new_hash(tmp_path, monkeypatch):
    pkg, output = prepare(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        if "submit" in args:
            return json.dumps({"status": "Accepted", "id": "synthetic-request"})
        if "staple" in args:
            Path(args[-1]).write_bytes(b"fake stapled installer")
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    module.main()
    receipt = json.loads((output / "notarization.json").read_text())
    signing = json.loads((output / "VaryzSetup-test.signing.json").read_text())
    assert receipt["sha256"] == hashlib.sha256((output / pkg.name).read_bytes()).hexdigest()
    assert receipt["sha256"] != receipt["signed_sha256_before_stapling"]
    assert signing["notarized"] and signing["stapled"] and not signing["distribution_ready"]
    assert (
        json.loads((output / "native-release-provenance.json").read_text())["revision"]
        == "expected"
    )


def test_rejected_notarization_never_staples(tmp_path, monkeypatch):
    pkg, output = prepare(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        if "submit" in args:
            return json.dumps({"status": "Invalid", "id": "synthetic-request"})
        assert "staple" not in args and "spctl" not in args
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    with pytest.raises(RuntimeError, match="did not accept"):
        module.main()
    assert (
        json.loads((output / "notarization.json").read_text())["notarization_status"] == "Invalid"
    )
    assert not (output / "VaryzSetup-test.signing.json").exists()


def test_stapling_ticket_delay_is_retried_but_bounded(monkeypatch):
    results = iter([68, 68, 0])
    delays = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=next(results), stdout="ok"),
    )
    assert module.run(["xcrun", "stapler", "staple", "synthetic.pkg"]) == "ok"
    assert delays == [10, 30]
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=68, stdout="", stderr="synthetic ticket delay"),
    )
    delays.clear()
    with pytest.raises(RuntimeError, match="xcrun stapler staple failed with exit code 68"):
        module.run(["xcrun", "stapler", "staple", "synthetic.pkg"])
    assert delays == [10, 30, 60]


def test_gatekeeper_failure_is_never_retried_or_waived(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda *a: pytest.fail("must not retry assessment"))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=68, stdout="")
    )
    with pytest.raises(RuntimeError):
        module.run(["spctl", "--assess", "synthetic.pkg"])
