"""Provisional reads recover recurrence without weakening final evidence gates."""

import json
from pathlib import Path

import pytest

from tests.test_profile_reader import _entry, _item, _merged, _owner
from varyz.db.connection import open_db
from varyz.resolution import profile_reader as reader
from varyz.resolution.principles import SQLitePrincipleStore


@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "data" / "varyz.db")
    yield db
    db.close()


@pytest.mark.parametrize("same_conversation", [False, True])
def test_recurrence_across_read_windows_still_needs_independent_conversations(
    conn, tmp_path: Path, monkeypatch, same_conversation: bool,
):
    first = "Run the relevant tests before saying a change is finished."
    second = "You said it was finished without running the relevant tests again."
    _entry(conn, "a", first, day=1, thread="one")
    _entry(conn, "b", second, day=2, thread="one" if same_conversation else "two")
    monkeypatch.setitem(reader.TASK_CHARS, "agent", len(first) + 60)
    state = reader.ProfileState(tmp_path / "profile.json")
    statement = "Run the relevant tests before reporting a change finished."
    for quote in ("Run the relevant tests", "running the relevant tests"):
        read = reader.next_task(state, conn, tier="cloud", reader="agent")
        assert read.kind == "read" and len(state.tasks[read.id]["turns"]) == 1
        accepted = reader.accept(state, conn, read.id, [
            _item("rule", statement, (1, quote)),
        ], tier="cloud")
        assert accepted.notes == 1 and not accepted.items

    # Merging needs a larger window; read windows were deliberately small.
    monkeypatch.setitem(reader.TASK_CHARS, "agent", 5000)
    # Root keeps this generic test/reporting habit beyond the shop.
    # Follow both project and global merge before judging the final gate.
    found = []
    while (merge := reader.next_task(state, conn, tier="cloud", reader="agent")):
        assert merge.kind == "merge"
        count = len(state.tasks[merge.id]["notes"])
        accepted = reader.accept(state, conn, merge.id, [
            _merged("rule", statement, *range(1, count + 1)),
        ], tier="cloud")
        found.extend(accepted.items)
    assert len(found) == (0 if same_conversation else 1)
    if found:
        assert found[0].messages == 2
        assert found[0].scope == reader.GLOBAL_SCOPE


def test_provisional_notes_do_not_turn_temporary_requests_into_rules(conn, tmp_path):
    _entry(conn, "a", "Skip the browser tests for now, the preview server is offline.", day=1)
    _entry(conn, "b", "Skip the browser tests for this run while the server is offline.", day=2)
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    accepted = reader.accept(state, conn, read.id, [
        _item("rule", "Skip browser tests when checking the preview.",
              (1, "Skip the browser tests"), (2, "Skip the browser tests")),
    ], tier="cloud")
    assert accepted.notes == 0 and accepted.dropped == 1


def test_merge_sees_owner_conditions_and_reason_without_agent_claims(conn, tmp_path):
    text = "Always ask before deploying production; staging can deploy automatically."
    _entry(conn, "a", text, said="The owner wants all deployments to run without approval.")
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(state, conn, read.id, [
        _item("rule", "Ask before production deployments; deploy staging automatically.",
              (1, text), why="Production needs approval.", applies="work"),
    ], tier="cloud")
    merge = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert text in merge.text
    assert "Independent conversations: 1" in merge.text
    assert "Reason noted: Production needs approval." in merge.text
    assert "all deployments to run without approval" not in merge.text


def test_merge_budget_counts_evidence_not_just_short_paraphrases(conn, tmp_path, monkeypatch):
    state = reader.ProfileState(tmp_path / "profile.json")
    for number in range(8):
        quote = f"Instruction {number}: " + "verified owner evidence " * 20
        _entry(conn, str(number), quote)
        state.add_note({
            "project": "shop", "slot": "rule", "statement": "Run the relevant tests first.",
            "applies_to": "project", "domain": "", "why": "", "name": "", "steps": [],
            "question": "", "answer": "", "evidence": [{
                "entry_id": str(number), "content_hash": str(number), "quote": quote,
                "thread": str(number), "occurred": "2026-09-01", "project": "shop",
            }],
        })
    state.read = {str(number): str(number) for number in range(8)}
    monkeypatch.setitem(reader.TASK_CHARS, "agent", 1500)
    merge = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert len(state.tasks[merge.id]["notes"]) == 2
    assert len(merge.text) < 1650  # project/profile headings add a small fixed cost


@pytest.mark.parametrize("applies", ["project", "work"])
def test_every_overflow_page_is_merged_after_restart_and_global_forwarding_survives(
    conn, tmp_path, monkeypatch, applies,
):
    state = reader.ProfileState(tmp_path / "profile.json")
    # Project-only notes name the project's actual services; generic
    # service-checking guidance would be widened by root's existing policy.
    service = "shop service" if applies == "project" else "service"
    statements = [f"Always check {service} {number} before reporting success."
                  for number in range(8)]
    for number, statement in enumerate(statements):
        _entry(conn, str(number), statement)
        state.add_note({
            "project": "shop", "slot": "rule", "statement": statement,
            "applies_to": applies, "domain": "", "why": "", "name": "", "steps": [],
            "specifics": ["shop"] if applies == "project" else [],
            "question": "", "answer": "", "evidence": [{
                "entry_id": str(number), "content_hash": str(number), "quote": statement,
                "thread": str(number), "occurred": "2026-09-01", "project": "shop",
            }],
        })
    monkeypatch.setitem(reader.TASK_CHARS, "agent", 600)
    state.read = {str(number): str(number) for number in range(len(statements))}
    found = []
    pages = 0
    while (merge := reader.next_task(state, conn, tier="cloud", reader="agent")):
        assert merge.kind == "merge"
        numbers = state.tasks[merge.id]["notes"]
        by_number = {note["n"]: note for note in state.notes}
        result = reader.accept(state, conn, merge.id, [
            _merged("rule", by_number[n]["statement"], index, applies=applies)
            for index, n in enumerate(numbers, 1)
        ], tier="cloud")
        found.extend(result.items)
        pages += 1
        assert pages < 20
        state.save()
        state = reader.ProfileState(state.path)
    assert pages >= 3
    assert {item.statement for item in found} == set(statements)
    assert len(found) == len(statements)
    assert not state.merge_pass and not state.dirty
    expected_scope = "shop" if applies == "project" else reader.GLOBAL_SCOPE
    assert {item.scope for item in found} == {expected_scope}


def test_cloud_merge_only_exposes_validated_cloud_approved_quotes(conn, tmp_path):
    _entry(conn, "allowed", "Always confirm before deploying shop production.",
           sensitivity="personal")
    _entry(conn, "sensitive", "Always send the confidential bank note.", sensitivity="sensitive")
    _entry(conn, "unclassified", "Always send the unclassified private note.",
           sensitivity="unclassified")
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert "confidential bank" not in read.text and "unclassified private" not in read.text
    result = reader.accept(state, conn, read.id, [
        _item("rule", "Confirm before deploying shop production changes.",
              (1, "Always confirm before deploying shop production"), specifics=["shop"]),
        _item("rule", "Send confidential information to agents.", (1, "confidential bank note")),
    ], tier="cloud")
    assert result.notes == 1 and result.dropped == 1
    merge = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert "Always confirm before deploying shop production" in merge.text
    assert "confidential bank" not in merge.text and "unclassified private" not in merge.text
    # A merge cannot override the project's original scope.
    result = reader.accept(state, conn, merge.id, [
        _merged("rule", "Confirm before deploying shop production changes.", 1, applies="work"),
    ], tier="cloud")
    assert len(result.items) == 1 and result.items[0].scope == "shop"


@pytest.mark.parametrize("change", ["sensitive", "unclassified", "delete", "edit"])
def test_notes_are_revoked_before_merge_if_original_source_changes(conn, tmp_path, change):
    quote = "Always confirm before deploying production."
    statement = "Confirm before deploying production changes."
    _entry(conn, "a", quote)
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(state, conn, read.id, [
        _item("rule", statement, (1, quote)),
    ], tier="cloud")
    state.save()
    if change in {"sensitive", "unclassified"}:
        conn.execute("UPDATE entries SET sensitivity = ? WHERE id = 'a'", (change,))
    elif change == "delete":
        conn.execute("DELETE FROM entries WHERE id = 'a'")
    else:
        conn.execute("UPDATE entries SET content_hash = 'changed', body = ? WHERE id = 'a'",
                     ("From: me\nConversation: Claude Code — shop\n\nWhat is the next step here?",))
    conn.commit()
    state = reader.ProfileState(state.path)
    task = reader.next_task(state, conn, tier="cloud", reader="agent")
    assert task is None or (quote not in task.text and statement not in task.text)
    assert not state.notes and not state.dirty


def test_source_revoked_after_merge_was_issued_cannot_queue_old_candidate(conn, tmp_path):
    quote = "Always confirm before deploying production."
    _entry(conn, "a", quote)
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(state, conn, read.id, [
        _item("rule", "Confirm before deploying production changes.", (1, quote)),
    ], tier="cloud")
    merge = reader.next_task(state, conn, tier="cloud", reader="agent")
    conn.execute("UPDATE entries SET sensitivity = 'sensitive' WHERE id = 'a'")
    conn.commit()
    result = reader.accept(state, conn, merge.id, [
        _merged("rule", "Confirm before deploying production changes.", 1),
    ], tier="cloud")
    assert not result.items and result.dropped == 1


def test_older_reader_stands_down_without_overwriting_newer_profile(conn, tmp_path):
    path = tmp_path / "profile.json"
    original = json.dumps({"version": reader.PROFILE_VERSION + 1,
                           "read": {"a": "a"}, "notes": [], "dirty": [], "tasks": {}})
    path.write_text(original, encoding="utf-8")
    _entry(conn, "a", "Always run the relevant tests before reporting success.")
    state = reader.ProfileState(path)
    assert state.newer
    assert reader.waiting(state, conn, "cloud") == 0
    assert reader.next_task(state, conn, tier="cloud", reader="agent") is None
    state.save()
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("old_version", [3, 4, 5, 6, 7])
def test_current_generation_rereads_archives_from_prior_generations(conn, tmp_path, old_version):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": old_version, "read": {"a": "a"}}), encoding="utf-8")
    _entry(conn, "a", "Always run the relevant tests before reporting success.")
    state = reader.ProfileState(path)
    assert old_version < reader.PROFILE_VERSION
    assert not state.newer and not state.read
    assert reader.waiting(state, conn, "cloud") == 1
    assert reader.next_task(state, conn, tier="cloud", reader="agent").kind == "read"


@pytest.mark.parametrize("two_projects", [False, True])
def test_work_recurrence_in_different_projects_reaches_final_global_validation(
    conn, tmp_path, two_projects,
):
    statement = "Run the relevant tests before reporting a change finished."
    _entry(conn, "a", "Run the relevant tests before reporting a change finished.",
           project="shop", thread="one")
    if two_projects:
        _entry(conn, "b", "You forgot the relevant tests before reporting success again.",
               project="blog", thread="two")
    state = reader.ProfileState(tmp_path / "profile.json")
    for project in (["shop", "blog"] if two_projects else ["shop"]):
        read = reader.next_task(state, conn, tier="cloud", reader="agent")
        assert read.project == project
        quote = ("Run the relevant tests" if project == "shop" else "the relevant tests")
        reader.accept(state, conn, read.id, [
            _item("rule", statement, (1, quote), applies="work"),
        ], tier="cloud")
    while (merge := reader.next_task(state, conn, tier="cloud", reader="agent")):
        count = len(state.tasks[merge.id]["notes"])
        result = reader.accept(state, conn, merge.id, [
            _merged("rule", statement, *range(1, count + 1), applies="work"),
        ], tier="cloud")
        if merge.project != reader.GLOBAL_SCOPE:
            assert not result.items
        else:
            assert len(result.items) == (1 if two_projects else 0)
            if result.items:
                assert result.items[0].scope == reader.GLOBAL_SCOPE
                assert result.items[0].messages == 2


@pytest.mark.parametrize("text,slot,expected", [
    ("I prefer change reports as brief bullet lists rather than paragraphs.", "rule", True),
    ("My usual workflow is inspect the diff, run focused tests, then package the build.",
     "workflow", True),
    ("For every release, inspect the diff, run focused tests, then package the build.",
     "workflow", True),
    ("Always inspect the diff, run focused tests, then package the build.", "workflow", True),
    ("I want change reports as brief bullet lists.", "rule", False),
    ("I prefer this button to be wider.", "rule", False),
    ("I prefer brief bullet lists on this screen.", "rule", False),
    ("I prefer brief bullet lists today.", "rule", False),
    ("I used to prefer brief bullet lists.", "rule", False),
    ("I do not prefer brief bullet lists.", "rule", False),
    ('The client said "I prefer brief bullet lists".', "rule", False),
    ('An example is "I prefer brief bullet lists".', "rule", False),
    ('Do you mean "I prefer brief bullet lists"?', "rule", False),
])
def test_singleton_durability_comes_from_validated_owner_context(
    conn, tmp_path, text, slot, expected,
):
    _entry(conn, "a", text)
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    # Deliberately shorten quoted examples: their surrounding owner context
    # must still prevent another person's preference from becoming durable.
    quote = "I prefer brief bullet lists" if '"I prefer brief bullet lists"' in text else text
    extra = ({"name": "release", "steps": ["Inspect the diff", "Run focused tests", "Package"]}
             if slot == "workflow" else {})
    statement = "Write change reports as brief bullet lists." if slot != "workflow" else ""
    result = reader.accept(state, conn, read.id, [
        _item(slot, statement, (1, quote), **extra),
    ], tier="cloud")
    if not result.notes:
        assert not expected
        return
    # Generic working preferences can forward to the global merge;
    # singleton validity is asserted only after the whole pipeline finishes.
    found = []
    while (merge := reader.next_task(state, conn, tier="cloud", reader="agent")):
        result = reader.accept(state, conn, merge.id, [
            _merged(slot, statement, 1, **extra),
        ], tier="cloud")
        found.extend(result.items)
    assert bool(found) == expected


@pytest.mark.parametrize("global_work", [False, True])
def test_prohibitions_keep_real_scope_and_can_be_queued_confirmed_and_retrieved(
    conn, tmp_path, global_work,
):
    prefix = "Across every project, " if global_work else "In the shop, "
    text = prefix + "never delete backups unless I approve a cleanup."
    statement = "Never delete backups unless the owner approves a cleanup."
    applies = "work" if global_work else "project"
    _entry(conn, "a", text)
    state = reader.ProfileState(tmp_path / "profile.json")
    read = reader.next_task(state, conn, tier="cloud", reader="agent")
    reader.accept(state, conn, read.id, [
        _item("never", statement, (1, text), applies=applies),
    ], tier="cloud")
    found = []
    while (merge := reader.next_task(state, conn, tier="cloud", reader="agent")):
        result = reader.accept(state, conn, merge.id, [
            _merged("never", statement, 1, applies=applies),
        ], tier="cloud")
        found.extend(result.items)
    [item] = found
    assert item.scope == (reader.GLOBAL_SCOPE if global_work else "shop")
    store = SQLitePrincipleStore(conn)
    caller, device_id = _owner(tmp_path)
    candidate = store.propose(caller=caller, device_id=device_id,
                              **item.principle(reader.SOURCES["agent"]))
    assert candidate
    store.confirm(candidate, caller=caller, device_id=device_id)
    shop, _ = store.applicable("clean old backups", project="shop")
    blog, _ = store.applicable("clean old backups", project="blog")
    assert [match.principle.statement for match in shop] == [statement]
    assert [match.principle.statement for match in blog] == ([statement] if global_work else [])
