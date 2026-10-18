"""Explicit project requirements must not depend on incidental slot choice."""

import pytest

from tests.test_profile_reader import _entry, _item, _merged
from varyz.db.connection import open_db
from varyz.resolution import profile_reader as reader


def read_one(tmp_path, text, *, applies="project", tagged=True, project="shop"):
    conn = open_db(tmp_path / "archive.db")
    _entry(conn, "owner", text, project=project)
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    item = _item(
        "rule",
        "Keep the repair queue usable while the shop is offline.",
        (1, text),
        applies=applies,
    )
    if tagged:
        item["durability"] = "project_requirement"
    reader.accept(state, conn, task.id, [item], tier="cloud")
    return conn, state


def test_single_explicit_project_behavior_survives_at_project_scope(tmp_path):
    conn, state = read_one(
        tmp_path, "The repair queue must remain usable while the shop is offline."
    )
    try:
        task = reader.next_task(state, conn, tier="cloud", reader="agent")
        assert "Explicit lasting project requirement" in task.text
        result = reader.accept(
            state,
            conn,
            task.id,
            [
                _merged("rule", "Keep the repair queue usable while the shop is offline.", 1),
            ],
            tier="cloud",
        )
        [item] = result.items
        assert item.scope == "shop" and item.messages == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "text",
    [
        "For today the repair queue should use the temporary sample data.",
        "Make this button wider so it is easier to tap.",
        'The client said "The repair queue must work offline".',
        "Maybe the repair queue should work offline; I haven't decided.",
        "I used to want the repair queue to work offline.",
    ],
)
def test_durability_tag_does_not_override_temporary_artifact_or_reported_source(tmp_path, text):
    conn, state = read_one(tmp_path, text)
    try:
        assert not any(
            proof.get("project_requirement") for note in state.notes for proof in note["evidence"]
        )
    finally:
        conn.close()


@pytest.mark.parametrize("applies", ["work", "products"])
def test_project_tag_never_grants_general_durability(tmp_path, applies):
    conn, state = read_one(
        tmp_path, "The repair queue must remain usable offline.", applies=applies
    )
    try:
        assert not any(
            proof.get("project_requirement") for note in state.notes for proof in note["evidence"]
        )
    finally:
        conn.close()


def test_unmarked_project_observations_remain_provisional(tmp_path):
    conn, state = read_one(tmp_path, "The repair queue must remain usable offline.", tagged=False)
    try:
        assert not any(
            proof.get("project_requirement") for note in state.notes for proof in note["evidence"]
        )
    finally:
        conn.close()
