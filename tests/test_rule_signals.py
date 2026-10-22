"""Whether rules held, what answers became, and what the owner corrected: kept locally,
and shared only as clean, linkable labels after opt-in."""

import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from varyz import analytics, contribute
from varyz.config import Config
from varyz.connectors.ai_conversations import Turn
from varyz.db.connection import open_db
from varyz.domain import new_id
from varyz.resolution import rule_signals
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore

KEY = "sk-ant-" + "a" * 32


def caller() -> Caller:
    return Caller(agent_id="claude-code", display_name="Claude Code", purpose="coding",
                  device_id="device:agent")


@pytest.fixture
def cfg(tmp_path):
    return Config(base_dir=tmp_path)


@pytest.fixture
def conn(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = open_db(cfg.db_path)
    yield db
    db.close()


def rule(conn, statement="Use pnpm, never npm", *, provenance=None, supersedes=None):
    return SQLitePrincipleStore(conn).add(
        statement=statement, caller=caller(), device_id="device:test",
        source="agent_relay:claude-code", provenance=provenance or {}, supersedes_id=supersedes,
    )


def ledger(conn, conversation, *, disposition="answered", answer="Use pnpm"):
    ledger_id = new_id()
    conn.execute(
        "INSERT INTO resolution_ledger (id,question,normalized_question,answer,basis,"
        "caller_agent_id,caller_display_name,caller_purpose,caller_device_id,"
        "caller_transport,purpose,disposition,tier,confidence,policy_version,citations_json,"
        "withheld_json,request_json,resolution_json,device_id,monotonic_seq,resolved_at,"
        "conversation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ledger_id, "Which package manager?", "which package manager", answer,
         "user_confirmed", "claude-code", "Claude Code", "coding", "device:agent", "mcp",
         "coding", disposition, 1, 0.9, "v1", "[]", "[]", "{}", "{}", "device:test",
         conn.execute("SELECT COUNT(*) FROM resolution_ledger").fetchone()[0] + 1,
         datetime.now(UTC).isoformat(), conversation),
    )
    conn.commit()
    return ledger_id


def receipt(rule_id, status, *, verified=False):
    return {"rule_id": rule_id, "status": status, "independently_verified": verified,
            "method": "artifact_inspection" if verified else "agent_report",
            "evidence": f"checked with {KEY}"}


def checks(conn):
    return conn.execute(
        "SELECT status,attempt,independently_verified,evidence FROM rule_checks "
        "ORDER BY rowid").fetchall()


def test_only_changes_are_kept_with_attempts_and_redacted_evidence(conn):
    kept = rule(conn)
    for status in ("failed", "failed", "passed", "passed"):
        rule_signals.record_checks(conn, "task-1", [receipt(kept, status)], project="p")
    rule_signals.record_checks(conn, "task-1", [receipt("not-a-rule", "failed")])
    rows = checks(conn)
    assert [(r[0], r[1]) for r in rows] == [("failed", 1), ("passed", 2)]
    assert all(KEY not in r[3] for r in rows)
    # No task id, nothing to link: nothing is written.
    assert rule_signals.record_checks(conn, None, [receipt(kept, "failed")]) == []
    assert rule_signals.held(conn, kept) == {"passed": 1}


def test_finished_task_adopts_its_open_answers_once(conn):
    used = ledger(conn, "task-1")
    ledger(conn, "task-1", disposition="not_known", answer=None)
    ledger(conn, "task-2")
    first = rule_signals.adopt_finished(conn, "task-1", device_id="device:test")
    again = rule_signals.adopt_finished(conn, "task-1", device_id="device:test")
    assert len(first) == 1 and again == []
    assert [tuple(r) for r in conn.execute(
        "SELECT ledger_id,outcome,source FROM resolution_outcomes")] == [
        (used, "adopted", "task_finished")]


def test_owner_correction_marks_the_old_rule_and_keeps_the_turn_locally(conn):
    old = rule(conn)
    new = rule(conn, "Use bun, never npm", supersedes=old)
    rule_signals.record_checks(conn, "task-1", [receipt(old, "passed")])
    assert rule_signals.record_correction(conn, old, "task-1")
    assert rule_signals.record_turn(conn, new, method="agent_supplied",
                                    agent_turn=f"Ran npm install with {KEY}")
    assert not rule_signals.record_turn(conn, new, method="transcript", agent_turn="later")
    assert [(r[0], r[1]) for r in checks(conn)] == [("passed", 1), ("corrected", 2)]
    turn = conn.execute("SELECT method,agent_turn FROM correction_turns").fetchone()
    assert turn[0] == "agent_supplied" and KEY not in turn[1] and "npm install" in turn[1]


def entry(conn, cfg, external_id, *, after=None):
    entry_id, vault_ref = new_id(), ""
    if after is not None:
        vault_ref = f"ab/{entry_id}.json"
        vault = cfg.data_dir / "vault" / vault_ref
        vault.parent.mkdir(parents=True, exist_ok=True)
        vault.write_text(json.dumps({"after": after, "text": "no, ask first"}), encoding="utf-8")
    conn.execute(
        "INSERT INTO entries (id,kind,source,source_account,external_id,content_hash,body,"
        "occurred_start,tz,time_precision,captured_at,ingested_at,thread_id,sensitivity,"
        "vault_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, "message", "ai_conversations", "", external_id, entry_id, "owner message",
         "2026-10-01T00:00:00+00:00", "UTC", "exact", "2026-10-01T00:00:00+00:00",
         "2026-10-01T00:00:00+00:00", "ai:" + external_id.rsplit(":", 1)[0], "personal",
         vault_ref),
    )
    conn.commit()
    return entry_id


def mined(conn, statement, entry_id):
    return rule(conn, statement, provenance={"corrections": [
        {"agent_did": "did it", "wanted": "not that", "entry_id": entry_id}]})


def test_mined_corrections_find_the_agent_turn_before_them(conn, cfg, monkeypatch):
    session = "83744a2c-25f0-4b21-8cf3-c97e6b5456c7"
    said = entry(conn, cfg, f"claude_code:{session}:u2")
    # Two rules from one owner message both get the agent's whole reply.
    first = mined(conn, "Do not run the program during setup", said)
    second = mined(conn, "Install dependencies in a venv", said)
    # This chat's transcript is gone, but the archive kept the agent's message.
    kept = mined(conn, "Ask before deleting", entry(conn, cfg, "codex:gone:u9",
                                                    after="Deleted the old folder."))
    lost = mined(conn, "Ask first", new_id())
    turns = [Turn("user", "set it up", "u1", session, None),
             Turn("assistant", "Installing globally.", "a1", session, None),
             Turn("assistant", "Running python main.py now", "a2", session, None),
             Turn("user", "dont run it, use a venv", "u2", session, None)]
    adapter = SimpleNamespace(adapter_id="claude_code")
    path = cfg.data_dir / f"{session}.jsonl"
    from varyz import session_miner

    monkeypatch.setattr(session_miner, "_paths", lambda _cfg, _id: iter(
        [(adapter, path)] if _id == "claude_code" else []))
    monkeypatch.setattr(session_miner, "_turns", lambda _a, _p, _s: (
        SimpleNamespace(turn=t, reference=f"claude_code:{session}:{t.turn_id}") for t in turns))
    assert rule_signals.capture_turns(conn, cfg, threading.RLock()) == {"kept": 3,
                                                                        "not_found": 1}
    rows = {row[0]: tuple(row[1:]) for row in conn.execute(
        "SELECT principle_id,method,agent_turn,owner_turn FROM correction_turns")}
    reply = "Installing globally.\n\nRunning python main.py now"
    assert rows == {
        first: ("transcript", reply, "dont run it, use a venv"),
        second: ("transcript", reply, "dont run it, use a venv"),
        kept: ("archive", "Deleted the old folder.", "no, ask first"),
        lost: ("not_found", "", ""),
    }
    assert rule_signals.capture_turns(conn, cfg, threading.RLock()) == {"kept": 0,
                                                                        "not_found": 0}


def test_network_gets_linkable_clean_checks_and_correction_pairs(conn, cfg):
    analytics.consent(cfg, True)
    old = rule(conn, "Use npm in secret-launch")
    new = rule(conn, "Use pnpm, never npm", supersedes=old, provenance={"corrections": [
        {"agent_did": "Installed with npm in C:\\Users\\egor\\app", "wanted": "pnpm"}]})
    rule_signals.record_checks(conn, "task-1", [receipt(new, "failed")])
    rule_signals.record_checks(conn, "task-1", [receipt(new, "passed", verified=True)])
    rule_signals.record_checks(conn, "task-2", [receipt(new, "unchecked")])
    rule_signals.record_correction(conn, old, "task-1")
    ledger(conn, "task-1")
    rule_signals.adopt_finished(conn, "task-1", device_id="device:test")
    records = [json.loads(json.dumps(r)) for r in contribute.preview(cfg, limit=50)["records"]]
    by_kind: dict[str, list] = {}
    for record in records:
        by_kind.setdefault(record["kind"], []).append(record)
    revised = next(r for r in by_kind["rule"] if r["outcome"] == "revised")
    assert revised["agent_did"] == "Installed with npm in [path]" and revised["wanted"] == "pnpm"
    assert revised["replaces_ref"] and revised["label_source"] == "owner"
    checks_sent = sorted((r["outcome"], r["attempt"], r["label_source"])
                         for r in by_kind["check"])
    # Unchecked stays local; the corrected rule's check links to the rule it was replaced by.
    assert checks_sent == [("corrected", 1, "owner"), ("failed", 1, "agent_report"),
                           ("passed", 2, "inspected")]
    new_checks = {r["rule_ref"] for r in by_kind["check"] if r["outcome"] != "corrected"}
    assert new_checks == {revised["rule_ref"]}
    assert {r["rule_ref"] for r in by_kind["check"] if r["outcome"] == "corrected"} == {
        revised["replaces_ref"]}
    adopted = by_kind["correction"][0]
    assert adopted["label_source"] == "task_finished"
    assert adopted["task_ref"] == by_kind["check"][0]["task_ref"]
    text = json.dumps(records)
    assert "task-1" not in text and new not in text and "evidence" not in text
    # A new opt-in gets a new secret: references never link across opt-ins.
    with analytics.connection(cfg) as db:
        before = contribute._ref(db, new)
    with analytics.connection(cfg) as db:
        assert contribute._ref(db, new) == before
    analytics.consent(cfg, False)
    analytics.consent(cfg, True)
    with analytics.connection(cfg) as db:
        assert contribute._ref(db, new) != before


def test_the_tools_record_through_the_daemon(tmp_path):
    from varyz.mcp_server import VaryzServer
    from varyz.resolution.client import ResolutionClient
    from varyz.resolution.daemon import build_resolution_daemon
    from varyz.resolution.escalation import NullNotifier
    from varyz.trust import register_agent

    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
                      encoding="utf-8")
    daemon = build_resolution_daemon(tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
                                     device_id="device:test", notifier=NullNotifier())
    with daemon:
        client = ResolutionClient(*daemon.address)
        old = client.add_principle(statement="Use npm.", caller=caller(),
                                   device_id="device:test")
        setup = open_db(tmp_path / "data" / "varyz.db")
        token = register_agent(setup, agent_id="agent:claude-code:local",
                               display_name="Claude Code", device_id="local").token
        setup.close()
        server = VaryzServer(config, agent_token=token, resolution_client=client)
        try:
            done = server.call("finish_check", {"task": "Add a dependency",
                                                "conversation_id": "task-9"})
            fixed = server.call("remember_owner_rule", {
                "statement": "Use pnpm, never npm.", "replaces": old, "conversation_id": "task-9",
                "agent_did": "Installed the package with npm", "wanted": "pnpm instead",
                "corrected_output": f"$ npm install left-pad  # token {KEY}",
            })
        finally:
            server.close()
    daemon.stop()
    assert done["verification"]["receipts"] and fixed["recorded"] is True
    conn = open_db(tmp_path / "data" / "varyz.db")
    try:
        assert [tuple(r) for r in conn.execute(
            "SELECT principle_id,status,attempt FROM rule_checks ORDER BY rowid")] == [
            (old, "unchecked", 1), (old, "corrected", 2)]
        turn = conn.execute(
            "SELECT principle_id,method,agent_turn FROM correction_turns").fetchone()
        assert turn[0] == fixed["rule_id"] and turn[1] == "agent_supplied"
        assert "npm install left-pad" in turn[2] and KEY not in turn[2]
        provenance = json.loads(conn.execute(
            "SELECT provenance_json FROM owner_principles WHERE id=?", (fixed["rule_id"],)
        ).fetchone()[0])
        [pair] = provenance["corrections"]
        assert (pair["agent_did"], pair["wanted"]) == ("Installed the package with npm",
                                                       "pnpm instead")
        assert "corrected_output" not in json.dumps(provenance)
    finally:
        conn.close()


def test_the_new_signals_flow_through_the_tools(tmp_path):
    from varyz.mcp_server import VaryzServer
    from varyz.resolution.client import ResolutionClient
    from varyz.resolution.daemon import build_resolution_daemon
    from varyz.resolution.escalation import NullNotifier
    from varyz.trust import register_agent

    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n'
                      "[signals]\nmemory_test = true\nmemory_test_rate = 0.1\n",
                      encoding="utf-8")
    daemon = build_resolution_daemon(tmp_path / "data" / "varyz.db", host="127.0.0.1", port=0,
                                     device_id="device:test", notifier=NullNotifier())
    # Few enough that finish_check checks every one.
    statements = [f"In Python code, name helper {n} in snake case" for n in range(5)]
    with daemon:
        client = ResolutionClient(*daemon.address)
        ids = {s: client.add_principle(statement=s, caller=caller(), device_id="device:test")
               for s in statements}
        setup = open_db(tmp_path / "data" / "varyz.db")
        token = register_agent(setup, agent_id="agent:claude-code:local",
                               display_name="Claude Code", device_id="local").token
        setup.close()
        server = VaryzServer(config, agent_token=token, resolution_client=client)
        try:
            task = "Refactor the Python helpers"
            # A task where the memory test holds at least one rule back.
            conv = next(f"task-{n}" for n in range(500) if any(
                rule_signals.arm(i, f"task-{n}", 0.1) == "held_out" for i in ids.values()))
            brief = json.dumps(server.call("start_task", {"task": task, "conversation_id": conv}))
            held = {s for s, i in ids.items() if rule_signals.arm(i, conv, 0.1) == "held_out"}
            assert held and not any(s in brief for s in held)
            check = {"task": task, "conversation_id": conv, "model": "claude-opus-5-5"}
            receipts = server.call("finish_check", check)["verification"]["receipts"]
            assert {r["rule_id"] for r in receipts} >= {ids[s] for s in held}  # Revealed.
            server.call("finish_check", {**check, "evidence": [
                {"rule_id": r["rule_id"], "status": "passed", "evidence": "ok"}
                for r in receipts]})
            asked = server.call("ask", {"question": "Should errors print a hint line?",
                                        "decision_kind": "preference", "model": "gpt-6.1-sol",
                                        "proposed_choice": "Yes, one short hint",
                                        "reason": "Changes the output",
                                        "conversation_id": conv})
            escalation = asked["ask_owner"]["escalation_id"]
            assert server.call("answer_owner_question", {
                "escalation_id": escalation, "answer": "Yes, a short hint",
                "ask_feedback": "unneeded"})["recorded"]
            fixed = server.call("remember_owner_rule", {
                "statement": "In Python code, name helpers in kebab case",
                "replaces": ids[statements[0]], "conversation_id": conv,
                "should_have_asked": True})
            assert fixed["recorded"]
        finally:
            server.close()
    daemon.stop()
    conn = open_db(tmp_path / "data" / "varyz.db")
    try:
        assert {r[0] for r in conn.execute("SELECT DISTINCT model FROM rule_checks")} == {
            "claude-opus-5-5"}
        arms = dict(conn.execute("SELECT principle_id,arm FROM rule_exposures").fetchall())
        assert {i for i, a in arms.items() if a == "held_out"} == {ids[s] for s in held}
        assert tuple(conn.execute(
            "SELECT decision_kind,model,agent_guess FROM answer_predictions").fetchone()) == (
            "preference", "gpt-6.1-sol", "Yes, one short hint")
        assert {tuple(r) for r in conn.execute(
            "SELECT label, escalation_id IS NULL FROM ask_feedback")} == {
            ("unneeded", 0), ("should_have_asked", 1)}
        assert rule_signals.summary(conn)["predictions"]["agent"] == {"scored": 1,
                                                                      "hit_rate": 1.0}
    finally:
        conn.close()


def test_models_are_named_from_an_allowlist_only():
    assert rule_signals.model_name(" Claude-Opus-5-5 ") == "claude-opus-5-5"
    assert rule_signals.model_name("gpt-6.1-sol") == "gpt-6.1-sol"
    for leak in ("my-secret-project", "claude opus", "egor@varyzai.com", "x" * 80, None, 5):
        assert rule_signals.model_name(leak) == ""


def test_checks_show_false_done_claims_and_first_tries_per_model(conn):
    kept, broken = rule(conn), rule(conn, "Write tests first")
    rule_signals.record_checks(conn, "task-1", [receipt(kept, "passed"), receipt(broken, "failed")],
                               agent="claude-code", model="claude-opus-5-5")
    rule_signals.record_checks(conn, "task-2", [receipt(kept, "failed")],
                               agent="codex", model="gpt-6.1-sol")
    rule_signals.record_checks(conn, "task-3", [receipt(kept, "passed")],
                               agent="codex", model="not a model")
    # The owner corrected a rule the agent had reported as passed: a false "done".
    rule_signals.record_correction(conn, kept, "task-1")
    rule_signals.record_correction(conn, kept, "task-2")
    assert {tuple(r) for r in conn.execute(
        "SELECT conversation_id,agent,model FROM rule_checks WHERE attempt=1")} == {
        ("task-1", "claude-code", "claude-opus-5-5"), ("task-2", "codex", "gpt-6.1-sol"),
        ("task-3", "codex", "")}
    checks_seen = rule_signals.summary(conn)["checks"]
    assert checks_seen["false_done_claims"] == 1 and checks_seen["owner_corrections"] == 2
    assert checks_seen["first_try_by_model"] == {
        "claude-opus-5-5": {"passed": 1, "failed": 1, "pass_rate": 0.5},
        "gpt-6.1-sol": {"passed": 0, "failed": 1, "pass_rate": 0.0},
        "codex": {"passed": 1, "failed": 0, "pass_rate": 1.0},
    }


def test_the_memory_test_is_off_by_default_and_never_hides_high_stakes_rules(conn, cfg):
    safe = [rule(conn, f"Name helper number {n} in snake case") for n in range(40)]
    risky = [rule(conn, f"Ask before deleting folder {n}") for n in range(40)]
    store = SQLitePrincipleStore(conn)
    principles = [store.get(pid) for pid in safe + risky]
    assert rule_signals.held_back(principles, "task-1", cfg) == set()
    cfg.signals.memory_test, cfg.signals.memory_test_rate = True, 0.1
    assert rule_signals.held_back(principles, None, cfg) == set()
    hidden = set().union(*(rule_signals.held_back(principles, f"task-{n}", cfg)
                           for n in range(20)))
    assert hidden and hidden <= set(safe)
    # The same task always draws the same arm.
    assert rule_signals.held_back(principles, "task-3", cfg) == rule_signals.held_back(
        principles, "task-3", cfg)
    with pytest.raises(ValueError):
        type(cfg.signals)(memory_test_rate=0.5)


def test_memory_test_arms_are_kept_beside_the_first_result(conn):
    rules = [rule(conn, f"Name helper number {n} in snake case") for n in range(30)]
    rule_signals.record_checks(conn, "task-1", [receipt(r, "passed") for r in rules],
                               memory_test=0.5)
    rule_signals.record_checks(conn, "task-1", [receipt(rules[0], "failed")], memory_test=0.5)
    arms = dict(conn.execute("SELECT principle_id,arm FROM rule_exposures").fetchall())
    assert set(arms) == set(rules) and set(arms.values()) == {"shown", "held_out"}
    assert all(arms[r] == rule_signals.arm(r, "task-1", 0.5) for r in rules)
    tested = rule_signals.summary(conn)["memory_test"]
    assert sum(t["passed"] for t in tested.values()) == 30
    assert sum(t["failed"] for t in tested.values()) == 0  # The first try is what counts.


def queue(conn):
    from varyz.resolution.escalation import SQLiteEscalationQueue
    from varyz.resolution.knowledge import SQLiteKnowledgeStore

    return SQLiteEscalationQueue(conn, SQLiteKnowledgeStore(conn), device_id="device:test")


def question(asked, text, conversation="task-1"):
    from varyz.resolution.models import ResolutionRequest

    return asked.enqueue(ResolutionRequest(question=text, caller=caller(), purpose="coding",
                                           conversation_id=conversation, project="p"),
                         tried_tiers=(1, 2), urgency="normal")


def test_answers_are_guessed_before_the_owner_replies_and_scored_after(conn):
    asked = queue(conn)
    earlier = question(asked, "Should the release notes use British spelling?")
    asked.answer(earlier, "Yes, British spelling", device_id="device:test", scope="p")
    later = question(asked, "Should the changelog use British spelling?")
    assert rule_signals.record_prediction(conn, later, decision_kind="preference",
                                          agent="claude-code", model="claude-opus-5-5",
                                          agent_guess="No, American spelling")
    assert not rule_signals.record_prediction(conn, later, agent_guess="second guess")
    asked.answer(later, "yes british spelling please", device_id="device:test", scope="p")
    assert not rule_signals.record_prediction(conn, earlier, agent_guess="too late")
    row = conn.execute("SELECT agent_guess,memory_guess,memory_source FROM answer_predictions"
                       ).fetchone()
    assert tuple(row) == ("No, American spelling", "Yes, British spelling", earlier)
    assert rule_signals.summary(conn)["predictions"] == {
        "agent": {"scored": 1, "hit_rate": 0.0}, "memory": {"scored": 1, "hit_rate": 1.0}}
    assert rule_signals.answers_match("Keep the old name", "keep the old name for now")
    assert not rule_signals.answers_match("yes, ship it", "no")
    assert not rule_signals.answers_match("Use pnpm", "Use bun")


def test_questions_are_labelled_needed_unneeded_or_dropped(conn):
    asked = queue(conn)
    needed = question(asked, "Which colour for the logo?")
    handed_back = question(asked, "Tabs or spaces in the new file?")
    told = question(asked, "Should the button say Save?")
    dropped = question(asked, "Rename the folder?")
    asked.answer(needed, "Teal", device_id="device:test", scope="p")
    asked.answer(handed_back, "Up to you!", device_id="device:test", scope="p")
    # Strict on purpose: a hand-back with a preference attached still needed asking.
    assert rule_signals.ask_label("answered", "Up to you, but I like tabs") == "needed"
    asked.answer(told, "Yes", device_id="device:test", scope="p")
    assert rule_signals.record_ask_feedback(conn, "unneeded", escalation_id=told)
    assert not rule_signals.record_ask_feedback(conn, "needed", escalation_id=told)
    assert not rule_signals.record_ask_feedback(conn, "bogus", escalation_id=needed)
    asked.dismiss(dropped)
    missed = rule(conn, "Ask before renaming public functions")
    assert rule_signals.record_ask_feedback(conn, "should_have_asked", principle_id=missed,
                                            conversation_id="task-1")
    assert rule_signals.summary(conn)["asks"] == {
        "needed": 1, "unneeded": 2, "dropped": 1, "should_have_asked": 1}


def test_rule_chains_show_how_long_each_preference_lasted(conn):
    made: list[str] = []
    for statement, day in (("Use npm", "2026-01-01"), ("Use pnpm", "2026-03-02"),
                           ("Use bun", "2026-03-12")):
        store = SQLitePrincipleStore(conn, clock=lambda d=day: datetime.fromisoformat(
            d + "T00:00:00+00:00"))
        made.append(store.add(statement=statement, caller=caller(), device_id="device:test",
                              source="agent_relay:claude-code",
                              supersedes_id=made[-1] if made else None))
    first, second, third = made
    assert rule_signals.lineage(conn, first) == {"chain_depth": 0, "replaced_after_days": 0}
    assert rule_signals.lineage(conn, second) == {"chain_depth": 1, "replaced_after_days": 60}
    assert rule_signals.lineage(conn, third) == {"chain_depth": 2, "replaced_after_days": 10}
    assert rule_signals.summary(conn)["preference_change"] == {
        "revisions": 2, "median_days_a_rule_lasted": 60}


def test_network_gets_the_new_signals_as_closed_labels(conn, cfg):
    analytics.consent(cfg, True)
    old = rule(conn, "Name helpers in camel case")
    new = rule(conn, "Name helpers in snake case", supersedes=old)
    rule_signals.record_checks(conn, "task-1", [receipt(old, "passed")], agent="claude-code",
                               model="claude-opus-5-5", memory_test=0.05)
    rule_signals.record_correction(conn, old, "task-1")
    rule_signals.record_ask_feedback(conn, "should_have_asked", principle_id=new)
    asked = queue(conn)
    earlier = question(asked, "Should the release notes use British spelling?")
    asked.answer(earlier, "Yes, British spelling", device_id="device:test", scope="p")
    later = question(asked, "Should the changelog use British spelling?")
    rule_signals.record_prediction(conn, later, model="gpt-6.1-sol",
                                   agent_guess="Yes, British spelling")
    asked.answer(later, "yes british spelling please", device_id="device:test", scope="p")
    open_one = question(asked, "Which font for the docs?")
    rule_signals.record_prediction(conn, open_one, agent_guess="Marcellus")
    records = [json.loads(json.dumps(r)) for r in contribute.preview(cfg, limit=50)["records"]]
    corrected = next(r for r in records if r["kind"] == "check" and r["outcome"] == "corrected")
    assert (corrected["previous"], corrected["model"]) == ("passed", "claude-opus-5-5")
    assert corrected["arm"] == rule_signals.arm(old, "task-1", 0.05)
    revised = next(r for r in records if r["kind"] == "rule" and r["outcome"] == "revised")
    assert (revised["chain_depth"], revised["ask_label"]) == (1, "should_have_asked")
    asks = {r["question"]: r for r in records if r["kind"] == "escalation"}
    # Still open: nothing leaves until the owner closes it.
    assert set(asks) == {"Should the release notes use British spelling?",
                         "Should the changelog use British spelling?"}
    scored = asks["Should the changelog use British spelling?"]
    assert (scored["agent_guess_hit"], scored["memory_guess_hit"], scored["model"],
            scored["ask_label"]) == ("hit", "hit", "gpt-6.1-sol", "needed")
    assert scored["task_ref"] and scored["task_ref"] == corrected["task_ref"]
    asked.dismiss(open_one)
    # A guess is the agent's text: it is scrubbed like everything else, and a guess
    # at a choice the owner handed back is not scored.
    handed = question(asked, "Which font for the changelog?")
    rule_signals.record_prediction(conn, handed, agent_guess=f"Marcellus, key {KEY}")
    secret = question(asked, "Which font for the slides?")
    rule_signals.record_prediction(conn, secret, agent_guess="Marcellus, from C:\\Users\\egor")
    asked.answer(handed, "Up to you", device_id="device:test", scope="p")
    asked.answer(secret, "Marcellus", device_id="device:test", scope="p")
    later = {r["question"]: r for r in contribute.preview(cfg, limit=50)["records"]
             if r["kind"] == "escalation"}
    assert (later["Which font for the docs?"]["outcome"],
            later["Which font for the docs?"]["ask_label"]) == ("dismissed", "dropped")
    changelog = later["Which font for the changelog?"]
    assert KEY not in changelog["agent_guess"] and changelog["agent_guess_hit"] == ""
    assert changelog["ask_label"] == "unneeded"
    slides = later["Which font for the slides?"]
    assert slides["agent_guess"] == "Marcellus, from [path]"
    assert slides["agent_guess_hit"] == "hit"
    assert rule_signals.guess_hit("Teal", "Up to you") == ""
