"""Consent conditions must not turn into unconditional prohibitions."""

import pytest

from tests.test_profile_reader import _entry, _item, _merged
from varyz.db.connection import open_db
from varyz.resolution import profile_reader as reader


@pytest.mark.parametrize("action", ["send", "upload", "share", "transfer"])
def test_unconditional_transfer_ban_is_not_supported_by_no_silent_transfer(action):
    proof = {"quote": "Don't silently send destinations to analytics. Usage analytics is opt-in."}
    assert not reader._prohibition_conditions_preserved(
        f"Do not {action} any destination data to analytics.", [proof]
    )


@pytest.mark.parametrize("statement", [
    "Require opt-in before sending destination data to analytics.",
    "Do not upload destination data without consent.",
    "Never send destination data unless analytics was approved.",
    "Do not silently send destination data to analytics.",
])
def test_consent_based_paraphrases_do_not_need_the_original_wording(statement):
    assert reader._prohibition_conditions_preserved(statement, [
        {"quote": "Don't silently send destinations to analytics. Usage analytics is opt-in."}
    ])


def test_explicit_unconditional_source_ban_remains_valid():
    assert reader._prohibition_conditions_preserved("Never upload destination data.", [
        {"quote": "Never send destination data to any external service."}
    ])


def test_bad_decision_answer_cannot_bypass_the_read_check(tmp_path):
    conn = open_db(tmp_path / "archive.db")
    try:
        text = "Don't silently send destinations to analytics. Usage analytics here must be opt in."
        _entry(conn, "owner", text)
        state = reader.ProfileState(tmp_path / "profile.json")
        task = reader.next_task(state, conn, tier="cloud", reader="agent")
        result = reader.accept(state, conn, task.id, [_item(
            "decision", "", (1, text),
            question="Do not send destination data to analytics?",
            answer="Usage analytics must be opt-in, and no destination data should be sent.",
        )], tier="cloud")
        assert result.dropped == 1 and not state.notes
    finally:
        conn.close()


def test_merge_cannot_remove_an_approval_exception(tmp_path):
    conn = open_db(tmp_path / "archive.db")
    try:
        text = "Never publish a public build without explicit approval."
        _entry(conn, "owner", text)
        state = reader.ProfileState(tmp_path / "profile.json")
        task = reader.next_task(state, conn, tier="cloud", reader="agent")
        reader.accept(state, conn, task.id, [_item(
            "never", "Never publish a public build without explicit approval.", (1, text),
        )], tier="cloud")
        task = reader.next_task(state, conn, tier="cloud", reader="agent")
        result = reader.accept(state, conn, task.id, [
            _merged("never", "Never publish a public build under any circumstances.", 1),
        ], tier="cloud")
        assert result.redo and "conditional prohibition" in result.retry_reason
        assert not result.items and state.dirty
    finally:
        conn.close()
