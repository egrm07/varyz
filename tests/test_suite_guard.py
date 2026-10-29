"""Guards on the test suite itself."""

import pytest


def test_the_suite_stops_at_start_when_the_temp_disk_is_nearly_full(monkeypatch):
    """The test Mac's disk filled at 30% of one run and failed tests at random."""
    import shutil
    from types import SimpleNamespace as Namespace

    from tests import conftest

    monkeypatch.setattr(shutil, "disk_usage", lambda path: Namespace(free=1024**3))
    session = Namespace(config=Namespace(option=Namespace(basetemp=None)))
    with pytest.raises(pytest.exit.Exception, match="1.0 GB free"):
        conftest.pytest_sessionstart(session)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Namespace(free=50 * 1024**3))
    conftest.pytest_sessionstart(session)
