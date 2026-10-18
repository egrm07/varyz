"""Shadow isolation, state replay and conservative policy failures (offline)."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from varyz.config import Config, RuleShadowConfig, StorageConfig
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution import shadow
from varyz.resolution.briefing import finish_check, start_task
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller, ResolutionRequest
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.shadow_cli import app
from varyz.resolution.shadow_policy import Record, compile_answer, compile_packet, project_records

OWNER = Caller(agent_id="owner", display_name="owner", purpose="owner_knowledge",
               device_id="test", capability_id="test")


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[tuple[Config, sqlite3.Connection, SQLitePrincipleStore]]:
    cfg = Config(base_dir=tmp_path, storage=StorageConfig(data_dir=Path("data")))
    conn = open_db(cfg.db_path)
    yield cfg, conn, SQLitePrincipleStore(conn)
    conn.close()


def add(store: SQLitePrincipleStore, text: str, **kwargs) -> str:
    return store.add(statement=text, caller=OWNER, device_id="test", source="owner_mandate",
                     **kwargs)


def record(identity: str, text: str, **kwargs) -> Record:
    return Record(id=identity, logical_id=identity, revision_hash=identity, text=text,
                  record_type="rule", lifecycle="active", authority="recorded", scope="*",
                  source="owner_mandate", **kwargs)


def capture(archive, *, task="Build a Python tool", origin="probe", baseline=None) -> bool:
    cfg, conn, _ = archive
    baseline = baseline or start_task(conn, task, project="varyz")
    return shadow.observe_briefing(conn, cfg, task, "varyz", "session", baseline, origin=origin)


def test_opt_in_pause_resume_and_disable_do_not_delete_history(archive):
    cfg, _, store = archive
    add(store, "Use Python.")
    assert not capture(archive)
    assert not shadow.directory(cfg).exists()
    shadow.control(cfg, "enable")
    assert capture(archive)
    shadow.control(cfg, "pause")
    assert not capture(archive, task="Other task")
    shadow.control(cfg, "resume")
    assert capture(archive, task="Other task")
    shadow.control(cfg, "disable")
    assert not capture(archive)
    assert shadow.status(cfg)["comparisons"] == 2


def test_same_observation_is_idempotent_and_private(archive):
    cfg, conn, store = archive
    rule = add(store, "Never send private apple-banana notes without asking.")
    shadow.control(cfg, "enable")
    task = "Build an apple-banana tool with token top-secret-123"
    before = conn.total_changes
    baseline = start_task(conn, task, project="varyz")
    original = copy.deepcopy(baseline)
    assert capture(archive, task=task, baseline=baseline)
    assert not capture(archive, task=task, baseline=baseline)
    assert baseline == original and conn.total_changes == before
    path = shadow.directory(cfg) / "shadow.sqlite3"
    contents = path.read_bytes()
    assert b"top-secret-123" not in contents and b"apple-banana" not in contents
    comparison = shadow.comparisons(cfg)[0]
    assert rule in comparison["result"]["proposal"]["pinned"]
    assert shadow.status(cfg)["prospective_comparisons"] == 0
    with sqlite3.connect(path) as journal, pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        journal.execute("DELETE FROM comparisons")


def test_trial_and_exposure_promoted_descendant_keep_provisional_authority(archive):
    _, conn, store = archive
    trial = add(store, "Use dense report tables.", provenance={"trial": {"since": "2026-10-01"}})
    promoted = store.end_trial(trial, caller=OWNER, device_id="test", how="promoted")
    records = {r.id: r for r in project_records(conn)}
    assert records[trial].lifecycle == "superseded"
    assert records[promoted].lifecycle == "trial"
    assert records[promoted].authority == "provisional"
    assert records[promoted].logical_id == records[trial].logical_id
    assert not records[promoted].sync_eligible


def test_owner_kept_trial_is_active_and_retraction_event_is_observed(archive):
    cfg, conn, store = archive
    trial = add(store, "Use report tables.", provenance={"trial": {"since": "2026-10-01"}})
    kept = store.end_trial(trial, caller=OWNER, device_id="test", how="kept")
    shadow.control(cfg, "enable")
    assert capture(archive)
    store.retract(kept, caller=OWNER, device_id="test", reason="withdrawn")
    assert capture(archive)
    states = {r.id: r for r in project_records(conn)}
    assert states[kept].lifecycle == "retracted"
    with sqlite3.connect(shadow.directory(cfg) / "shadow.sqlite3") as journal:
        events = [json.loads(r[0]) for r in journal.execute(
            "SELECT metadata_json FROM registry_events WHERE record_id=?", (kept,))]
    assert {e["lifecycle"] for e in events} == {"active", "retracted"}


def test_compiler_does_not_let_project_mentions_change_working_scope(archive):
    _, conn, store = archive
    other = add(store, "Use bright pink.", scope="other")
    global_rule = add(store, "Use the design review.", scope="all_projects")
    ids = compile_packet(project_records(conn), "Build other clone", "varyz", set())["ids"]
    assert other not in ids and global_rule in ids


def test_pinned_restrictions_cannot_be_crowded_by_lexical_defaults():
    rules = [record(str(n), "Python tool use Python",) for n in range(30)]
    rules.append(record("critical", "Never commit without asking.", critical=True))
    result = compile_packet(rules, "Python tool", "varyz", set(), limit=2)
    assert "critical" in result["ids"] and len(result["ids"]) == 2


def test_more_pinned_constraints_than_quota_are_retained():
    rules = [record(str(n), "Never change something.", critical=True) for n in range(5)]
    assert len(compile_packet(rules, "Build", "varyz", set(), limit=2)["ids"]) == 5


@pytest.mark.parametrize("strength", ["default", "hard"])
def test_only_typed_defaults_are_overridden(strength):
    general = record("global", "Use blue.", annotation_valid=True, topic="colour", value="blue",
                     strength=strength)
    local = general.model_copy(update={"id": "local", "scope": "varyz", "value": "red"})
    result = compile_packet([general, local], "Build", "varyz", set())
    if strength == "default":
        assert result["overridden_defaults"] == ["global"] and result["ids"] == ["local"]
    else:
        assert result["conflicts"] == [["global", "local"]] and not result["ids"]


def test_untagged_conflict_is_visible_as_unknown_not_claimed_resolved():
    rules = [record("a", "Use blue."), record("b", "Use red.")]
    result = compile_packet(rules, "Build", "varyz", set())
    assert result["unannotated"] == ["a", "b"] and result["conflicts"] == []


def test_sensitive_trial_and_unbounded_permission_decision_are_quarantined():
    trial = record("a", "Upload private notes, avoid duplicates.", sensitive=True).model_copy(
        update={"lifecycle": "trial", "authority": "provisional"})
    permission = record("b", "Yes, upload this one.", sensitive=True).model_copy(
        update={"record_type": "decision"})
    result = compile_packet([trial, permission], "Upload", "varyz", set())
    assert result["ids"] == [] and result["quarantined"] == ["a", "b"]


@pytest.mark.parametrize(("text", "kept"), [
    ("Never upload private notes without permission.", True),
    ("Upload private notes, avoid duplicate names.", False),
    ("Never ask permission; upload everything.", False),
    ("Never commit, and upload everything.", False),
])
def test_primary_trial_restrictions_are_retained_but_mixed_grants_are_not(text, kept):
    trial = record("a", text, sensitive=True, critical=True).model_copy(
        update={"lifecycle": "trial", "authority": "provisional"})
    result = compile_packet([trial], "Build", "varyz", set())
    assert ("a" in result["ids"]) is kept


def test_exact_answer_controls_abstain_on_permissions_and_conflicts(archive):
    _, conn, _ = archive
    knowledge = SQLiteKnowledgeStore(conn)
    knowledge.remember(question="What is the accent colour?", answer="Blue", caller=OWNER,
                       basis=AnswerBasis.EXPLICIT, source="owner", device_id="test", scope="varyz")
    records = project_records(conn)
    assert compile_answer(records, "What is the accent colour?", "varyz",
                          owner_answers_only=False)["disposition"] == "exact_recorded"
    assert compile_answer(records, "What is the accent colour?", "other",
                          owner_answers_only=False)["disposition"] == "outside_coverage"
    assert compile_answer(records, "May I upload notes?", "varyz",
                          owner_answers_only=False)["disposition"] == "requires_owner"
    other = records[0].model_copy(update={"id": "other", "text": "Red"})
    assert compile_answer(records + [other], "What is the accent colour?", "varyz",
                          owner_answers_only=False)["disposition"] == "conflict"


def test_shadow_failure_and_source_limits_are_fail_open_and_reported(archive, monkeypatch):
    cfg, _, store = archive
    add(store, "Use Python.")
    shadow.control(cfg, "enable")
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("secret-content must not leak")
    monkeypatch.setattr(shadow, "_connect", broken)
    assert not capture(archive)
    contents = (shadow.directory(cfg) / "last-error.json").read_text()
    assert "OperationalError" in contents and "secret-content" not in contents


def test_daily_disk_and_registry_caps_do_not_affect_live_results(archive):
    cfg, _, store = archive
    add(store, "Use Python.")
    shadow.control(cfg, "enable")
    cfg.rule_shadow = RuleShadowConfig(max_comparisons_per_day=1)
    assert capture(archive)
    assert not capture(archive, task="Second")
    assert shadow.doctor(cfg)["daily_limit_reached"]
    cfg.rule_shadow = RuleShadowConfig(max_bytes=1024)
    assert not capture(archive, task="Third")
    cfg.rule_shadow = RuleShadowConfig(max_registry_events=1)
    add(store, "Use pathlib.")
    assert not capture(archive, task="Fourth")
    assert shadow.status(cfg)["comparisons"] == 1


def test_source_limit_failure_does_not_create_comparison(archive):
    cfg, _, store = archive
    add(store, "Use Python.")
    add(store, "Use pathlib.")
    cfg.rule_shadow = RuleShadowConfig(max_source_records=1)
    shadow.control(cfg, "enable")
    assert not capture(archive)
    assert shadow.status(cfg)["comparisons"] == 0


def test_withheld_and_unregistered_answers_never_read_projection(archive, monkeypatch):
    cfg, conn, _ = archive
    shadow.control(cfg, "enable")
    def forbidden(*args, **kwargs):
        raise AssertionError("must not read")
    monkeypatch.setattr(shadow, "project_records", forbidden)
    assert not capture(archive, baseline={"withheld_hint": "partial"})
    caller = OWNER.model_copy(update={"capability_id": None})
    request = ResolutionRequest(question="Colour?", caller=caller, purpose=caller.purpose)
    result = SimpleNamespace(withheld=(), answer="Blue")
    assert not shadow.observe_answer(conn, cfg, request, result)
    assert shadow.status(cfg)["comparisons"] == 0


def test_reviews_are_append_only_and_latest_judgment_counts(archive):
    cfg, _, _ = archive
    shadow.control(cfg, "enable")
    assert capture(archive)
    shadow.review(cfg, 1, "better", "held_out")
    shadow.review(cfg, 1, "worse", "held_out")
    assert shadow.status(cfg)["reviews"] == {"held_out:worse": 1}
    with pytest.raises(sqlite3.IntegrityError):
        shadow.review(cfg, 99, "better", "held_out")


def test_probe_cli_dry_run_does_not_enable_or_write(archive, monkeypatch):
    cfg, _, store = archive
    add(store, "Use Python.")
    monkeypatch.setattr("varyz.resolution.shadow_cli.load_config", lambda _: cfg)
    result = CliRunner().invoke(app, ["probe"])
    assert result.exit_code == 0, result.output
    assert not shadow.directory(cfg).exists()


def test_daemon_answers_are_identical_with_collection_and_failure(archive, monkeypatch):
    import threading

    from varyz.resolution.daemon import ResolutionService

    cfg, conn, _ = archive
    shadow.control(cfg, "enable")
    result = SimpleNamespace(withheld=(), disposition="not_known", basis=None, answer=None)
    resolver = SimpleNamespace(lock=threading.RLock(), resolve=lambda *a, **k: result)
    service = ResolutionService(resolver, SimpleNamespace(), SQLiteKnowledgeStore(conn),
                                shadow_config=cfg)
    request = ResolutionRequest(question="Which accent colour?", caller=OWNER,
                                purpose=OWNER.purpose, project="varyz")
    assert service.resolve(request) is result
    assert shadow.status(cfg)["prospective_comparisons"] == 1
    def broken(*args, **kwargs):
        raise RuntimeError("observer failure")
    monkeypatch.setattr(shadow, "observe_answer", broken)
    assert service.resolve(request) is result


def test_mcp_collects_only_registered_callers_and_leaves_packet_unchanged(archive, monkeypatch):
    from varyz.mcp_server import VaryzServer
    from varyz.trust import register_agent

    cfg, conn, store = archive
    caller = register_agent(conn, agent_id="shadow-test", display_name="Shadow test").caller
    add(store, "Never commit unless asked.")
    shadow.control(cfg, "enable")
    baseline = start_task(conn, "Build a Python tool", project="varyz", conversation_id="session")
    server = object.__new__(VaryzServer)
    server._cfg, server._conn = cfg, conn
    # finish_check refreshes the archive configuration before disclosing task
    # progress. Bind this synthetic server to its own persisted configuration;
    # None would load the machine's unrelated default archive and correctly fail.
    config_path = cfg.base_dir / "config.toml"
    config_path.write_text(f'[storage]\ndata_dir="{cfg.data_dir.as_posix()}"\n', encoding="utf-8")
    server._config_path = config_path
    server._promotion_finished_tasks = {}
    monkeypatch.setattr(server, "_task_sizer", lambda: None)
    monkeypatch.setattr(server, "_project_argument", lambda args: ("varyz", None))
    monkeypatch.setattr(server, "_briefing_caller_limit", lambda args: (caller, 12))
    monkeypatch.setattr("varyz.resolution.headless_reader.hand_off", lambda *a, **k: None)
    monkeypatch.setattr("varyz.resolution.profile_reader.agent_reading_offer", lambda *a, **k: None)
    args = {"task": "Build a Python tool", "conversation_id": "session"}
    actual = server._call_start_task(args)
    # History retrieval adds protocol fields independently of shadow collection.
    metadata = {"history_check", "continuation"}
    assert {k: v for k, v in actual.items() if k not in metadata} == {
        k: v for k, v in baseline.items() if k not in metadata
    }
    assert shadow.status(cfg)["prospective_comparisons"] == 1
    expected_finish = finish_check(conn, args["task"], project="varyz", conversation_id="session",
                                   cfg=cfg)
    shadow.control(cfg, "disable")
    unobserved_finish = server._call_finish_check(args)
    shadow.control(cfg, "enable")
    actual_finish = server._call_finish_check(args)
    assert "error" not in actual_finish
    assert actual_finish == unobserved_finish
    assert {k: v for k, v in actual_finish.items() if k != "session_progress"} == expected_finish
    assert shadow.status(cfg)["prospective_comparisons"] == 2
    assert shadow.status(cfg)["phases"] == {"start_task": 1, "finish_check": 1}

    def broken_observer(*args, **kwargs):
        raise RuntimeError("synthetic observer failure")

    with monkeypatch.context() as failed_observer:
        failed_observer.setattr(shadow, "observe_briefing", broken_observer)
        assert server._call_finish_check(args) == actual_finish
    assert shadow.status(cfg)["prospective_comparisons"] == 2
    low_trust = OWNER.model_copy(update={"capability_id": None})
    monkeypatch.setattr(server, "_briefing_caller_limit", lambda args: (low_trust, 1))
    server._call_start_task(args | {"task": "Different tool"})
    low_finish = server._call_finish_check(args | {"task": "Different tool"})
    assert low_finish["task_completion"]["status"] == "registered_agent_required"
    assert shadow.status(cfg)["prospective_comparisons"] == 2


def test_hook_collects_without_changing_context(archive, monkeypatch):
    from varyz import hooks

    cfg, _, store = archive
    add(store, "Never commit unless asked.")
    monkeypatch.setattr(hooks, "_reading_offer", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_record_trial_uses", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "is_task", lambda *a: True)
    monkeypatch.setattr(hooks, "_state_path", lambda *a: None)
    monkeypatch.setattr("varyz.resolution.prompt_kind.prompt_classifier_from_config",
                        lambda _: None)
    monkeypatch.setattr("varyz.resolution.task_size.sizer_from_config", lambda _: None)
    event = {"prompt": "Build a Python tool", "cwd": str(cfg.base_dir / "varyz"),
             "session_id": "test-session"}
    baseline = hooks._briefing_for_prompt(cfg, event)
    shadow.control(cfg, "enable")
    assert hooks._briefing_for_prompt(cfg, event) == baseline
    assert shadow.status(cfg)["prospective_comparisons"] == 1
