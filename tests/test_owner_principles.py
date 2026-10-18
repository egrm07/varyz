"""Varyz as an advisor: the owner's rules reach the agent before it builds.

The agent plans and judges; varyz supplies how the owner would have it done.
Only confirmed rules are advice. Mined suggestions wait for the owner, and the
ledger keeps every accept, reword and rejection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from varyz import hooks
from varyz.db.connection import open_db
from varyz.mcp_server import _UNTRUSTED_NOTE, INSTRUCTIONS, LEGACY_TOOLS, TOOLS, VaryzServer
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.advice import HOW_TO_USE, advise
from varyz.resolution.briefing import start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller, ResolutionRequest
from varyz.resolution.principles import GLOBAL_SCOPE, SQLitePrincipleStore, normalize_scope
from varyz.resolution.rule_miner import (
    MiningStats,
    mine_files,
    mine_turns,
    rules_in_text,
    select,
)
from varyz.trust import grant_agent_capability, register_agent

OWNER = Caller(
    agent_id="owner:local",
    display_name="Local owner",
    purpose="owner_knowledge",
    device_id="device:test",
    transport="stdio",
)
DEVICE = "device:test"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SQLitePrincipleStore:
    return SQLitePrincipleStore(conn)


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db",
        host="127.0.0.1",
        port=0,
        device_id=DEVICE,
        notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


def _add(store: SQLitePrincipleStore, statement: str, **kwargs) -> str:
    return store.add(
        statement=statement, caller=OWNER, device_id=DEVICE, source="manual", **kwargs
    )


# -- the ledger ---------------------------------------------------------------


def test_adding_the_same_rule_twice_keeps_one(store: SQLitePrincipleStore) -> None:
    first = _add(store, "Run the whole loop in dry-run before calling it done.")
    again = _add(store, "  run the whole loop in DRY-RUN before calling it done ")

    assert first == again
    assert [rule.id for rule in store.current()] == [first]


def test_scope_aliases_mean_every_project() -> None:
    for alias in ("", "all", "Global", "*", None, "all_projects", "All Projects",
                  "all-projects", "every project", "every_project", "every-project",
                  "all my projects", "all_my_projects", "all-my-projects",
                  "  ALL   PROJECTS  "):
        assert normalize_scope(alias) == "*"
    assert normalize_scope("  SAN7.14 ") == "san7"
    for name in ("all_projects_tool", "my-projects", "project_all", "alloy",
                 "org:alpha", "published:alpha"):
        assert normalize_scope(name) == name


def test_a_folders_version_is_not_part_of_its_project() -> None:
    for folder in ("SAN7.13", "san7.14", "San7.15", "san7"):
        assert normalize_scope(folder) == "san7"
    assert normalize_scope("shop-v2") == normalize_scope("shop_v2.1") == "shop"
    for name in ("python3", "node.js", "example.com", "gpt-4", "1.2", "v2"):
        assert normalize_scope(name) == name


def _stored_as(conn: sqlite3.Connection, table: str, row_id: str, scope: str) -> None:
    # A row written before folders' versions were folded; the ledger is append-only.
    conn.execute(f"DROP TRIGGER {table}_no_update")
    conn.execute(f"UPDATE {table} SET scope = ? WHERE id = ?", (scope, row_id))
    conn.commit()


def test_rules_stored_under_a_folders_version_hold_in_every_version(
    conn: sqlite3.Connection, store: SQLitePrincipleStore
) -> None:
    rule = _add(store, "Keep the SAN report in one table.", scope="san7")
    _stored_as(conn, "owner_principles", rule, "san7.14")

    assert store.get(rule).scope == "san7"
    assert _add(store, "Keep the SAN report in one table.", scope="SAN7.15") == rule
    chosen, _ = store.applicable("Write the report", project="SAN7.15")
    assert [item.principle.id for item in chosen] == [rule]


@pytest.mark.parametrize("scope", ["all_projects", "all projects", "all-my-projects"])
def test_legacy_global_labels_apply_everywhere_without_rewriting_history(
    conn: sqlite3.Connection, store: SQLitePrincipleStore, scope: str,
) -> None:
    statement = "Avoid neon design accents."
    rule = _add(store, statement)
    _stored_as(conn, "owner_principles", rule, scope)

    assert store.get(rule).scope == GLOBAL_SCOPE
    assert _add(store, statement, scope="all") == rule
    for project in ("varyz", "san7", "future-project"):
        chosen, _ = store.applicable("Build the page", project=project)
        assert [item.principle.id for item in chosen] == [rule]
        briefing = start_task(conn, "Build the page", project=project)
        assert [item["id"] for item in briefing["how_owner_works"]] == [rule]
        assert briefing["project_rules"] == []
    assert (
        conn.execute("SELECT scope FROM owner_principles WHERE id = ?", (rule,)).fetchone()[0]
        == scope
    )


def _seed(store: SQLitePrincipleStore, statement: str, **kwargs) -> str:
    # A starter rule as setup seeded it until 2026-09-29.
    return store.add(
        statement=statement, caller=OWNER, device_id=DEVICE, source="standards_seed",
        **kwargs,
    )


def test_rules_an_earlier_setup_seeded_no_longer_apply(
    store: SQLitePrincipleStore,
) -> None:
    seeded = _seed(store, "Keep the change to what was asked.")
    _seed(store, "Add a favicon.", domain="website")
    own = _add(store, "Fix what you find in the code you are working on.")

    assert [rule.id for rule in store.current()] == [own]
    chosen, left_out = store.applicable(
        "Fix the text bug on the website", project="varyz", domains=("website",)
    )
    assert [item.principle.id for item in chosen] == [own] and left_out == 0
    assert store.checklist(("website",), project="varyz") == {}
    assert store.stats()["by_source"] == {"manual": {"confirmed": 1, "candidate": 0}}
    assert store.get(seeded) is not None


def test_a_starter_rule_the_owner_states_is_theirs(store: SQLitePrincipleStore) -> None:
    seeded = _seed(store, "Keep the change to what was asked.")
    _seed(store, "Run the tests before calling it done.")

    own = _add(store, "Keep the change to what was asked.")
    mined = store.propose(
        statement="Run the tests before calling it done.", caller=OWNER, device_id=DEVICE,
        source="mined:ai_conversations",
    )

    assert own != seeded
    assert mined is not None
    assert [rule.id for rule in store.current()] == [own]
    assert [rule.id for rule in store.current("candidate")] == [mined]


def _learned(store: SQLitePrincipleStore, statement: str, project: str) -> str | None:
    return store.propose(
        statement=statement, caller=OWNER, device_id=DEVICE,
        source="mined:ai_conversations", scope=project,
    )


def test_a_rule_learned_in_two_projects_is_queued_once_for_every_project(
    conn: sqlite3.Connection, store: SQLitePrincipleStore,
) -> None:
    website = _learned(store, "Never use em-dashes.", "website")
    lifted = _learned(store, "never use em dashes!", "marketing")

    assert lifted is not None and lifted != website
    [candidate] = store.current("candidate")
    assert (candidate.id, candidate.scope) == (lifted, GLOBAL_SCOPE)
    assert candidate.provenance["lifted_from"] == ["marketing", "website"]
    # Still the owner's to keep: nothing is confirmed for them.
    assert store.current() == ()
    # A third project adds nothing: it is already asked for every project.
    assert _learned(store, "Never use em-dashes", "persona") is None
    assert [rule.id for rule in store.current("candidate")] == [lifted]

    store.confirm(lifted, caller=OWNER, device_id=DEVICE)
    briefing = start_task(conn, "Write the launch copy", project="persona")
    assert [rule["statement"] for rule in briefing["how_owner_works"]] == [
        "never use em dashes!"]


def test_a_rule_kept_for_one_project_stays_beside_its_candidate_for_every_project(
    store: SQLitePrincipleStore,
) -> None:
    kept = _add(store, "Show the generated output before claiming done.", scope="marketing")
    lifted = _learned(store, "show the generated output before claiming done", "website")

    assert [rule.id for rule in store.current()] == [kept]
    [candidate] = store.current("candidate")
    assert (candidate.id, candidate.scope) == (lifted, GLOBAL_SCOPE)
    # Said in one project only, a rule stays that project's.
    alone = _learned(store, "Keep the shop's checkout on one page.", "shop")
    assert store.get(alone).scope == "shop"


def test_rules_for_every_project_reach_a_project_with_many_rules_of_its_own(
    conn: sqlite3.Connection, store: SQLitePrincipleStore,
) -> None:
    for number in range(6):
        _add(store, f"Shop pages rule {number}: keep shop pages light.", scope="shop")
    everywhere = _add(store, "Never use em-dashes.")

    chosen, left_out = store.applicable("Build the shop pages", project="shop", limit=4)
    assert len(chosen) == 4 and left_out == 3
    assert everywhere in [item.principle.id for item in chosen]
    briefing = start_task(conn, "Build the shop pages", project="shop", limit=4)
    assert [rule["id"] for rule in briefing["how_owner_works"]] == [everywhere]


def test_an_answer_stored_under_a_folders_version_answers_the_next(
    conn: sqlite3.Connection,
) -> None:
    knowledge = SQLiteKnowledgeStore(conn)
    entry = knowledge.remember(
        question="Which unit does the SAN report use?", answer="Kilonewtons.",
        caller=OWNER, device_id=DEVICE, basis=AnswerBasis.EXPLICIT, source="manual",
        scope="san7",
    )
    _stored_as(conn, "owner_knowledge", entry, "san7.14")

    assert knowledge.get(entry).scope == "san7"
    request = ResolutionRequest(
        question="Which unit does the SAN report use?", caller=OWNER, purpose=OWNER.purpose,
        project="SAN7.15",
    )
    resolution = knowledge.try_resolve(request)
    assert resolution is not None and resolution.answer == "Kilonewtons."


def test_a_vision_belongs_to_one_project(store: SQLitePrincipleStore) -> None:
    with pytest.raises(ValueError, match="one project"):
        _add(store, "An advisor, never the lead.", kind="vision")
    assert _add(store, "An advisor, never the lead.", kind="vision", scope="varyz")


def test_confirming_supersedes_the_candidate_and_rejection_is_remembered(
    store: SQLitePrincipleStore,
) -> None:
    keep = store.propose(
        statement="Always pass the config flag.", caller=OWNER, device_id=DEVICE,
        source="mined:file",
    )
    drop = store.propose(
        statement="Never write tests for scripts.", caller=OWNER, device_id=DEVICE,
        source="mined:file",
    )
    assert keep and drop
    assert len(store.current("candidate")) == 2

    confirmed = store.confirm(
        keep, caller=OWNER, device_id=DEVICE,
        statement="Always pass --config to the varyz CLI.", scope="varyz",
    )
    store.retract(drop, caller=OWNER, device_id=DEVICE, reason="rejected in review")

    assert store.current("candidate") == ()
    [rule] = store.current()
    assert rule.id == confirmed
    assert rule.statement == "Always pass --config to the varyz CLI."
    assert rule.scope == "varyz"
    assert rule.supersedes_id == keep
    assert rule.provenance["candidate_source"] == "mined:file"
    # Re-mining the same words never asks the owner again.
    for statement in ("Always pass the config flag.", "Never write tests for scripts."):
        assert store.propose(
            statement=statement, caller=OWNER, device_id=DEVICE, source="mined:file"
        ) is None
    with pytest.raises(ValueError, match="not a pending candidate"):
        store.confirm(confirmed, caller=OWNER, device_id=DEVICE)
    assert store.stats()["acceptance_rate"] == 0.5


def test_the_ledger_cannot_be_rewritten(
    store: SQLitePrincipleStore, conn: sqlite3.Connection
) -> None:
    rule_id = _add(store, "Never commit unless asked.")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE owner_principles SET statement='x' WHERE id=?", (rule_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM owner_principles WHERE id=?", (rule_id,))
    store.retract(rule_id, caller=OWNER, device_id=DEVICE)
    assert store.current() == ()
    with pytest.raises(ValueError, match="already superseded or withdrawn"):
        store.retract(rule_id, caller=OWNER, device_id=DEVICE)
    with pytest.raises(KeyError):
        store.retract("nope", caller=OWNER, device_id=DEVICE)


# -- advice ---------------------------------------------------------------------


def test_advice_keeps_other_projects_out_and_puts_the_vision_first(
    store: SQLitePrincipleStore, conn: sqlite3.Connection
) -> None:
    _add(store, "Run the whole loop in dry-run before calling anything done.")
    _add(store, "Never create git commits unless asked.")
    _add(store, "Use Expo EAS for every iOS release build.", scope="san7.14")
    vision = _add(
        store, "varyz advises coding agents; it never plans or leads.",
        kind="vision", scope="varyz",
    )
    _add(store, "Pass --config explicitly on every varyz command.", scope="varyz")

    result = advise(conn, "Add a git commit hook that runs the tests", project="varyz")

    assert result["known"] is True
    assert [item["id"] for item in result["project_vision"]] == [vision]
    statements = [item["statement"] for item in result["owner_rules"]]
    assert statements[0] == "Never create git commits unless asked."
    assert "Use Expo EAS for every iOS release build." not in statements
    assert len(statements) == 3
    assert result["owner_rules"][0]["matched_terms"] == ["commit", "git"]
    # A rule for another project applies when the task names that project.
    named = advise(conn, "Bump the SAN7.14 build number", project="varyz")
    assert any("Expo EAS" in item["statement"] for item in named["owner_rules"])


def test_advice_is_bounded_and_says_how_much_it_left_out(
    store: SQLitePrincipleStore, conn: sqlite3.Connection
) -> None:
    for number in range(5):
        _add(store, f"Always keep rule number {number} in mind while building.")
    chosen, withheld = store.applicable("anything", project="varyz", limit=2)
    assert len(chosen) == 2
    assert withheld == 3


def test_empty_advice_is_an_answer_not_a_guess(conn: sqlite3.Connection) -> None:
    result = advise(conn, "Refactor the parser", project="varyz")
    assert result["known"] is False
    assert result["owner_rules"] == result["project_vision"] == []
    assert "no confirmed rule" in result["how_to_use"]


def test_advice_carries_related_owner_decisions_and_pending_count(
    store: SQLitePrincipleStore, conn: sqlite3.Connection
) -> None:
    SQLiteKnowledgeStore(conn).remember(
        question="Should the relay store answers as explicit knowledge?",
        answer="Yes, as user_confirmed.",
        caller=OWNER,
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id=DEVICE,
    )
    store.propose(
        statement="Always write a dated report.", caller=OWNER, device_id=DEVICE,
        source="mined:ai_conversations",
    )
    result = advise(conn, "Change how the relay stores confirmed answers", project="varyz")
    [decision] = result["owner_decisions"]
    assert decision["answer"] == "Yes, as user_confirmed."
    # Candidates are counted, never shown.
    assert result["pending_rule_suggestions"] == 1
    assert "Always write a dated report." not in str(result)


# -- daemon and client -----------------------------------------------------------


def test_rules_travel_through_the_daemon(daemon_client: ResolutionClient) -> None:
    rule_id = daemon_client.add_principle(
        statement="Dry-run first, always.", caller=OWNER, device_id=DEVICE
    )
    candidate = daemon_client.add_principle(
        statement="Prefer uv for new tools.", caller=OWNER, device_id=DEVICE,
        source="mined:file", status="candidate", provenance={"score": 4},
    )
    assert candidate is not None
    assert daemon_client.add_principle(
        statement="prefer uv for new tools", caller=OWNER, device_id=DEVICE,
        status="candidate",
    ) is None
    assert [item["id"] for item in daemon_client.principles()] == [rule_id]
    [pending] = daemon_client.principles("candidate")
    assert pending["provenance"] == {"score": 4}

    kept = daemon_client.confirm_principle(candidate, caller=OWNER, device_id=DEVICE)
    assert {item["id"] for item in daemon_client.principles()} == {rule_id, kept}
    daemon_client.retract_principle(rule_id, caller=OWNER, device_id=DEVICE)
    assert [item["id"] for item in daemon_client.principles()] == [kept]

    with pytest.raises(Exception, match="HTTP 400"):
        daemon_client.add_principle(
            statement="x y z w", caller=OWNER, device_id=DEVICE, kind="mood"
        )


# -- MCP -----------------------------------------------------------------------


def test_the_workflow_tools_lead_the_listing_and_advise_stays_callable() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    legacy = {tool["name"]: tool for tool in LEGACY_TOOLS}
    assert list(tools)[:3] == ["start_task", "ask", "finish_check"]
    assert "advise" not in tools and legacy["advise"]["annotations"]["readOnlyHint"] is True
    assert tools["remember_owner_rule"]["annotations"]["readOnlyHint"] is False
    assert tools["remember_owner_rule"]["inputSchema"]["required"] == ["statement"]
    assert "replaces" in tools["remember_owner_rule"]["inputSchema"]["properties"]
    for name in ("start_task", "ask", "finish_check", "remember_owner_rule"):
        assert tools[name]["description"].endswith(_UNTRUSTED_NOTE)
    assert "AskUserQuestion" in tools["remember_owner_rule"]["description"]
    for name in ("start_task", "ask", "finish_check", "remember_owner_rule"):
        assert name in INSTRUCTIONS
    for text in (INSTRUCTIONS, tools["remember_owner_rule"]["description"], HOW_TO_USE,
                 hooks.SESSION_START_CONTEXT):
        # A rule the user corrects is updated with the correction.
        assert "replaces" in text and "which rule you updated" in text


def _relaying_server(tmp_path: Path, daemon_client: ResolutionClient) -> VaryzServer:
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:claude-code:local", display_name="Claude Code", device_id="local"
    ).token
    conn.close()
    return VaryzServer(_config(tmp_path), agent_token=token, resolution_client=daemon_client)


def test_a_rule_the_user_corrects_is_replaced_by_the_correction(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    old = daemon_client.add_principle(
        statement="Use bold headings on every screen.", caller=OWNER, device_id=DEVICE,
        scope="shop", domain="website", rationale="Headings must stand out.")
    server = _relaying_server(tmp_path, daemon_client)
    try:
        updated = server.call("remember_owner_rule", {
            "statement": "Use thin headings on every screen, never bold.", "replaces": old,
        })
        again = server.call("remember_owner_rule", {
            "statement": "Use light headings.", "replaces": old,
        })
    finally:
        server.close()

    assert updated["recorded"] is True
    assert updated["replaced"] == "Use bold headings on every screen."
    assert updated["scope"] == "shop" and updated["definition_of_done_for"] == "website"
    assert "which rule you updated" in updated["note"]
    # Only the correction is current, where the old rule applied, and it says what it replaced.
    [rule] = daemon_client.principles()
    assert (rule["statement"], rule["scope"], rule["domain"], rule["supersedes_id"]) == (
        "Use thin headings on every screen, never bold.", "shop", "website", old)
    assert rule["rationale"] == "Headings must stand out."
    assert rule["provenance"]["corrects"] == old
    assert rule["provenance"]["corrected_statement"] == "Use bold headings on every screen."
    # A rule already replaced cannot be corrected again by its old id.
    assert again["recorded"] is False and "already replaced" in again["error"]
    assert len(daemon_client.principles()) == 1


def test_a_correction_may_move_a_rule_and_a_new_rule_still_needs_its_scope(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    old = daemon_client.add_principle(
        statement="Push after every commit.", caller=OWNER, device_id=DEVICE)
    server = _relaying_server(tmp_path, daemon_client)
    try:
        moved = server.call("remember_owner_rule", {
            "statement": "Push after every commit in this project only.",
            "replaces": old, "scope": "varyz",
        })
        with pytest.raises(ValueError, match="scope is required"):
            server.call("remember_owner_rule", {"statement": "Keep commits small."})
    finally:
        server.close()
    assert moved["scope"] == "varyz"
    assert [(item["statement"], item["scope"]) for item in daemon_client.principles()] == [
        ("Push after every commit in this project only.", "varyz")]


def test_an_agent_relays_a_rule_and_every_agent_is_then_advised_with_it(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    config = _config(tmp_path)
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:claude-code:local", display_name="Claude Code", device_id="local"
    ).token
    conn.close()
    server = VaryzServer(config, agent_token=token, resolution_client=daemon_client)
    try:
        recorded = server.call("remember_owner_rule", {
            "statement": "Never create git commits unless I ask.",
            "scope": "all",
            "why": "I review the diff myself.",
        })
        server.call("remember_owner_rule", {
            "statement": "Keep reports aggregate-only on live data.", "scope": "varyz",
        })
        advice = server.call("advise", {
            "plan": "Finish the feature and commit it to git", "project": "varyz",
        })
    finally:
        server.close()

    assert recorded["recorded"] is True
    assert recorded["scope"] == "all projects"
    [rule] = [item for item in daemon_client.principles() if "commits" in item["statement"]]
    assert rule["source"] == "agent_relay:agent:claude-code:local"
    assert rule["rationale"] == "I review the diff myself."
    first = advice["owner_rules"][0]
    assert first["statement"] == "Never create git commits unless I ask."
    assert first["why"] == "I review the diff myself."
    assert len(advice["owner_rules"]) == 2
    assert "withheld_hint" not in advice


def test_an_unregistered_agent_gets_only_the_most_relevant_rule(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    for statement in ("Never create git commits unless asked.", "Always dry-run first."):
        daemon_client.add_principle(statement=statement, caller=OWNER, device_id=DEVICE)
    server = VaryzServer(_config(tmp_path), agent_token="", resolution_client=daemon_client)
    try:
        advice = server.call("advise", {"plan": "commit the change to git", "project": "x"})
    finally:
        server.close()
    assert [item["statement"] for item in advice["owner_rules"]] == [
        "Never create git commits unless asked."
    ]
    assert advice["more_rules_in_scope"] == 1
    assert "varyz install" in advice["withheld_hint"]


def test_a_grant_without_owner_knowledge_gets_no_rules(
    tmp_path: Path, daemon_client: ResolutionClient
) -> None:
    daemon_client.add_principle(statement="Always dry-run first.", caller=OWNER, device_id=DEVICE)
    conn = open_db(tmp_path / "data" / "varyz.db")
    token = register_agent(
        conn, agent_id="agent:narrow", display_name="Narrow", purpose="summaries_only"
    ).token
    grant_agent_capability(
        conn, "agent:narrow", purpose="owner_context", resource_types=("summary",)
    )
    conn.commit()
    conn.close()
    server = VaryzServer(_config(tmp_path), agent_token=token, resolution_client=daemon_client)
    try:
        advice = server.call("advise", {"plan": "dry-run it", "project": "x"})
        recorded = server.call("remember_owner_rule", {"statement": "a b c d", "scope": "all"})
    finally:
        server.close()
    assert advice["error"] == "caller_capability_denied"
    assert "owner_rules" not in advice
    assert recorded == {"recorded": False, "error": "caller_capability_denied"}
    assert len(daemon_client.principles()) == 1


def test_advise_names_a_misnamed_argument(tmp_path: Path, daemon_client) -> None:
    server = VaryzServer(_config(tmp_path), agent_token="", resolution_client=daemon_client)
    try:
        with pytest.raises(ValueError, match="it takes plan"):
            server.call("advise", {"task": "do it"})
    finally:
        server.close()


# -- mining --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No, always run the whole loop in dry-run first.",
         "Always run the whole loop in dry-run first."),
        ("From now on never push to the remote without asking me.",
         "From now on never push to the remote without asking me."),
        ("actually, I want every report to lead with the numbers",
         "I want every report to lead with the numbers"),
        ("Fix the text bug, dont use m dashes use - instead.",
         "Fix the text bug, dont use m dashes use - instead."),
    ],
)
def test_corrections_become_rules_in_the_owners_words(text: str, expected: str) -> None:
    assert [statement for statement, _ in rules_in_text(text, correction_turn=True)] == [
        expected
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Can you always run the tests?",
        "Fix this file please",
        "Looks good, thanks",
        "Don't change that line.",
        "Run `pytest -q` and always check {config}",
        "see https://example.com/always for why this should work",
        "What do i need to do, if anything",
        "How long do i need to use varyz for to get a card",
        "Will it generate once I run it or does it never.",
        "I need to go.",
        "Stop everything for now",
        "I dont want to create the accounts for publishing yet.",
    ],
)
def test_questions_one_offs_and_code_are_not_rules(text: str) -> None:
    assert list(rules_in_text(text, correction_turn=True)) == []


def _entry(conn, entry_id, body, *, authored=1, sensitivity="personal"):
    conn.execute(
        """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
               occurred_start, ingested_at, thread_id, sensitivity, authored_by_user)
           VALUES (?, 'message', 'ai_conversations', ?, ?, ?, ?, ?, 'ai:claude_code:s1', ?, ?)""",
        (entry_id, entry_id, entry_id, body,
         datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
         datetime(2026, 9, 1, tzinfo=UTC).isoformat(), sensitivity, authored),
    )


def test_mining_reads_only_the_owners_non_sensitive_turns(conn: sqlite3.Connection) -> None:
    header = "From: me\nConversation: Claude Code — varyz\n\n"
    _entry(conn, "mine", header + "No, always write the report before the summary.")
    _entry(conn, "secret", header + "Never tell anyone the vault password.",
           sensitivity="sensitive")
    _entry(conn, "assistant", "From: Claude Code assistant\nConversation: Claude Code — "
           "varyz\n\nI will always run the tests first.", authored=0)
    _entry(conn, "mangled", "From: me\nConversation: Claude Code — C--Users-egor\n\n"
           "Always prefer uv over pip for new tools.")
    conn.commit()

    stats = MiningStats()
    chosen = select(mine_turns(conn, stats), stats)

    assert {(item.statement, item.scope) for item in chosen} == {
        ("Always write the report before the summary.", "varyz"),
        ("Always prefer uv over pip for new tools.", "*"),
    }
    assert stats.turns_read == 2
    assert stats.sensitive_skipped == 1
    assert chosen[0].provenance["thread_id"] == "ai:claude_code:s1"


def test_mining_instruction_files_skips_headings_tables_and_code(tmp_path: Path) -> None:
    notes = tmp_path / "CLAUDE.md"
    notes.write_text(
        "# Always a heading\n"
        "- Dry-run first, always: every system runs its loop with zero keys.\n"
        "| Always | a table |\n"
        "```\nnever run this code block\n```\n"
        "- Never create commits unasked.\n"
        "- Never create commits unasked.\n",
        encoding="utf-8",
    )
    stats = MiningStats()
    chosen = select(mine_files([tmp_path], stats, scope="*"), stats, cap=1)
    assert stats.files_read == 1
    assert stats.duplicates == 1
    assert stats.capped == 1
    assert len(chosen) == 1
    assert chosen[0].provenance["file"].endswith("CLAUDE.md")


def test_a_hard_wrapped_rule_is_mined_whole(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "**Cross-project patterns (the things I always do, in every project):**\n"
        "\n"
        "5. **Privacy is fail-closed.** Secrets always live in Windows\n"
        "   Credential Manager or an untracked env file; never logged.\n"
        "6. Never create commits\n"
        "   unasked.\n",
        encoding="utf-8",
    )
    stats = MiningStats()
    chosen = select(mine_files([tmp_path], stats, scope="*"), stats)

    statements = sorted(candidate.statement for candidate in chosen)
    assert statements == [
        "Never create commits unasked.",
        "Secrets always live in Windows Credential Manager or an untracked env file; "
        "never logged.",
    ]
    assert sorted(candidate.provenance["line"] for candidate in chosen) == [3, 5]


def test_a_folder_is_mined_without_its_dependencies(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("- Never create commits unasked.\n", encoding="utf-8")
    for vendored in ("node_modules/left-pad", ".venv/lib", "vendor/x"):
        (tmp_path / vendored).mkdir(parents=True)
        (tmp_path / vendored / "README.md").write_text(
            "- Always pin the version you install.\n", encoding="utf-8"
        )
    stats = MiningStats()
    chosen = select(mine_files([tmp_path], stats, scope="*"), stats)

    assert stats.files_read == 1
    assert [candidate.statement for candidate in chosen] == ["Never create commits unasked."]


# -- CLI -----------------------------------------------------------------------


def test_cli_mine_is_a_dry_run_of_counts_and_apply_queues_for_review(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch
) -> None:
    from varyz import cli

    config = _config(tmp_path)
    notes = tmp_path / "AGENTS.md"
    notes.write_text("- Always lead reports with the numbers.\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    runner = CliRunner()

    dry = runner.invoke(cli.app, [
        "rules", "mine", "--from-file", str(notes), "--config", str(config),
    ])
    assert dry.exit_code == 0, dry.output
    assert "Always lead reports" not in dry.output
    assert "Dry run" in dry.output
    assert daemon_client.principles("candidate") == ()

    applied = runner.invoke(cli.app, [
        "rules", "mine", "--from-file", str(notes), "--apply", "--config", str(config),
    ])
    assert applied.exit_code == 0, applied.output
    assert len(daemon_client.principles("candidate")) == 1

    reviewed = runner.invoke(
        cli.app, ["rules", "review", "--terminal", "--config", str(config)],
        input="s\nvaryz\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    [rule] = daemon_client.principles()
    assert rule["scope"] == "varyz"
    assert "1 kept" in reviewed.output

    stats = runner.invoke(cli.app, ["rules", "stats", "--config", str(config)])
    assert "acceptance rate" in stats.output and "1.0" in stats.output


def test_explicit_owner_rule_precedes_more_relevant_learned_trials(
    store: SQLitePrincipleStore,
) -> None:
    for i in range(15):
        store.add(statement=f"Landing page hero design preference {i}", caller=OWNER,
                  device_id=DEVICE, source="agent:profile",
                  provenance={"trial": {"signals": ["correction"]}})
    rule_id = _add(store, "dont use . in titles", scope="all")
    selected, omitted = store.applicable("Build a landing page hero design", limit=3)
    assert selected[0].principle.id == rule_id
    assert omitted == 13


def test_active_agent_receives_another_agents_correction_at_progress_checkpoint(
    tmp_path: Path, daemon_client: ResolutionClient,
) -> None:
    server = _relaying_server(tmp_path, daemon_client)
    checkpoint = {"project": "persona", "conversation_id": "live-work",
                  "checkpoint_id": "before", "task": "Build a landing page"}
    try:
        assert server.call("record_progress", checkpoint)["recorded"]
        rule = server.call("remember_owner_rule", {
            "statement": "dont use . in titles", "scope": "all",
        })
        server.call("remember_owner_rule", {
            "statement": "Only SAN7 titles use blue text", "scope": "san7",
        })
        result = server.call("record_progress", {**checkpoint, "checkpoint_id": "after"})
        updates = result["preference_updates"]
        assert [r["id"] for r in updates["rules"]] == [rule["rule_id"]]
        assert updates["rules"][0]["scope"] == "all projects"
        again = server.call("record_progress", {**checkpoint, "checkpoint_id": "again"})
        assert "preference_updates" not in again
        replacement = server.call("remember_owner_rule", {
            "statement": "Don't use periods in headings either", "replaces": rule["rule_id"],
        })
        after = server.call("record_progress", {**checkpoint, "checkpoint_id": "replaced"})
        assert after["preference_updates"]["no_longer_active"] == [rule["rule_id"]]
        assert after["preference_updates"]["rules"][0]["id"] == replacement["rule_id"]
    finally:
        server.close()


def test_live_update_budget_omissions_are_retried(
    tmp_path: Path, daemon_client: ResolutionClient, monkeypatch,
) -> None:
    import varyz.trust

    server = _relaying_server(tmp_path, daemon_client)
    checkpoint = {"project": "persona", "conversation_id": "budget-work",
                  "checkpoint_id": "before", "task": "Build a landing page"}
    try:
        server.call("record_progress", checkpoint)
        rule = server.call("remember_owner_rule", {
            "statement": "dont use . in titles", "scope": "all",
        })
        real_limits = varyz.trust.capability_limits
        monkeypatch.setattr(varyz.trust, "capability_limits", lambda conn, caller: (20, 1))
        small = server.call("record_progress", {**checkpoint, "checkpoint_id": "small"})
        assert "preference_updates" not in small
        monkeypatch.setattr(varyz.trust, "capability_limits", real_limits)
        result = server.call("record_progress", {**checkpoint, "checkpoint_id": "normal"})
        assert result["preference_updates"]["rules"][0]["id"] == rule["rule_id"]
    finally:
        server.close()
