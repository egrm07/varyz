"""The probe the compile supervisor watches a run through.

Read-only and never fatal by design, so its failures are silent by design
too: a wrong answer here becomes a wrong line in the report Egor reads
instead of an error anybody notices.
"""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from varyz.personal_model.compiler import CompilerStage

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compile_progress.py"
SPEC = importlib.util.spec_from_file_location("compile_progress", SCRIPT)
assert SPEC and SPEC.loader
progress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(progress)


def test_the_stage_list_still_names_every_stage_the_compiler_has() -> None:
    """The probe cannot import varyz, so it copies the list. This is the copy."""
    assert tuple(str(stage) for stage in CompilerStage) == progress.STAGES


def _database(path: Path, reached: dict[str, str], stale: list[str] | None = None) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE personal_model_run_metrics"
            " (generation_id TEXT, stage TEXT, metric_name TEXT, metric_json TEXT)"
        )
        if stale is not None:
            conn.execute(
                "INSERT INTO personal_model_run_metrics"
                " VALUES ('gen-1','compiler','stale_stages',?)",
                (json.dumps(stale),),
            )
        conn.execute("CREATE TABLE personal_model_observation_annotation_cache (k TEXT)")
        conn.execute("CREATE TABLE personal_model_records (generation_id TEXT, record_kind TEXT)")
        conn.execute(
            "CREATE TABLE personal_model_generations (id TEXT, state TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE personal_model_active_generation"
            " (singleton INTEGER PRIMARY KEY, generation_id TEXT, activated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE personal_model_compiler_checkpoints"
            " (generation_id TEXT, stage TEXT, state TEXT)"
        )
        conn.execute(
            "INSERT INTO personal_model_generations VALUES ('gen-1','failed','2026-08-31')"
        )
        conn.executemany(
            "INSERT INTO personal_model_compiler_checkpoints VALUES ('gen-1',?,?)",
            sorted(reached.items()),
        )
    conn.close()


def _run(path: Path) -> dict[str, object]:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--full"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def test_a_stage_the_compile_never_reached_is_reported_as_not_reached(tmp_path: Path) -> None:
    """The failure this exists for: a run that dies early writes no row for
    the stages ahead of it, and reading only the rows that exist says every
    stage is complete."""
    database = tmp_path / "varyz.db"
    _database(database, {"pin_inputs": "complete", "identity": "complete"})

    checkpoints = _run(database)["checkpoints"]

    assert isinstance(checkpoints, dict)
    assert checkpoints["identity"] == "complete"
    assert checkpoints["synthesis"] == "not reached"
    assert checkpoints["activate"] == "not reached"
    assert set(checkpoints) == set(progress.STAGES)


def test_a_stage_that_ran_without_the_semantic_model_is_named(tmp_path: Path) -> None:
    """Complete and hollow at once: no stage state can show this, and the
    compiler records it in a metric nothing read."""
    database = tmp_path / "varyz.db"
    _database(
        database,
        {stage: "complete" for stage in progress.STAGES},
        stale=["observations", "synthesis"],
    )

    assert _run(database)["stale_stages"] == ["observations", "synthesis"]


def test_a_run_with_every_stage_behind_a_model_says_so(tmp_path: Path) -> None:
    database = tmp_path / "varyz.db"
    _database(database, {stage: "complete" for stage in progress.STAGES}, stale=[])

    assert _run(database)["stale_stages"] == []


def test_a_run_that_finished_has_nothing_outstanding(tmp_path: Path) -> None:
    database = tmp_path / "varyz.db"
    _database(database, {stage: "complete" for stage in progress.STAGES})

    checkpoints = _run(database)["checkpoints"]

    assert isinstance(checkpoints, dict)
    assert set(checkpoints.values()) == {"complete"}


def test_a_path_that_is_not_a_database_leaves_nothing_behind(tmp_path: Path) -> None:
    """Read-only has to mean it. The fallback connection creates the file it
    is given, so a mistyped argument used to leave an empty database named
    after the flag that was meant to follow the path."""
    missing = tmp_path / "--full"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "error" in json.loads(done.stdout)
    assert not missing.exists()


def _activate(path: Path, generation: str, *, state: str = "current") -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO personal_model_generations VALUES (?,?,'2026-09-03')",
            (generation, state),
        )
        conn.execute(
            "INSERT INTO personal_model_active_generation VALUES (1,?,'2026-09-03')",
            (generation,),
        )
    conn.close()


def test_the_activated_generation_is_named_rather_than_left_blank(tmp_path: Path) -> None:
    """The probe used to ask for `state='active'`, which the column's CHECK
    constraint does not allow, so the field was empty on every report ever
    written -- including the runs that did activate."""
    database = tmp_path / "varyz.db"
    _database(database, {stage: "complete" for stage in progress.STAGES})
    _activate(database, "gen-live")

    assert _run(database)["active_generation"] == "gen-live"


def test_a_generation_marked_current_counts_even_before_the_singleton_lands(
    tmp_path: Path,
) -> None:
    """Activation writes two rows. An archive read between them still has an
    answer, and it is the generation, not nothing."""
    database = tmp_path / "varyz.db"
    _database(database, {stage: "complete" for stage in progress.STAGES})
    conn = sqlite3.connect(database)
    with conn:
        conn.execute(
            "INSERT INTO personal_model_generations VALUES ('gen-live','current','2026-09-03')"
        )
    conn.close()

    assert _run(database)["active_generation"] == "gen-live"


def test_an_archive_with_nothing_activated_says_so(tmp_path: Path) -> None:
    database = tmp_path / "varyz.db"
    _database(database, {stage: "complete" for stage in progress.STAGES})

    assert _run(database)["active_generation"] is None
