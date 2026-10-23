"""Task recipes: the next agent given a worked-out task starts where the last one finished."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from varyz.db.connection import open_db
from varyz.mcp_server import TOOLS, VaryzServer
from varyz.resolution import recipes
from varyz.resolution.briefing import finish_check, render_markdown, start_task
from varyz.resolution.models import Caller
from varyz.resolution.recipes import MAX_TOTAL_CHARS, SQLiteRecipeStore, render
from varyz.trust import register_agent

AGENT = Caller(
    agent_id="agent:claude-code:local",
    display_name="Claude Code",
    purpose="advice",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"
RELEASE = [
    "bump the version: pyproject.toml and CHANGELOG together",
    "build: uv build --wheel, never pip wheel",
    "smoke test: install the wheel in a fresh venv and run varyz doctor",
]
CHECK = "varyz --version in the fresh venv prints the new version"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SQLiteRecipeStore:
    return SQLiteRecipeStore(conn)


def _record(store: SQLiteRecipeStore, **overrides) -> str:
    fields = {
        "name": "release a new varyz version",
        "steps": RELEASE,
        "check": CHECK,
        "pitfalls": "pip wheel drops the migrations folder",
        "scope": "varyz",
        "caller": AGENT,
        "device_id": DEVICE,
        "source": "agent:test",
    }
    fields.update(overrides)
    return store.record(**fields)


def test_a_recorded_recipe_comes_back_with_the_same_task(
    conn: sqlite3.Connection, store: SQLiteRecipeStore
) -> None:
    recipe_id = _record(store)

    briefing = start_task(conn, "Release a new version of varyz", project="varyz")

    recipe = briefing["task_recipe"]
    assert recipe["id"] == recipe_id and recipe["scope"] == "varyz"
    assert recipe["steps"][1] == "build: uv build --wheel, never pip wheel"
    assert recipe["check"] == CHECK
    assert recipe["pitfalls"] == "pip wheel drops the migrations folder"
    assert "record_recipe" in recipe["guidance"]
    text = render_markdown(briefing)
    assert f"Recipe from a past task: release a new varyz version (id {recipe_id})" in text
    assert "2. build: uv build --wheel, never pip wheel" in text
    assert f"Check: {CHECK}" in text
    # Another kind of task, or another project, does not get it.
    assert start_task(conn, "Fix the setup page layout", project="varyz")["task_recipe"] is None
    assert start_task(conn, "Release a new version", project="san7")["task_recipe"] is None


def test_a_recipe_stays_light(store: SQLiteRecipeStore) -> None:
    recipe_id = _record(store)
    recipe = store.get(recipe_id)
    assert recipe is not None and len(render(recipe)) < 400

    long_step = "x" * 150
    with pytest.raises(ValueError, match="at most 8 steps"):
        _record(store, steps=[f"step {n}" for n in range(9)])
    with pytest.raises(ValueError, match="at least two steps"):
        _record(store, steps=["just do it"])
    with pytest.raises(ValueError, match="200 characters"):
        _record(store, steps=["a: " + "y" * 200, "b"])
    with pytest.raises(ValueError, match=f"under {MAX_TOTAL_CHARS}"):
        _record(
            store, steps=[f"step {n}: {long_step}" for n in range(8)],
            check="c" * 240, pitfalls="p" * 240,
        )


def test_a_recipe_never_carries_a_credential(store: SQLiteRecipeStore) -> None:
    with pytest.raises(ValueError, match="credential"):
        _record(store, steps=[
            "log in: export GITHUB_TOKEN=" + "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
            "push the tag",
        ])
    assert store.current() == ()


def test_a_corrected_recipe_replaces_the_old_one(
    conn: sqlite3.Connection, store: SQLiteRecipeStore
) -> None:
    old = _record(store)
    assert _record(store) == old  # the same recipe again is one recipe
    new = _record(store, replaces=old, steps=[*RELEASE, "tag: git tag vX.Y.Z after the smoke test"])

    [current] = store.current()
    assert current.id == new and current.supersedes_id == old
    assert store.uses(new)["changed"] == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM task_recipes")


def test_a_recipe_that_keeps_failing_stops_being_served(
    conn: sqlite3.Connection, store: SQLiteRecipeStore
) -> None:
    recipe_id = _record(store)
    store.outcome(recipe_id, "worked", caller=AGENT, device_id=DEVICE)
    served = start_task(conn, "Release a new varyz version", project="varyz")["task_recipe"]
    assert served["worked_before"] == 1

    for _ in range(2):
        store.outcome(recipe_id, "failed", caller=AGENT, device_id=DEVICE, note="uv missing")
    assert start_task(conn, "Release a new varyz version", project="varyz")["task_recipe"] is None


def test_the_projects_own_recipe_beats_a_general_one(store: SQLiteRecipeStore) -> None:
    general = _record(store, scope="all", name="release a new version")
    own = _record(store)

    assert store.applicable("Release a new varyz version", project="varyz").id == own
    assert store.applicable("Release a new version", project="san7").id == general


VIDEO = {
    "name": "make a varyz demo video",
    "steps": [
        "record: a real session in OBS, notifications off",
        "sound: one sound for every popup, music under the narration",
        "title: logo title in Marcellus",
        "cut: no pause over 2 seconds, silent end card",
        "render: 1080p with imageio-ffmpeg",
    ],
    "check": "watch it through: every popup has a sound and the end card is silent",
    "pitfalls": "a notification covered one take; check the frames before export",
}
SOCIAL = {
    "name": "publish varyz videos on social",
    "steps": [
        "upload: X, Bluesky and LinkedIn through Chrome",
        "captions: English captions on X",
        "verify: open each live post and play the video",
    ],
    "check": "each live post plays to the end",
    "pitfalls": "",
}

# Worded differently from the recipe's name, or named outright; None is no recipe.
VIDEO_TASKS = [
    ("Film a launch clip for varyz", "video", "similar"),
    ("Make a short trailer showing away mode", "video", "similar"),
    ("Record a screencast of the recipes feature", "video", "similar"),
    ("Make a 20 second video of the setup steps", "video", "similar"),
    ("Make a new demo video", "video", "named"),
    ("Publish the new varyz demo video on X and Bluesky", "social", "named"),
    ("Ship the next varyz version", "release", "similar"),
    ("Fix the setup page layout", None, None),
    ("Write a report on how video recipes are matched", None, None),
    ("Deploy the Cloud worker", None, None),
    ("Fix the bug where setup step 5 hangs", None, None),
    ("Plan dinner for Saturday", None, None),
]


@pytest.mark.parametrize(("task", "expected", "match"), VIDEO_TASKS)
def test_a_video_recipe_is_found_when_the_task_is_worded_differently(
    store: SQLiteRecipeStore, task: str, expected: str | None, match: str | None
) -> None:
    ids = {
        "video": _record(store, scope="varyz", **VIDEO),
        "social": _record(store, scope="varyz", **SOCIAL),
        "release": _record(store),
    }

    found = store.match(task, project="varyz")

    if expected is None:
        assert found is None, found
        return
    assert found is not None and (found[0].id, found[1]) == (ids[expected], match)


def test_a_similar_recipe_says_it_is_for_similar_work(
    conn: sqlite3.Connection, store: SQLiteRecipeStore
) -> None:
    recipe_id = _record(store, scope="varyz", **VIDEO)

    recipe = start_task(conn, "Film a launch clip for varyz", project="varyz")["task_recipe"]

    assert recipe["id"] == recipe_id and recipe["match"] == "similar"
    assert "similar work, not this exact task" in recipe["guidance"]
    assert "record_recipe" in recipe["guidance"]
    # A synced payload, as the Cloud holds it: steps as {step, how}.
    steps = [step.to_dict() for step in recipes.parse_recipe_steps(VIDEO["steps"])]
    payload = {**VIDEO, "steps": steps, "scope": "varyz"}
    hosted = recipes.hosted(
        [{"id": recipe_id, "kind": "recipe", "payload": payload}],
        "Film a launch clip for varyz", "varyz",
    )
    assert hosted is not None and hosted["id"] == recipe_id and hosted["match"] == "similar"


def test_finish_check_offers_a_recipe_for_new_work_and_asks_how_a_served_one_went(
    conn: sqlite3.Connection, store: SQLiteRecipeStore
) -> None:
    fresh = finish_check(
        conn, "Set up the Cloud backup job and verify a restore works", project="varyz"
    )["task_recipe"]
    assert "record_recipe" in fresh["offer"] and "Skip it for routine work" in fresh["offer"]

    recipe_id = _record(store)
    served = finish_check(conn, "Release a new varyz version", project="varyz")["task_recipe"]
    assert served["recipe_id"] == recipe_id and "replaces=recipe_id" in served["record"]


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    return path


def test_an_agent_records_a_recipe_through_the_tool(tmp_path: Path) -> None:
    assert any(tool["name"] == "record_recipe" for tool in TOOLS)
    config = _config(tmp_path)
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:claude-code:local", display_name="Claude Code", device_id="local"
    ).token
    conn.close()
    server = VaryzServer(config, agent_token=token)
    try:
        recorded = server.call("record_recipe", {
            "name": "release a new varyz version", "steps": RELEASE, "check": CHECK,
            "project": "varyz", "conversation_id": "conv-1",
        })
        briefing = server.call("start_task", {
            "task": "Release a new varyz version", "project": "varyz",
        })
        outcome = server.call("record_recipe", {
            "recipe_id": recorded["recipe_id"], "outcome": "worked", "project": "varyz",
        })
    finally:
        server.close()

    assert recorded["recorded"] is True and recorded["chars"] < MAX_TOTAL_CHARS
    assert briefing["task_recipe"]["id"] == recorded["recipe_id"]
    assert outcome["recorded"] is True and outcome["outcome"] == "worked"
    conn = open_db(tmp_path / "data" / "varyz.db")
    try:
        recipe = SQLiteRecipeStore(conn).get(recorded["recipe_id"])
    finally:
        conn.close()
    assert recipe is not None and recipe.source == "recipe:agent:claude-code:local"
    assert recipe.provenance == {"conversation_id": "conv-1", "project": "varyz"}
