"""An activated logging policy survives deploys; SSH failure must not disable it."""

import runpy
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("returncode", [0, 1, 255])
def test_retention_detection_fails_closed(monkeypatch, returncode):
    module = runpy.run_path(str(Path(__file__).parents[1] / "deploy/cloud/deploy.py"))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(subprocess, "run", run)
    if returncode == 255:
        with pytest.raises(subprocess.CalledProcessError):
            module["retention_override"]("example.invalid")
    else:
        result = module["retention_override"]("example.invalid")
        assert bool(result) is (returncode == 0)
        if result:
            assert result == " -f /etc/varyz-cloud/compose.retention.yaml"
    assert calls[0][-1] == "test -s /etc/varyz-cloud/compose.retention.yaml"
    assert "StrictHostKeyChecking=yes" in calls[0]
