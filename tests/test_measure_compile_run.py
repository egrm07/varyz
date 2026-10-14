"""The supervisor-log parser behind the rebuild figure.

A run's attempt wall times are the only measurement of what recompiling costs
after one new entry, and a parser that quietly reads a log wrong reports a
number nobody can tell is wrong.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measure_compile_run", ROOT / "scripts" / "measure_compile_run.py"
)
assert SPEC and SPEC.loader
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)

LOG = """\
2026-08-30T21:04:10Z  attempt 1: uv run --no-sync varyz model compile --run
2026-08-31T00:39:51Z  attempt 1 exited 1
grouped annotation failed with ValidationError: 1 validation error
2026-08-31T00:39:54Z  retrying in 30s
2026-08-31T00:40:24Z  attempt 2: uv run --no-sync varyz model compile --run
2026-08-31T01:23:30Z  attempt 2 exited 1
2026-08-31T01:24:33Z  attempt 3: uv run --no-sync varyz model compile --run
"""


def test_each_finished_attempt_is_timed_from_its_own_start(tmp_path: Path) -> None:
    log = tmp_path / "supervisor.log"
    log.write_text(LOG, encoding="utf-8")

    timed = [(number, round(minutes, 2), code) for number, minutes, code in measure.attempts(log)]

    assert timed == [(1, 215.68, 1), (2, 43.1, 1)]


def test_an_attempt_still_running_is_not_given_a_duration(tmp_path: Path) -> None:
    """Attempt 3 has a start and no exit, and a duration needs both ends."""

    log = tmp_path / "supervisor.log"
    log.write_text(LOG, encoding="utf-8")

    assert 3 not in {number for number, _minutes, _code in measure.attempts(log)}


def test_a_line_that_only_mentions_an_attempt_is_not_one(tmp_path: Path) -> None:
    """The log carries stderr tails, and those quote the run's own messages."""

    log = tmp_path / "supervisor.log"
    log.write_text(
        "2026-08-31T01:59:38Z  stderr tail: attempt 9 exited 1 was in the output\n"
        "  attempt 4: something indented from a traceback\n",
        encoding="utf-8",
    )

    assert measure.attempts(log) == []
