"""Controlled development regressions, not held-out evidence of model recall."""

import json

import pytest

from tests.test_profile_reader import _entry, _item, _merged
from varyz.db.connection import open_db
from varyz.resolution import profile_reader as reader
from varyz.resolution.rule_miner import MiningStats


@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "data" / "varyz.db")
    yield db
    db.close()


def _read(conn, tmp_path, text, statement, *, applies="project", slot="rule", **extra):
    _entry(conn, "owner", text)
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    result = reader.accept(
        state,
        conn,
        task.id,
        [
            _item(slot, statement, (1, text), applies=applies, **extra),
        ],
        tier="cloud",
    )
    assert not result.items and result.notes in {0, 1}
    return state


def _finish(conn, state, statement, *, applies="project", slot="rule", **extra):
    found = []
    while task := reader.next_task(state, conn, tier="cloud", reader="agent"):
        assert task.kind == "merge"
        result = reader.accept(
            state,
            conn,
            task.id,
            [
                _merged(slot, statement, 1, applies=applies, **extra),
            ],
            tier="cloud",
        )
        assert not result.redo
        found.extend(result.items)
    return found


@pytest.mark.parametrize(
    "text",
    [
        "I like agents to report the observed outcome before implementation details.",
        "For Cedar, I prefer a warning before rebuilding a cache that contains unsaved edits.",
        "When a release discards unsaved edits, I prefer a warning, unless I approved the reset.",
        "In our release tooling, I normally prefer an explicit preview before signing.",
    ],
)
def test_stated_preferences_keep_conditions_without_literal_always(conn, tmp_path, text):
    statement = "Warn before rebuilding a cache containing unsaved edits unless reset was approved."
    state = _read(conn, tmp_path, text, statement)
    [item] = _finish(conn, state, statement)
    assert item.statement == statement and item.messages == 1
    # The root reader already widens unnamed product behavior beyond one project.
    assert item.scope == reader.GLOBAL_SCOPE and item.evidence[0]["quote"] == text


def test_usual_procedure_is_durable_and_preserves_step_order(conn, tmp_path):
    text = "Our usual procedure for a catalog release is render a proof, print it, then archive it."
    fields = {
        "name": "catalog release",
        "steps": ["Render a proof", "Print the proof", "Archive it"],
    }
    state = _read(conn, tmp_path, text, "", slot="workflow", **fields)
    [item] = _finish(conn, state, "", slot="workflow", **fields)
    assert item.steps == fields["steps"]


@pytest.mark.parametrize(
    "text",
    [
        'The client said "Always send short reports".',
        'An example is "I like agents to send short reports".',
        'This prompt says "Never run tests".',
        "They said never run tests.",
        "Do you mean I prefer short reports?",
        "Maybe I like short reports.",
        "I used to prefer short reports.",
        "For Cedar, I prefer short reports for this task.",
        "For Cedar, I prefer this button wider.",
        "I like short reports today.",
        "I want short reports.",
    ],
)
def test_quoted_reported_uncertain_temporary_and_oneoff_are_not_singleton_rules(
    conn,
    tmp_path,
    text,
):
    state = _read(conn, tmp_path, text, "Write a short report of the result.")
    assert not _finish(conn, state, "Write a short report of the result.")


@pytest.mark.parametrize(
    "explicit,scope", [(True, reader.GLOBAL_SCOPE), (False, reader.GLOBAL_SCOPE)]
)
def test_explicit_all_product_taste_keeps_retrieval_scope_from_one_project(
    conn,
    tmp_path,
    explicit,
    scope,
):
    text = (
        "For all my apps, I prefer lightweight typography with no bold headings."
        if explicit
        else "I prefer lightweight typography with no bold headings."
    )
    statement = "Use lightweight typography without bold headings."
    state = _read(conn, tmp_path, text, statement, applies="products")
    [item] = _finish(conn, state, statement, applies="products")
    assert item.scope == scope


def test_narrative_vision_is_project_description_with_verbatim_evidence(conn, tmp_path):
    text = (
        "The scrolling is wrong. Cedar is a field notebook for volunteer gardeners; "
        "people should feel they are recording observations, not filling in a work form."
    )
    statement = (
        "Cedar is a field notebook for volunteer gardeners with an observation-led experience."
    )
    state = _read(conn, tmp_path, text, statement, slot="vision")
    [item] = _finish(conn, state, statement, slot="vision")
    assert item.scope == "shop" and item.slot == "vision"
    assert item.principle(reader.SOURCES["agent"])["provenance"]["quote"] == text


def test_noisy_short_messages_are_bounded_and_every_source_survives_restart(conn, tmp_path):
    for number in range(53):
        _entry(
            conn,
            str(number),
            f"The preview task number {number} is still running.",
            thread=f"thread-{number}",
        )
    state = reader.ProfileState(tmp_path / "profile.json")
    seen = []
    while task := reader.next_task(state, conn, tier="cloud", reader="agent"):
        assert task.kind == "read"
        ids = [turn[0] for turn in state.tasks[task.id]["turns"]]
        assert len(ids) <= 16
        seen.extend(ids)
        reader.accept(state, conn, task.id, [], tier="cloud")
        state.save()
        state = reader.ProfileState(state.path)
    assert len(seen) == 53 and len(set(seen)) == 53


def test_long_turn_is_fully_read_without_manufacturing_recurrence(conn, tmp_path, monkeypatch):
    monkeypatch.setitem(reader.READ_CHARS, "agent", 1200)
    first = "Check the accessibility labels before reporting the app finished."
    last = "Check the accessibility labels before reporting the app finished."
    text = first + " The preview is still processing this intermediate operation." * 80 + last
    _entry(conn, "long", text, thread="single")
    state = reader.ProfileState(tmp_path / "profile.json")
    statement = "Check accessibility labels before reporting the app finished."
    spans = []
    while (task := reader.next_task(state, conn, tier="cloud", reader="agent")).kind == "read":
        span = state.tasks[task.id]["spans"][0]
        spans.append(span)
        items = [_item("rule", statement, (1, first))] if first in task.text else []
        reader.accept(state, conn, task.id, items, tier="cloud")
        if span[1] < len(text):
            assert "long" not in state.read
        state.save()
        state = reader.ProfileState(state.path)
    assert len(spans) > 2 and spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(
        current[0] < previous[1] for previous, current in zip(spans, spans[1:], strict=False)
    )
    assert state.read == {"long": "long"} and not state.read_offsets
    count = len(state.tasks[task.id]["notes"])
    result = reader.accept(
        state,
        conn,
        task.id,
        [
            _merged("rule", statement, *range(1, count + 1)),
        ],
        tier="cloud",
    )
    assert not result.items  # repeated excerpts of one owner turn are not recurrence


def test_changed_long_source_restarts_at_beginning(conn, tmp_path, monkeypatch):
    monkeypatch.setitem(reader.READ_CHARS, "agent", 1200)
    _entry(conn, "long", "An intermediate preview is still processing. " * 80)
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(state, conn, task.id, [], tier="cloud")
    assert state.read_offsets["long"]["offset"] > 0
    conn.execute("UPDATE entries SET content_hash = 'new' WHERE id = 'long'")
    conn.commit()
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert state.tasks[task.id]["spans"][0][0] == 0


def _two_notes(conn, tmp_path):
    text = "Always run the focused tests. I prefer reports to include the observable result."
    _entry(conn, "owner", text)
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(
        state,
        conn,
        task.id,
        [
            _item(
                "rule",
                "For Shop, run the focused tests before reporting success.",
                (1, "Always run the focused tests."),
            ),
            _item(
                "rule",
                "For Shop, open reports with the observable result.",
                (1, "I prefer reports to include the observable result."),
            ),
        ],
        tier="cloud",
    )
    return state


def test_partial_nonempty_merge_reconciles_transactionally_and_persists_pending(conn, tmp_path):
    state = _two_notes(conn, tmp_path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    partial = [_merged("rule", "For Shop, run the focused tests before reporting success.", 1)]
    result = reader.accept(state, conn, task.id, partial, tier="cloud")
    assert result.redo and "[2]" in result.retry_reason and not result.items
    assert state.dirty == {"shop"} and not state.merge_pass
    state.save()
    state = reader.ProfileState(state.path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert "Account for notes [2]" in task.instructions
    with pytest.raises(ValueError, match="still incomplete"):
        reader.accept(state, conn, task.id, partial, tier="cloud")
    assert task.id in state.tasks and state.dirty == {"shop"}
    result = reader.accept(
        state,
        conn,
        task.id,
        partial
        + [
            _merged("rule", "For Shop, open reports with the observable result.", 2),
        ],
        tier="cloud",
    )
    assert len(result.items) == 2 and not state.dirty


def test_specific_exclusions_finish_without_reissuing_rejected_notes(conn, tmp_path):
    state = _two_notes(conn, tmp_path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    result = reader.accept(
        state,
        conn,
        task.id,
        [
            _merged("rule", "For Shop, run the focused tests before reporting success.", 1),
            {
                "notes": [2],
                "exclude": ("Already in the profile as For Shop, open reports "
                            "with the observable result."),
            },
        ],
        tier="cloud",
    )
    assert len(result.items) == 1 and not result.redo
    assert reader.next_task(state, conn, tier="cloud", reader="agent") is None


@pytest.mark.parametrize("reason", ["", "skip", "not useful for the profile"])
def test_generic_exclusion_is_incomplete(conn, tmp_path, reason):
    state = _two_notes(conn, tmp_path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    result = reader.accept(
        state, conn, task.id, [{"notes": [1, 2], "exclude": reason}], tier="cloud"
    )
    assert result.redo and not result.items


@pytest.mark.parametrize(
    "copied",
    [
        "I prefer reports to include the observable result.",
        "pls make it less clutered like i said",
        "Keep it like before when the data is missing.",
    ],
)
def test_conversational_copy_gets_bounded_rewrite_then_keeps_adapted_rule(conn, tmp_path, copied):
    state = _read(
        conn, tmp_path, "I like clear reports that lead with the observable result.", copied
    )
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    result = reader.accept(state, conn, task.id, [_merged("rule", copied, 1)], tier="cloud")
    assert result.redo and "Rewrite" in result.retry_reason and not result.items
    with pytest.raises(ValueError, match="still incomplete"):
        reader.accept(state, conn, task.id, [_merged("rule", copied, 1)], tier="cloud")
    result = reader.accept(
        state,
        conn,
        task.id,
        [
            _merged("rule", "Open reports with the observable result.", 1),
        ],
        tier="cloud",
    )
    items = result.items or _finish(conn, state, "Open reports with the observable result.")
    [item] = items
    assert item.statement == "Open reports with the observable result."
    assert item.evidence[0]["quote"] == "I like clear reports that lead with the observable result."


def test_merge_that_keeps_failing_its_correction_settles_with_sound_items(conn, tmp_path):
    """A reader that never rewords one note cannot hold the whole reading behind it."""
    state = _two_notes(conn, tmp_path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    answer = [
        _merged("rule", "For Shop, run the focused tests before reporting success.", 1),
        _merged("rule", "I prefer reports to include the observable result.", 2),
    ]
    assert reader.accept(state, conn, task.id, answer, tier="cloud").redo
    for _ in range(reader.MERGE_FAILED_RETRIES - 1):
        with pytest.raises(ValueError, match="still incomplete"):
            reader.accept(state, conn, task.id, answer, tier="cloud")
    result = reader.accept(state, conn, task.id, answer, tier="cloud")
    assert not result.redo and result.dropped >= 1 and task.id not in state.tasks
    assert all(not reader._OWNER_VOICE.search(item.statement) for item in result.items)
    assert not state.dirty
    assert reader.next_task(state, conn, tier="cloud", reader="agent") is None


@pytest.mark.parametrize(
    "statement",
    [
        "Always run the focused tests before reporting success.",
        "Please always confirm before deploying production.",
        "Use this repository's formatter before committing changes.",
    ],
)
def test_already_canonical_owner_instructions_need_no_cosmetic_rewrite(conn, tmp_path, statement):
    state = _read(
        conn, tmp_path, "Always run the focused tests before reporting success.", statement
    )
    [item] = _finish(conn, state, statement)
    assert item.statement == statement


def test_model_loop_bounds_failed_reconciliation_and_keeps_partial_status(conn, tmp_path):
    state = _two_notes(conn, tmp_path)

    class PartialModel:
        calls = 0

        def complete(self, instructions, text):
            self.calls += 1
            return json.dumps(
                {
                    "items": [
                        _merged(
                            "rule", "For Shop, run the focused tests before reporting success.", 1
                        )
                    ]
                }
            )

    model = PartialModel()
    stats = MiningStats()
    assert (
        reader.read_with_model(state, conn, model, tier="cloud", reader="agent", stats=stats) == []
    )
    assert model.calls == 2 and stats.unread == 1
    assert state.dirty == {"shop"} and state.tasks


def test_generation_seven_state_is_reread_but_newer_state_is_never_overwritten(conn, tmp_path):
    _entry(conn, "owner", "Always check the focused tests before reporting success.")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 7, "read": {"owner": "owner"}}), encoding="utf-8")
    state = reader.ProfileState(path)
    assert reader.waiting(state, conn, "cloud") == 1
    newer = json.dumps({"version": reader.PROFILE_VERSION + 1, "read_offsets": {"private": 15}})
    path.write_text(newer, encoding="utf-8")
    state = reader.ProfileState(path)
    assert reader.next_task(state, conn, tier="cloud", reader="agent") is None
    state.save()
    assert path.read_text(encoding="utf-8") == newer


def test_reported_quotes_do_not_become_recurrence_across_conversations(conn, tmp_path):
    for number in range(2):
        _entry(
            conn,
            str(number),
            'The client said "Always send short reports".',
            thread=f"independent-{number}",
        )
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    statement = "Always send short reports about completed work."
    reader.accept(
        state,
        conn,
        task.id,
        [
            _item(
                "rule",
                statement,
                (1, "Always send short reports"),
                (2, "Always send short reports"),
            ),
        ],
        tier="cloud",
    )
    assert not _finish(conn, state, statement)


def test_real_corrections_in_question_form_can_supply_independent_recurrence(conn, tmp_path):
    _entry(
        conn,
        "one",
        "Why did you skip the accessibility check before reporting success?",
        thread="first",
    )
    _entry(
        conn,
        "two",
        "Why did you skip the accessibility check before reporting success again?",
        thread="second",
    )
    state = reader.ProfileState(tmp_path / "profile.json")
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    statement = "Run accessibility checks before reporting success."
    reader.accept(
        state,
        conn,
        task.id,
        [
            _item(
                "rule",
                statement,
                (1, "skip the accessibility check"),
                (2, "skip the accessibility check"),
            ),
        ],
        tier="cloud",
    )
    [item] = _finish(conn, state, statement)
    assert item.messages == 2


def test_malformed_note_reference_never_partially_applies_merge(conn, tmp_path):
    state = _two_notes(conn, tmp_path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    result = reader.accept(
        state,
        conn,
        task.id,
        [
            _merged("rule", "For Shop, run the focused tests before reporting success.", 1),
            {
                "slot": "rule",
                "statement": "Open reports with the observable result.",
                "notes": [{"number": 2}],
            },
        ],
        tier="cloud",
    )
    assert result.redo and "integer note numbers" in result.retry_reason
    assert not result.items and not state.merge_pass
