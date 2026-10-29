"""Away mode continues useful work without inventing preferences or expanding authority."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from varyz import hooks, install
from varyz.cli import app
from varyz.config import load_config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity, utc_now
from varyz.mcp_server import VaryzServer
from varyz.personal_model.query import AnswerBasis, QueryDisposition
from varyz.resolution.away import review, set_mode, status
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.models import Resolution, ResolutionRequest
from varyz.trust import register_agent


class Notifications:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def notify(self, escalation_id: str, urgency: str) -> None:
        self.ids.append(escalation_id)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
        '[workflows]\nparaphrase_verifier = "off"\n', encoding="utf-8",
    )
    return path


@contextmanager
def serving(path: Path, **daemon_options) -> Iterator[tuple[VaryzServer, Notifications]]:
    notices = Notifications()
    daemon = build_resolution_daemon(
        load_config(path).db_path, host="127.0.0.1", port=0,
        device_id="device:test", notifier=notices, **daemon_options,
    )
    with daemon:
        host, port = daemon.address
        server = VaryzServer(path, resolution_client=ResolutionClient(host, port))
        credential = register_agent(server.conn, agent_id="agent:test", display_name="Test")
        server._agent_token = credential.token
        server._task_sizer = lambda *args, **kwargs: None
        try:
            yield server, notices
        finally:
            server.close()


@pytest.fixture
def agent(config: Path, monkeypatch) -> Iterator[tuple[VaryzServer, Notifications]]:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with serving(config) as served:
        yield served


def activate(server: VaryzServer, **extra) -> dict:
    return server.call("away", {
        "action": "on", "project": "lumen", "conversation_id": "task-1",
        "instruction": "Keep working while I'm away; I'll review later.", **extra,
    })


def ask(server: VaryzServer, question: str, **extra) -> dict:
    return server.call("ask", {
        "question": question, "project": "lumen", "conversation_id": "task-1", **extra,
    })


def test_deferred_choice_survives_return_without_becoming_owner_knowledge(agent) -> None:
    server, notices = agent
    activate(server)
    first = ask(server, "Which accent colour should Lumen use?", proposed_choice="navy",
                reason="Uses a replaceable CSS variable.")
    assert "ask_owner" not in first
    assert first["deferred_owner"]["proposed_choice"] == "navy"
    assert not notices.ids
    assert not server.conn.execute("SELECT * FROM owner_knowledge").fetchall()
    repeated = ask(server, "Which accent colour should Lumen use?")
    assert repeated["escalation_id"] == first["escalation_id"]
    assert repeated["deferred_owner"]["proposed_choice"] == "navy"
    returned = server.call("away", {
        "action": "off", "project": "lumen", "conversation_id": "task-1",
        "instruction": "I'm back",
    })
    assert not returned["away_mode"]["active"]
    assert returned["review"]["pending"] == 1
    assert returned["review"]["questions"][0]["proposed_choice"] == "navy"
    server.call("answer_owner_question", {
        "escalation_id": first["escalation_id"], "answer": "Teal", "scope": "project",
    })
    assert ask(server, "Which accent colour should Lumen use?")["answer"] == "Teal"
    assert review(server.conn, "lumen", "task-1")["pending"] == 0


def test_choices_asked_together_while_away_are_queued_not_put_to_the_owner(agent) -> None:
    server, notices = agent
    activate(server)
    batch = server.call("ask", {"project": "lumen", "conversation_id": "task-1", "questions": [
        {"question": "Which accent colour should Lumen use?", "proposed_choice": "navy",
         "reason": "Uses a replaceable CSS variable."},
        {"question": "May I publish the Lumen page?", "decision_kind": "permission"},
        {"question": "Which test runner should Lumen use?", "decision_kind": "delegated"},
    ]})
    assert "ask_owner_batch" not in batch
    assert batch["away_mode"]["active"]
    deferred = batch["deferred_owner"]
    assert [(item["kind"], item["proposed_choice"]) for item in deferred["questions"]] == [
        ("preference", "navy"), ("permission", "")]
    assert "away_mode.active" in deferred["how"]
    assert "agent_decides" in batch["results"][2]
    assert not notices.ids
    assert not server.conn.execute("SELECT * FROM owner_knowledge").fetchall()
    assert review(server.conn, "lumen", "task-1")["pending"] == 2


@pytest.mark.parametrize("away", [False, True])
def test_delegated_engineering_checks_constraints_without_a_new_question(agent, away) -> None:
    server, notices = agent
    if away:
        activate(server)
    result = ask(server, "Which programming language for this tool?", decision_kind="delegated",
                 allow_escalation=True)
    assert result["answer"] is None
    assert result["agent_decides"]
    assert "deferred_owner" not in result
    assert not server.conn.execute("SELECT * FROM resolution_escalations").fetchall()
    assert not notices.ids
    assert not server.conn.execute("SELECT * FROM owner_knowledge").fetchall()


@pytest.mark.parametrize("away", [False, True])
def test_known_owner_constraint_still_wins_over_delegation(agent, away) -> None:
    server, _ = agent
    first = ask(server, "Which programming language for this tool?")
    server.call("answer_owner_question", {
        "escalation_id": first["escalation_id"], "answer": "Python", "scope": "project",
    })
    if away:
        activate(server)
    result = ask(server, "Which programming language for this tool?", decision_kind="delegated")
    assert result["answer"] == "Python"
    assert "agent_decides" not in result


def test_normal_batch_only_relays_owner_choices_not_delegated_unknowns(agent) -> None:
    server, notices = agent
    result = server.call("ask", {
        "project": "lumen", "conversation_id": "task-1", "questions": [
            {"question": "Which test runner?", "decision_kind": "delegated"},
            {"question": "Which colour?", "decision_kind": "preference"},
            {"question": "Publish Lumen publicly?", "decision_kind": "permission"},
        ],
    })
    assert result["results"][0]["agent_decides"]
    assert [item["question"] for item in result["ask_owner_batch"]["questions"]] == [
        "Which colour?", "Publish Lumen publicly?",
    ]
    assert server.call("owner_questions", {"project": "lumen"})["pending"] == 2
    assert len(notices.ids) == 2


@pytest.mark.parametrize("away", [False, True])
@pytest.mark.parametrize("boundary", ["withheld", "egress_request"])
def test_delegation_never_bypasses_a_privacy_boundary(agent, away, boundary) -> None:
    from varyz.personal_model.query import WithheldRecordMarker

    class Restricted:
        def resolve(self, request):
            return Resolution(
                disposition=QueryDisposition.NOT_KNOWN, basis=None, answer=None,
                tier=2, confidence=0, resolved_at=utc_now(), monotonic_seq=0,
                withheld=(WithheldRecordMarker(
                    record_kind="archive_evidence", reason="sensitive_evidence_withheld",
                    rule_version="alpha-local-v1",
                ),) if boundary == "withheld" else (),
                egress_request_id="request:test" if boundary == "egress_request" else None,
            )

    server, notices = agent
    if away:
        activate(server)
    server._resolution_client_override = Restricted()
    result = ask(server, "Which language?", decision_kind="delegated")
    assert result["disposition"] == "not_known"
    assert "agent_decides" not in result
    assert "ask_owner" not in result
    assert not notices.ids


def test_blockers_review_first_and_no_extra_authority(agent) -> None:
    server, notices = agent
    activate(server)
    ask(server, "Which palette?")
    ask(server, "Who is the audience?", decision_kind="blocking")
    result = ask(server, "May I publish?", decision_kind="permission")
    assert result["answer"] is None
    assert "agent_decides" not in result
    assert not notices.ids
    questions = review(server.conn, "lumen", "task-1")["questions"]
    assert [q["kind"] for q in questions] == ["permission", "blocking", "preference"]
    assert "no extra spending" in result["away_mode"]["how"]
    # Even the final upload must stay deferred until return, without granting consent.
    assert "Do not invoke an operation" in result["away_mode"]["how"]
    assert "even as the final step" in result["away_mode"]["how"]
    assert "only after the owner explicitly returns" in result["away_mode"]["how"]


class ArchiveSays:
    """Tier 2 that answers every question, the way an archive passage would."""

    tier = 2

    def try_resolve(self, request: ResolutionRequest) -> Resolution:
        return Resolution(
            disposition=QueryDisposition.ANSWERED, basis=AnswerBasis.OBSERVED,
            answer="Merge branch 'dev' into master [git: abc123]", tier=2,
            confidence=0.0154, resolved_at=utc_now(), monotonic_seq=0,
        )


def test_a_blocker_or_permission_is_the_owners_to_give_not_the_archives(
    config: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with serving(config, tiers=(ArchiveSays(),)) as (server, notices):
        # Away or not, an archive passage neither grants a permission nor
        # settles a blocker: on 2026-10-02 a "will you re-login?" came back
        # answered with merge commits.
        asked = ask(server, "May I publish the release notes?", decision_kind="permission")
        assert asked["answer"] is None and "ask_owner" in asked
        assert ask(server, "Which palette?")["disposition"] == "answered"

        activate(server)
        relogin = "Will you re-login so the gate replication can run?"
        for question, kind in ((relogin, "blocking"), ("May I spend $20?", "permission")):
            result = ask(server, question, decision_kind=kind, proposed_choice="wait",
                         reason="Nothing else depends on it.")
            assert result["answer"] is None
            assert result["deferred_owner"]["kind"] == kind
        # A preference still takes what the archive holds.
        assert ask(server, "Which palette?")["disposition"] == "answered"
        returned = server.call("away", {
            "action": "off", "project": "lumen", "conversation_id": "task-1",
            "instruction": "I'm back",
        })
        assert [q["question"] for q in returned["review"]["questions"]] == [
            "May I spend $20?", relogin, "May I publish the release notes?",
        ]
        assert len(notices.ids) == 1  # only the question asked before the owner left

        # Once the owner answers, that answer settles it.
        blocker = ask(server, relogin, decision_kind="blocking")
        server.call("answer_owner_question", {
            "escalation_id": blocker["escalation_id"], "answer": "Yes, done.",
            "scope": "project",
        })
        settled = ask(server, relogin, decision_kind="blocking")
        assert settled["answer"] == "Yes, done." and settled["tier"] == 1


# The three asks of 2026-10-02, and archive records in the shape of what they
# came back "answered" with: owner chat lines that share a word or two, merge
# commits from a cloned repository, an old commit under the project's old name.
OFF_TOPIC_ASKS = (
    ("Old 'varys' project history is in the archive under the old name. Should Varyz "
     "treat 'varys' as the same project as 'varyz'?", "preference"),
    ("Headless claude -p lost its login mid-eval, so the gate replication stopped. "
     "Will you re-login so the gate replication can run?", "blocking"),
    ("Two install defaults contradict rules learned from you. Should rules learned "
     "from you retire install defaults they contradict?", "preference"),
)
OFF_TOPIC_ARCHIVE = (
    ("ai_conversations", True, None, "rename every command and thing in varyz from varys to varyz"),
    ("ai_conversations", True, None, "decision: what do you suggest"),
    ("ai_conversations", True, None, "how many rules do i have"),
    ("git", False, "stable-diffusion-webui: Merge branch 'dev' into master", "Merge branch 'dev'"),
    ("git", False, "stable-diffusion-webui: Merge pull request #1201 from user/login-fix",
     "Fix the login redirect when gradio auth runs headless."),
    ("git", False, "stable-diffusion-webui: Merge pull request #1300 from user/defaults",
     "install: change defaults for the extension installer so learned settings are kept"),
    # Everyday lines that use the questions' other words, as any real archive
    # does. Coverage leaves out words an archive never used, so without these
    # a three-word overlap would count as most of the question.
    ("ai_conversations", True, None, "the eval numbers contradict the report"),
    ("ai_conversations", True, None, "retire the old laptop next month"),
    ("ai_conversations", True, None, "two builds failed overnight, same name as before"),
    ("git", True, "varys: trim each capsule at a line boundary",
     "A cut sentence reads as a different claim, so a trimmed capsule ends on a line."),
)


def _archive(config: Path, records) -> None:
    conn = open_db(load_config(config).db_path)
    when = datetime(2026, 9, 20, 9, tzinfo=UTC)
    try:
        with conn:
            for number, (source, authored, title, body) in enumerate(records):
                entry_id, _ = dao.upsert_entry(conn, NormalizedEntry(
                    kind=Kind.MESSAGE if source == "ai_conversations" else Kind.DOCUMENT,
                    external_id=f"{source}-{number}-{len(body)}", title=title, body=body,
                    occurred_start=when, captured_at=when, authored_by_user=authored,
                ), source=source, sensitivity=Sensitivity.PERSONAL)
                dao.replace_chunks(conn, entry_id, [f"{title}\n\n{body}" if title else body])
    finally:
        conn.close()


def test_off_topic_archive_hits_are_deferred_not_answered(
    config: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with config.open("a", encoding="utf-8") as settings:
        settings.write('evidence_verifier = "off"\n[retrieval]\ndistill = "off"\njudge = "off"\n')
    _archive(config, OFF_TOPIC_ARCHIVE)
    with serving(config, config=load_config(config)) as (server, _):
        server.call("away", {
            "action": "on", "project": "varyz", "conversation_id": "task-1",
            "instruction": "/away",
        })

        def asked(question: str, kind: str) -> dict:
            return server.call("ask", {
                "question": question, "project": "varyz", "conversation_id": "task-1",
                "decision_kind": kind, "proposed_choice": "a draft", "reason": "replaceable",
            })

        with monkeypatch.context() as unfloored:
            unfloored.setattr(
                "varyz.resolution.resolver.evidence_coverage", lambda *args, **kwargs: 1.0
            )
            # Without the coverage floor the installer merge answers, as it did then.
            unfloored_answer = asked(*OFF_TOPIC_ASKS[2])
            assert unfloored_answer["disposition"] == "answered"
            assert "stable-diffusion-webui" in unfloored_answer["answer"]
        for question, kind in OFF_TOPIC_ASKS:
            result = asked(question, kind)
            assert result["answer"] is None, question
            assert result["deferred_owner"]["kind"] == kind
        assert server.call("away", {
            "action": "status", "project": "varyz", "conversation_id": "task-1",
        })["review"]["pending"] == 3

        # A record that does settle it still answers, away or not.
        _archive(config, (("manual", True, None,
                           "The old varys project history is the same project, renamed: "
                           "treat varys as varyz."),))
        settled = asked(*OFF_TOPIC_ASKS[0])
        assert settled["disposition"] == "answered" and settled["tier"] == 2
        assert "renamed" in settled["answer"]


def test_task_scope_never_silences_another_task_or_project(agent) -> None:
    server, notices = agent
    activate(server)
    assert "ask_owner" in ask(server, "Which font?", conversation_id="task-2")
    assert "ask_owner" in ask(server, "Which font?", project="other")
    assert len(notices.ids) == 2
    assert "deferred_owner" in ask(server, "Which font?")
    assert len(notices.ids) == 2  # shared question, linked to this task without another notice
    assert review(server.conn, "lumen", "task-1")["pending"] == 1


def test_mode_persists_across_clients_and_restart(agent, config: Path) -> None:
    server, _ = agent
    activate(server)
    other = VaryzServer(config, agent_token=server._agent_token)
    try:
        mode = other.call("away", {
            "action": "status", "project": "lumen", "conversation_id": "task-1",
        })
        assert mode["away_mode"]["active"]
    finally:
        other.close()


def test_duration_and_scope_precedence(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "away.db")
    now = datetime(2026, 9, 27, tzinfo=UTC)
    try:
        set_mode(conn, active=True, scope="all", project=None, conversation_id=None,
                 instruction="Away everywhere for an hour", duration_minutes=60, now=now)
        assert status(conn, "other", "another", now=now)["active"]
        set_mode(conn, active=False, scope="task", project="lumen", conversation_id="one",
                 instruction="Back in this task", now=now)
        assert not status(conn, "lumen", "one", now=now)["active"]
        assert status(conn, "lumen", "two", now=now)["active"]
        assert not status(conn, "other", "two", now=now + timedelta(minutes=60))["active"]
    finally:
        conn.close()


def test_completion_preserves_review_and_does_not_end_other_tasks(agent) -> None:
    server, _ = agent
    activate(server, scope="all")
    ask(server, "Which palette?")
    result = server.call("away", {
        "action": "complete", "project": "lumen", "conversation_id": "task-1",
    })
    assert not result["away_mode"]["active"]
    assert result["review"]["pending"] == 1
    assert status(server.conn, "lumen", "task-2")["active"]
    with pytest.raises(ValueError, match="only ends this task"):
        server.call("away", {"action": "complete", "scope": "all"})


def test_pending_questions_before_activation_join_review(agent) -> None:
    server, _ = agent
    ask(server, "Which palette?")
    assert activate(server)["review"]["pending"] == 1


def test_missing_task_id_is_issued_once_for_the_agent_to_reuse(agent) -> None:
    server, _ = agent
    result = server.call("away", {
        "action": "on", "project": "lumen", "instruction": "/away",
    })
    task = result["away_mode"]["conversation_id"]
    assert task
    assert "deferred_owner" in ask(server, "Which palette?", conversation_id=task)
    assert not status(server.conn, "lumen", "unrelated")["active"]


def test_invalid_change_keeps_existing_mode_and_asks_nothing(agent) -> None:
    server, notices = agent
    activate(server)
    for extra in ({"instruction": ""}, {"duration_minutes": 0}, {"scope": "everything"}):
        with pytest.raises(ValueError):
            activate(server, **extra)
        assert status(server.conn, "lumen", "task-1")["active"]
    assert not notices.ids


@pytest.mark.parametrize("away", [False, True])
def test_daemon_outage_does_not_become_delegated_permission(agent, away) -> None:
    from varyz.resolution.client import ResolutionClientError

    class Down:
        def resolve(self, request):
            raise ResolutionClientError("offline")

    server, _ = agent
    if away:
        activate(server)
    server._resolution_client_override = Down()
    result = ask(server, "Which language?", decision_kind="delegated")
    assert result["error"] == "resolution_daemon_unavailable"
    assert "agent_decides" not in result


def test_unregistered_agent_cannot_change_mode(agent, config: Path) -> None:
    server, _ = agent
    other = VaryzServer(config, agent_token="")
    try:
        assert activate(other)["error"]
        assert not status(server.conn, "lumen", "task-1")["active"]
    finally:
        other.close()


def test_away_reaches_briefing_and_finish_without_local_intent_classification(agent) -> None:
    server, _ = agent
    activate(server)
    for tool in ("start_task", "finish_check"):
        result = server.call(tool, {
            "task": "Write a Python tool", "project": "lumen", "conversation_id": "task-1",
        })
        assert result["away_mode"]["active"]
    ask(server, "Which palette?")
    assert "defer" in server.call("owner_questions", {
        "project": "lumen", "conversation_id": "task-1",
    })["how"].lower()


def test_slash_hook_hands_intent_to_working_agent(agent, config: Path, tmp_path: Path) -> None:
    server, _ = agent
    result = hooks.user_prompt(load_config(config), {
        "prompt": "/away", "session_id": "task-1", "cwd": str(tmp_path / "lumen"),
    })
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "task-1" in text and "working agent" in text
    assert not status(server.conn, "lumen", "task-1")["active"]


def test_cli_requires_scope_and_can_end_absence(config: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    runner = CliRunner()
    assert runner.invoke(app, ["away", "--config", str(config)]).exit_code == 2
    args = ["--scope", "project", "--project", "lumen", "--config", str(config)]
    started = runner.invoke(app, ["away", "on", *args])
    assert started.exit_code == 0, started.output
    # Reading needs no task; on the test Mac it refused without --task.
    shown = runner.invoke(app, ["away", "status", "--project", "lumen", "--config", str(config)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["away_mode"]["active"] is True
    ended = runner.invoke(app, ["away", "off", *args])
    assert ended.exit_code == 0, ended.output
    conn = open_db(load_config(config).db_path)
    try:
        assert not status(conn, "lumen")["active"]
    finally:
        conn.close()


@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_command_install_preserves_conflicts_and_is_idempotent(tmp_path: Path, client: str) -> None:
    config = tmp_path / (".codex/config.toml" if client == "codex" else ".claude.json")
    destination = install._away_skill_destination(client, config)
    changed, _ = install._install_away_skill(client, config, dry_run=True)
    assert not changed and not destination.exists()
    assert install._install_away_skill(client, config, dry_run=False)[0]
    assert not install._install_away_skill(client, config, dry_run=False)[0]
    assert install.uninstall_away_skill(client, config)
    destination.write_text("My unrelated away command", encoding="utf-8")
    changed, detail = install._install_away_skill(client, config, dry_run=False)
    assert not changed and "conflict" in detail
    assert not install.uninstall_away_skill(client, config)
    assert destination.read_text(encoding="utf-8") == "My unrelated away command"


def test_claude_install_adds_a_reader_that_can_only_read_through_varyz(
    config: Path, tmp_path: Path
) -> None:
    client_config = tmp_path / ".claude.json"
    client_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    install.install_mcp(config_path=config, paths={"claude-code": client_config})
    reader = tmp_path / ".claude" / "agents" / "varyz-rule-miner.md"
    text = reader.read_text(encoding="utf-8")
    assert "model: sonnet" in text
    tools = next(line.removeprefix("tools: ") for line in text.splitlines()
                 if line.startswith("tools: "))
    assert set(tools.split(", ")) == {
        "mcp__varyz__profile_reading_task",
        "mcp__varyz__submit_profile_reading",
        "mcp__varyz__profile_reading_status",
    }
    assert not install._install_reader_agent("claude-code", client_config, dry_run=False)[0]
    assert not install.uninstall_reader_agent("codex", client_config)
    assert install.uninstall_reader_agent("claude-code", client_config)
    assert not reader.exists()
    # The owner's own agent of that name is theirs.
    reader.write_text("my reader", encoding="utf-8")
    changed, detail = install._install_reader_agent("claude-code", client_config, dry_run=False)
    assert not changed and "conflict" in detail
    assert not install.uninstall_reader_agent("claude-code", client_config)
    assert reader.read_text(encoding="utf-8") == "my reader"


def test_claude_install_includes_slash_skill(config: Path, tmp_path: Path) -> None:
    client_config = tmp_path / ".claude.json"
    client_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    install.install_mcp(config_path=config, paths={"claude-code": client_config})
    skill = install._away_skill_destination("claude-code", client_config)
    assert skill.is_file()
    assert "name: away" in skill.read_text(encoding="utf-8")
