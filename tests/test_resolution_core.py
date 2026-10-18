from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.query import AnswerBasis, QueryDisposition
from varyz.pipeline.windows import build_windows
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import ResolutionDaemon, ResolutionService, build_resolution_daemon
from varyz.resolution.escalation import SQLiteEscalationQueue
from varyz.resolution.evidence_verify import EvidenceVerifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.local_classifier import ClassifierUnavailable
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.resolver import EvidenceTier, ResolutionResolver, SQLiteResolutionLedger
from varyz.retrieval.capsule import Capsule, Evidence
from varyz.retrieval.search import evidence_coverage, keyword_search, lexically_grounded


def _caller() -> Caller:
    return Caller(
        agent_id="agent:test",
        display_name="Test agent",
        purpose="coding",
        device_id="device:agent",
    )


def _request(question: str, *, allow_escalation: bool = True) -> ResolutionRequest:
    return ResolutionRequest(
        question=question,
        caller=_caller(),
        purpose="coding",
        allow_escalation=allow_escalation,
    )


def test_explicit_knowledge_is_append_only_and_matches_normalized_question(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    first = store.remember(
        question="Should the API stay backward compatible?",
        answer="Yes.",
        caller=_caller(),
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id="device:owner",
    )
    second = store.remember(
        question="Should the API stay backward compatible?",
        answer="No; this is a clean break.",
        caller=_caller(),
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id="device:owner",
        supersedes_id=first,
    )

    result = store.try_resolve(_request("  SHOULD the API stay backward compatible?! "))
    assert result is not None
    assert result.answer == "No; this is a clean break."
    assert [item.id for item in store.history("Should the API stay backward compatible?")] == [
        first,
        second,
    ]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE owner_knowledge SET answer = 'mutated' WHERE id = ?", (first,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM owner_knowledge WHERE id = ?", (first,))


def test_owner_decision_paraphrases_resolve_but_unrelated_questions_abstain(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    store.remember(
        question="What should Varyz prioritize as its primary data sources?",
        answer=("Prioritize local AI conversations, Git and GitHub, and approved project "
                "documents. Keep WhatsApp and Telegram raw-only and exclude them from "
                "the graph, personal-model compile, and agent-facing retrieval."),
        caller=_caller(), basis=AnswerBasis.EXPLICIT, source="manual",
        device_id="device:owner",
    )
    for question in (
        "Which data sources has Egor explicitly prioritized for Varyz, "
        "and which should agents exclude?",
        "Should Claude process WhatsApp and Telegram data through Varyz's graph?",
    ):
        result = store.try_resolve(_request(question))
        assert result is not None
        assert "raw-only" in result.answer
        assert result.confidence < 1
    assert store.try_resolve(_request("Which colour should Claude use for Varyz?")) is None
    assert store.try_resolve(_request("Why exclude WhatsApp and Telegram from the graph?")) is None


def test_paraphrase_abstains_on_competing_owner_answers_and_supersession(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    old = store.remember(
        question="Should the site use a blue colour?", answer="Use blue.",
        caller=_caller(), basis=AnswerBasis.EXPLICIT, source="manual",
        device_id="device:owner",
    )
    store.remember(
        question="Should the site use a red colour?", answer="Use red.",
        caller=_caller(), basis=AnswerBasis.EXPLICIT, source="manual",
        device_id="device:owner",
    )
    assert store.try_resolve(_request("What colour should the site use?")) is None
    store.remember(
        question="Should the site use a blue colour?", answer="Do not use blue.",
        caller=_caller(), basis=AnswerBasis.EXPLICIT, source="manual",
        device_id="device:owner", supersedes_id=old,
    )
    result = store.try_resolve(_request("Should the site use a blue colour?"))
    assert result is not None and result.answer == "Do not use blue."


def test_tier_one_wins_and_resolution_ledger_is_self_contained(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    store.remember(
        question="Which API shape?",
        answer="Keep v1.",
        caller=_caller(),
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id="device:owner",
    )

    class ContradictingTier:
        tier = 2

        def try_resolve(self, request: ResolutionRequest) -> Resolution:
            return Resolution(
                disposition=QueryDisposition.ANSWERED,
                basis=AnswerBasis.OBSERVED,
                answer="Replace v1.",
                tier=2,
                confidence=1,
                resolved_at=datetime.now(UTC),
                monotonic_seq=0,
            )

    ledger = SQLiteResolutionLedger(conn, device_id="device:daemon")
    resolver = ResolutionResolver((store, ContradictingTier()), ledger=ledger)
    result = resolver.resolve(_request("Which API shape?"))

    assert result.tier == 1
    assert result.answer == "Keep v1."
    row = conn.execute("SELECT * FROM resolution_ledger").fetchone()
    assert row["question"] == "Which API shape?"
    assert row["caller_agent_id"] == "agent:test"
    assert row["answer"] == "Keep v1."
    assert row["request_json"] and row["resolution_json"]


def test_empty_archive_escalates_answer_survives_restart_and_becomes_tier_one(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restart.db"
    first_conn = open_db(db_path)
    knowledge = SQLiteKnowledgeStore(first_conn)
    queue = SQLiteEscalationQueue(first_conn, knowledge, device_id="device:daemon")
    resolver = ResolutionResolver((knowledge,), escalation_queue=queue)

    pending = resolver.resolve(_request("Which branch policy should I use?"))
    assert pending.disposition == QueryDisposition.CLARIFICATION
    assert pending.basis == AnswerBasis.ESCALATED
    escalation_id = pending.escalation_id
    assert escalation_id is not None
    first_conn.close()

    restarted_conn = open_db(db_path)
    restarted_knowledge = SQLiteKnowledgeStore(restarted_conn)
    restarted_queue = SQLiteEscalationQueue(
        restarted_conn, restarted_knowledge, device_id="device:daemon"
    )
    assert restarted_queue.poll(escalation_id) is not None
    answered = restarted_queue.answer(
        escalation_id, "Use short-lived feature branches.", device_id="device:owner"
    )
    assert answered.basis == AnswerBasis.USER_CONFIRMED

    restarted_resolver = ResolutionResolver((restarted_knowledge,))
    recalled = restarted_resolver.resolve(
        _request("which branch policy should i use", allow_escalation=False)
    )
    assert recalled.answer == "Use short-lived feature branches."
    assert recalled.tier == 1
    restarted_conn.close()


def test_empty_archive_without_escalation_abstains_safely(conn) -> None:
    store = SQLiteKnowledgeStore(conn)

    class BrokenOptionalTier:
        tier = 3

        def try_resolve(self, request: ResolutionRequest) -> Resolution | None:
            raise sqlite3.OperationalError("no active compiled generation")

    result = ResolutionResolver((store, BrokenOptionalTier())).resolve(
        _request("What has never been recorded?", allow_escalation=False)
    )
    assert result.disposition == QueryDisposition.NOT_KNOWN
    assert result.answer is None
    assert result.basis is None


def test_loopback_daemon_client_is_the_complete_public_surface(tmp_path: Path) -> None:
    db_path = tmp_path / "daemon.db"
    seed = open_db(db_path)
    seed.close()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    knowledge = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, knowledge, device_id="device:daemon")
    resolver = ResolutionResolver((knowledge,), escalation_queue=queue)
    service = ResolutionService(resolver, queue, knowledge)

    with ResolutionDaemon(service, host="127.0.0.1", port=0) as daemon:
        host, port = daemon.address
        client = ResolutionClient(host, port)
        pending = client.resolve(_request("Should this endpoint be stable?"))
        assert pending.escalation_id is not None
        answered = client.answer(
            pending.escalation_id,
            "Yes, preserve compatibility.",
            device_id="device:owner",
        )
        assert answered.basis == AnswerBasis.USER_CONFIRMED
        recalled = client.resolve(_request("should this endpoint be stable"))
        assert recalled.answer == "Yes, preserve compatibility."
        assert client.status().host == "127.0.0.1"
    conn.close()


def test_daemon_refuses_unsafe_widening(conn) -> None:
    knowledge = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, knowledge)
    service = ResolutionService(ResolutionResolver((knowledge,)), queue, knowledge)
    with pytest.raises(ValueError, match="non-loopback"):
        ResolutionDaemon(service, host="0.0.0.0", auth_mode="session")


def test_evidence_tier_returns_source_addressed_reported_answer(
    conn, cfg, monkeypatch
) -> None:
    evidence = Evidence(
        query="Which API shape did I choose?",
        capsules=[
            Capsule(
                text="I chose the versioned endpoint.",
                source="manual",
                kind="note",
                title=None,
                occurred="2026-09-01T10:00:00+00:00",
                external_id="note-1",
                entry_id="entry-1",
                score=0.91,
                sensitivity="personal",
                authored_by_user=True,
            )
        ],
    )
    monkeypatch.setattr("varyz.resolution.resolver.recall", lambda *args, **kwargs: evidence)
    monkeypatch.setattr(
        "varyz.resolution.resolver.lexically_grounded", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "varyz.resolution.resolver.evidence_coverage", lambda *args, **kwargs: 1.0
    )

    result = EvidenceTier(conn, cfg).try_resolve(
        _request("Which API shape did I choose?", allow_escalation=False)
    )

    assert result is not None
    assert result.tier == 2
    assert result.basis == AnswerBasis.REPORTED
    assert "[manual: note-1]" in (result.answer or "")
    assert result.life_result is not None
    assert result.life_result.answers[0].provenance.entry_ids == ("entry-1",)

    # Strict frozen contracts must also survive their real JSON/HTTP boundary.
    knowledge = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, knowledge, device_id="device:daemon")
    service = ResolutionService(
        ResolutionResolver((EvidenceTier(conn, cfg),)), queue, knowledge
    )
    with ResolutionDaemon(service, host="127.0.0.1", port=0) as daemon:
        transported = ResolutionClient(*daemon.address).resolve(
            _request("Which API shape did I choose?", allow_escalation=False)
        )
    assert transported.life_result is not None
    assert transported.life_result.answers[0].provenance.entry_ids == ("entry-1",)


def test_sensitive_match_is_withheld_instead_of_replaced_by_unrelated_evidence(
    conn, cfg
) -> None:
    """A withheld top hit must not make adjacent public records look like an answer."""

    occurred = datetime(2026, 2, 8, 9, tzinfo=UTC)
    records = (
        (
            "health-specialist",
            "Specialist follow-up",
            "The specialist requested a symptom log before the follow-up appointment.",
            Sensitivity.SENSITIVE,
        ),
        (
            "project-follow-up",
            "Project follow-up",
            "The team follow-up covered release notes and the deployment checklist.",
            Sensitivity.PERSONAL,
        ),
    )
    with conn:
        for external_id, title, body, sensitivity in records:
            entry_id, _ = dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.NOTE,
                    external_id=external_id,
                    title=title,
                    body=body,
                    occurred_start=occurred,
                    captured_at=occurred,
                    authored_by_user=True,
                ),
                source="manual",
                sensitivity=sensitivity,
            )
            dao.replace_chunks(conn, entry_id, [f"{title}\n\n{body}"])

    result = EvidenceTier(conn, cfg).try_resolve(
        _request(
            "What did the specialist ask me to do before the follow-up?",
            allow_escalation=False,
        )
    )

    assert result is not None
    assert result.disposition == QueryDisposition.NOT_KNOWN
    assert result.answer is None
    assert result.withheld[0].reason == "sensitive_evidence_withheld"


def test_agent_evidence_excludes_social_sources_but_manual_search_keeps_them(
    conn, cfg
) -> None:
    occurred = datetime(2026, 9, 20, 9, tzinfo=UTC)
    entry_id, _ = dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id="social-only-decision",
            title=None,
            body="The Varkesh launch rule is crimson.",
            occurred_start=occurred,
            captured_at=occurred,
            authored_by_user=True,
        ),
        source="whatsapp",
        sensitivity=Sensitivity.PERSONAL,
    )
    dao.replace_chunks(conn, entry_id, ["The Varkesh launch rule is crimson."])
    conn.commit()
    cfg.graph.excluded_sources = ["whatsapp", "telegram"]

    manual_ids, _ = keyword_search(conn, "Varkesh crimson", 5)
    agent_ids, _ = keyword_search(
        conn,
        "Varkesh crimson",
        5,
        excluded_sources=tuple(cfg.graph.excluded_sources),
    )
    resolved = EvidenceTier(conn, cfg).try_resolve(
        _request("What is the Varkesh launch rule?", allow_escalation=False)
    )

    assert manual_ids == [entry_id]
    assert agent_ids == []
    assert resolved is None


def test_default_daemon_is_keyless_and_silent(
    tmp_path: Path, cfg, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "varyz.resolution.resolver.recall",
        lambda *args, **kwargs: Evidence(query="unknown", abstained=True),
    )
    daemon = build_resolution_daemon(
        cfg.db_path,
        config=cfg,
        host="127.0.0.1",
        port=0,
        device_id="device:offline",
    )
    try:
        with daemon:
            host, port = daemon.address
            result = ResolutionClient(host, port).resolve(
                _request("What is not in this empty archive?")
            )
            assert result.basis == AnswerBasis.ESCALATED
    finally:
        daemon.stop()

    # A waiting question is found through owner_questions, never announced: the
    # Mac banner announced questions no agent was waiting on.
    assert importlib.util.find_spec("varyz.resolution.notify") is None
    assert not (cfg.data_dir / "runtime" / "resolution" / "cli-notifications.jsonl").exists()


def _know(store: SQLiteKnowledgeStore, question: str, answer: str) -> str:
    return store.remember(
        question=question, answer=answer, caller=_caller(), basis=AnswerBasis.EXPLICIT,
        source="manual", device_id="device:owner",
    )


def test_paraphrase_requires_every_salient_term_and_the_same_named_scope(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _know(store, "Should the Kestrel public API stay backward compatible?",
          "Yes. Keep the v1 endpoints working until at least 2027.")
    _know(store, "Which package manager should Python projects use?", "uv, with a lockfile.")
    _know(store, "Does the owner prefer morning meetings?", "No meetings before 11:00.")
    _know(store, "Which database does Kestrel use?", "Postgres hosted on Supabase.")

    for question, expected in (
        ("Must the Kestrel API remain backwards compatible?", "v1 endpoints"),
        ("What package manager does the owner prefer for Python?", "uv"),
        ("Does the owner like meetings in the morning?", "11:00"),
        ("Which DB should I use for Kestrel?", "Supabase"),
    ):
        result = store.try_resolve(_request(question))
        assert result is not None and expected in (result.answer or ""), question
        assert result.confidence < 1

    for question in (
        "Should the Lumen public API stay backward compatible?",  # different project
        "Which package manager should JavaScript projects use?",  # unexplained scope
        "Does the owner prefer morning workouts?",  # generic words alone
        "Which database should I use?",  # the decision is scoped to Kestrel
        "When did the owner pick Supabase for Kestrel?",  # timing is not recorded
    ):
        assert store.try_resolve(_request(question)) is None, question


def test_a_decision_recorded_under_the_old_name_answers_the_new_one(conn) -> None:
    # The project was renamed Varys -> Varyz; decisions stated before the rename
    # still name Varys, and agents now ask about Varyz.
    store = SQLiteKnowledgeStore(conn)
    _know(store, "Which brand colours and fonts does the Varys website use?",
          "Night #11120f with marigold #ffc23d, Palatino-style serif.")
    _know(store, "Where is the Varyz site hosted?", "varyzai.com, not published yet.")

    for question, expected in (
        ("Which brand colours and fonts does the Varyz website use?", "marigold"),
        ("Which brand colours and fonts does the Varys website use?", "marigold"),
        ("Where is the Varys site hosted?", "varyzai.com"),
    ):
        result = store.try_resolve(_request(question))
        assert result is not None and expected in (result.answer or ""), question
    # The alias does not make a name-free question match the scoped decision.
    assert store.try_resolve(_request("Which brand colours does the Lumen website use?")) is None


def test_paraphrase_can_match_through_the_stored_answer_with_enough_terms(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _know(store, "What should Varyz prioritize as its primary data sources?",
          "Keep WhatsApp and Telegram raw-only and exclude them from the graph.")
    result = store.try_resolve(
        _request("Should an agent process WhatsApp and Telegram in Varyz's graph?")
    )
    assert result is not None and "raw-only" in (result.answer or "")
    assert store.try_resolve(_request("Is WhatsApp raw?")) is None


def test_tier_one_withholds_from_denied_revoked_and_over_limit_callers(conn) -> None:
    from varyz.trust import grant_agent_capability, register_agent, revoke_agent

    store = SQLiteKnowledgeStore(conn)
    _know(store, "Which database does Kestrel use?", "Postgres hosted on Supabase.")
    with conn:
        allowed = register_agent(conn, agent_id="agent:ok", display_name="Ok",
                                 purpose="coding", device_id="d").caller
        zero = grant_agent_capability(conn, "agent:ok", purpose="zero",
                                      max_items=0, max_chars=0)
        tiny = grant_agent_capability(conn, "agent:ok", purpose="tiny",
                                      max_items=1, max_chars=5)
        no_knowledge = grant_agent_capability(conn, "agent:ok", purpose="summary_only",
                                              resource_types=("summary",))
        revoked = register_agent(conn, agent_id="agent:gone", display_name="Gone",
                                 purpose="coding", device_id="d").caller
        revoke_agent(conn, revoked.agent_id, capability_id=revoked.capability_id)

    def ask(caller: Caller) -> Resolution:
        result = store.try_resolve(ResolutionRequest(
            question="Which database does Kestrel use?", caller=caller, purpose=caller.purpose,
        ))
        assert result is not None
        return result

    assert ask(allowed).answer == "Postgres hosted on Supabase."
    for capability, purpose, reason in (
        (zero, "zero", "caller_capability_denied"),
        (no_knowledge, "summary_only", "caller_capability_denied"),
        (tiny, "tiny", "answer_exceeds_caller_limit"),
    ):
        result = ask(allowed.model_copy(update={"capability_id": capability, "purpose": purpose}))
        assert result.answer is None and result.withheld[0].reason == reason
    gone = ask(revoked)
    assert gone.answer is None and gone.withheld[0].reason == "caller_capability_denied"


def test_denied_caller_is_terminal_and_never_reaches_later_tiers_or_the_owner(conn) -> None:
    from varyz.trust import register_agent, revoke_agent

    store = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, store, device_id="device:owner")
    with conn:
        revoked = register_agent(conn, agent_id="agent:gone", display_name="Gone",
                                 purpose="coding", device_id="d").caller
        revoke_agent(conn, revoked.agent_id, capability_id=revoked.capability_id)
    result = ResolutionResolver((store,), escalation_queue=queue).resolve(ResolutionRequest(
        question="Which database does Kestrel use?", caller=revoked, purpose="coding",
    ))
    assert result.disposition == QueryDisposition.NOT_KNOWN
    assert result.escalation_id is None
    assert queue.pending() == ()


def test_sensitive_withholding_escalates_to_the_owner_without_leaking(conn, cfg) -> None:
    occurred = datetime(2026, 2, 8, 9, tzinfo=UTC)
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE, external_id="health", title="Specialist follow-up",
                body="The specialist requested a symptom log before the follow-up.",
                occurred_start=occurred, captured_at=occurred, authored_by_user=True,
            ),
            source="manual", sensitivity=Sensitivity.SENSITIVE,
        )
        dao.replace_chunks(
            conn, entry_id,
            ["Specialist follow-up\n\nThe specialist requested a symptom log."],
        )
    store = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, store, device_id="device:owner")
    resolver = ResolutionResolver((store, EvidenceTier(conn, cfg)), escalation_queue=queue)
    question = "What did the specialist ask me to do before the follow-up?"

    quiet = resolver.resolve(_request(question, allow_escalation=False))
    assert quiet.disposition == QueryDisposition.NOT_KNOWN and quiet.escalation_id is None
    assert quiet.withheld[0].reason == "sensitive_evidence_withheld"

    loud = resolver.resolve(_request(question))
    assert loud.basis == AnswerBasis.ESCALATED and loud.answer is None
    assert loud.withheld[0].reason == "sensitive_evidence_withheld"
    assert len(queue.pending()) == 1

    queue.answer(loud.escalation_id or "", "Keep a symptom log.", device_id="device:owner")
    answered = resolver.resolve(_request(question))
    assert answered.tier == 1 and answered.answer == "Keep a symptom log."


def test_retraction_hides_a_belief_but_keeps_its_history(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    kept = _know(store, "Which database does Kestrel use?", "Postgres hosted on Supabase.")
    old = _know(store, "Does the owner prefer morning meetings?", "No meetings before 11:00.")
    new = store.remember(
        question="Does the owner prefer morning meetings?", answer="Mornings are fine now.",
        caller=_caller(), basis=AnswerBasis.EXPLICIT, source="manual",
        device_id="device:owner", supersedes_id=old,
    )
    assert [entry.id for entry in store.current()] == [new, kept]

    store.retract(new, caller=_caller(), device_id="device:owner", reason="changed my mind")
    assert store.try_resolve(_request("Does the owner prefer morning meetings?")) is None
    assert [entry.id for entry in store.current()] == [kept]
    assert len(store.history("Does the owner prefer morning meetings?")) == 2

    with pytest.raises(ValueError, match="already retracted"):
        store.retract(new, caller=_caller(), device_id="device:owner")
    with pytest.raises(KeyError):
        store.retract("missing", caller=_caller(), device_id="device:owner")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM owner_knowledge_retractions")


def test_paraphrase_accepts_named_options_abbreviations_and_framing_only(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _know(store, "Which package manager should Python projects use?", "uv, with a lockfile.")
    _know(store, "Which frontend framework should new web projects use?", "SvelteKit.")
    _know(store, "Should agents update documentation for every change?",
          "Only when behaviour or setup changes.")
    _know(store, "What timezone does the owner work in?", "Europe/Amsterdam.")
    _know(store, "Should the Harbor CLI collect telemetry?", "No telemetry.")

    for question, expected in (
        ("For Python projects, is the package manager pip or uv?", "uv"),
        ("Vue or SvelteKit for the frontend framework of a new web project?", "SvelteKit"),
        ("Do the docs need updating for this change?", "setup"),
        ("Is the owner's time zone Europe/Amsterdam?", "Amsterdam"),
        ("Is it fine for the Harbor command-line tool to collect telemetry?", "No"),
    ):
        result = store.try_resolve(_request(question))
        assert result is not None and expected in (result.answer or ""), question

    for question in (
        "Should Python projects use Poetry or Hatch as the build backend?",  # neither option
        "Which backend framework should new web projects use?",  # different layer
        "Should the Harbor command-line tool collect crash dumps?",  # different object
        "Is Vue or React better for mobile apps?",  # no decision names either
    ):
        assert store.try_resolve(_request(question)) is None, question


def test_the_daemon_leaves_once_its_source_moved_on_and_settled() -> None:
    """A daemon started this morning must not keep answering with this morning's code."""
    from varyz.resolution.daemon import CODE_SETTLE_POLLS, watch_code

    stops: list[bool] = []
    # An edit in progress: every poll sees another fingerprint, so nothing stops.
    moving = iter(["launched"] + [f"edit-{n}" for n in range(30)])
    polls = iter(range(20))
    assert not watch_code(
        lambda: stops.append(True), lambda: next(moving),
        sleep=lambda _s: None, running=lambda: next(polls, None) is not None,
    )
    # An edit that was undone leaves the daemon where it was.
    undone = iter(["launched", "edit", "launched"] + ["launched"] * 20)
    polls = iter(range(20))
    assert not watch_code(
        lambda: stops.append(True), lambda: next(undone),
        sleep=lambda _s: None, running=lambda: next(polls, None) is not None,
    )
    assert stops == []
    # A finished edit stops it after the source held still long enough.
    finished = iter(["launched"] + ["released"] * 30)
    seen: list[int] = []
    assert watch_code(
        lambda: stops.append(True), lambda: seen.append(1) or next(finished),
        sleep=lambda _s: None,
    )
    assert stops == [True] and len(seen) == 1 + CODE_SETTLE_POLLS


def test_a_stale_daemon_stops_serving(tmp_path: Path, cfg) -> None:
    import threading

    from varyz.resolution.daemon import watch_code

    daemon = build_resolution_daemon(
        cfg.db_path, config=cfg, host="127.0.0.1", port=0, device_id="device:stale",
    )
    serving = threading.Thread(target=daemon.serve_forever, daemon=True)
    serving.start()
    try:
        for _ in range(500):  # serving before the source moves
            if daemon.address[1]:
                break
            threading.Event().wait(0.01)
        host, port = daemon.address
        assert ResolutionClient(host, port).status().running
        versions = iter(["launched"] + ["released"] * 30)
        assert watch_code(daemon.stop, lambda: next(versions), sleep=lambda _s: None)
        serving.join(timeout=10)
        assert not serving.is_alive()
    finally:
        daemon.stop()


def _in_project(question: str, project: str) -> ResolutionRequest:
    return ResolutionRequest(
        question=question, caller=_caller(), purpose="coding",
        allow_escalation=False, project=project,
    )


def test_the_project_named_in_the_question_is_context_not_subject(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _know(store, "Which analytics?", "Cookie-free only.")
    store.remember(
        question="Which accent colour?", answer="#6d5efc", caller=_caller(),
        basis=AnswerBasis.EXPLICIT, source="manual", device_id="device:owner",
        scope="lumen",
    )

    for question, expected in (
        ("Which analytics should lumen use?", "Cookie-free"),
        ("Which accent colour should the lumen page use?", "#6d5efc"),
        ("What accent colour does Lumen's site use?", "#6d5efc"),
    ):
        result = store.try_resolve(_in_project(question, "lumen"))
        assert result is not None and expected in (result.answer or ""), question
        assert result.confidence < 1

    for question in (
        "Which analytics dashboard should lumen use?",  # dashboard is unexplained
        "Which analytics should orbit use?",  # another project is still a subject
    ):
        assert store.try_resolve(_in_project(question, "lumen")) is None, question


def test_archive_evidence_is_not_grounded_by_the_projects_own_name(conn, cfg) -> None:
    occurred = datetime(2026, 9, 20, 9, tzinfo=UTC)
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE, external_id="lumen-pricing", title="Lumen pricing",
                body="The lumen pricing page ships after the waitlist closes.",
                occurred_start=occurred, captured_at=occurred, authored_by_user=True,
            ),
            source="manual", sensitivity=Sensitivity.PERSONAL,
        )
        dao.replace_chunks(
            conn, entry_id, ["Lumen pricing\n\nThe lumen pricing page ships after the waitlist."]
        )
    tier = EvidenceTier(conn, cfg)

    # "lumen" and "page" are half the question's words, and both only say where.
    assert tier.try_resolve(
        _in_project("Which accent colour should the lumen page use?", "lumen")
    ) is None
    known = tier.try_resolve(_in_project("When does the lumen pricing page ship?", "lumen"))
    assert known is not None and known.tier == 2
    assert "waitlist" in (known.answer or "")


def _note(conn, external_id: str, title: str | None, body: str) -> str:
    occurred = datetime(2026, 9, 20, 9, tzinfo=UTC)
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE, external_id=external_id, title=title, body=body,
                occurred_start=occurred, captured_at=occurred, authored_by_user=True,
            ),
            source="manual", sensitivity=Sensitivity.PERSONAL,
        )
        dao.replace_chunks(conn, entry_id, [f"{title}\n\n{body}" if title else body])
    return entry_id


# Both questions came back from the owner's own archive on 2026-09-27 as tier-2
# "answers" at confidence 0.015: the top keyword hits shared a stray word or two
# with the question and were about something else. These records keep the shape
# of those hits -- a commit that reverts a commit on a second interrupt, a long
# commit that uses setup, agent, line, still and say nowhere near each other, a
# chat message about finishing the last task -- and the index carries most of
# each question's words, so the index-wide gate passes as it did there.
_UNRELATED_TO_THE_COMMIT_QUESTION = (
    (
        "stable-diffusion-webui: stop on a second interrupt",
        "Immediately stop generating on the second interrupt. This reverts an "
        "earlier commit that waited for the current step to finish.",
    ),
    (None, "Folded the laundry before the new flatmate moved in."),
    (None, "Single origin beans are back in stock at the main market."),
    (None, "The film's release moved to Friday."),
)
_UNRELATED_TO_THE_SETUP_QUESTION = (
    (
        "varys: Let the owner say how much of the evidence an agent puts on the screen",
        "Evidence used to arrive as one block. Now the owner picks a budget in the "
        "config file and each tool trims what it returns to fit that budget, newest "
        "records first, so a small screen is never flooded with quotes nobody asked "
        "for.\n\nThe setup wizard gained a question for it, defaulting to the old "
        "behaviour so nothing changes for anyone who skips it. Tests cover the "
        "trimming, the default, and the migration of existing config files, which "
        "keep their current limits until the owner edits them.\n\nEach trimmed "
        "capsule ends at a line boundary rather than mid-word, since a cut sentence "
        "reads as a different claim. The reranker order is unchanged and still "
        "decides which records are kept.",
    ),
    (None, "are you finishing the last task"),
    (None, "The printer stays connected to the office wifi."),
)
_COMMIT_QUESTION = (
    "Should the new commit be a second commit on main or folded into the single "
    "release commit?"
)
_SETUP_QUESTION = (
    "When setup finishes with no agent connected, should the last line still say GO?"
)


@pytest.mark.parametrize(
    ("question", "archive", "answer"),
    [
        (
            _COMMIT_QUESTION,
            _UNRELATED_TO_THE_COMMIT_QUESTION,
            "Keep the new commit as a second commit on main; do not fold it into "
            "the single release commit.",
        ),
        (
            _SETUP_QUESTION,
            _UNRELATED_TO_THE_SETUP_QUESTION,
            "When setup finishes with no agent connected, the last line should "
            "still say GO, in amber rather than green.",
        ),
    ],
)
@pytest.mark.parametrize("vectors", [0, 1_000, 10_000])
def test_evidence_that_only_shares_stray_words_is_not_an_answer_at_any_size(
    conn, cfg, monkeypatch, question, archive, answer, vectors
) -> None:
    # The corpus-size floors decide which retrieval gates run; this one must
    # hold under every one of them, the tiny, the lexical and the calibrated.
    monkeypatch.setattr("varyz.retrieval.search.corpus_vectors", lambda _conn: vectors)
    for number, (title, body) in enumerate(archive):
        _note(conn, f"unrelated-{number}", title, body)
    tier = EvidenceTier(conn, cfg)
    request = _request(question, allow_escalation=False)
    assert lexically_grounded(conn, question)

    with monkeypatch.context() as unfloored:
        unfloored.setattr(
            "varyz.resolution.resolver.evidence_coverage", lambda *args, **kwargs: 1.0
        )
        # Without the floor this archive reproduces the bug it was built from.
        assert tier.try_resolve(request) is not None
    assert tier.try_resolve(request) is None

    answering = _note(conn, "the-answer", None, answer)
    resolved = tier.try_resolve(request)
    assert resolved is not None and resolved.tier == 2
    assert resolved.life_result is not None
    assert answering in {
        entry_id
        for typed in resolved.life_result.answers
        for entry_id in typed.provenance.entry_ids
    }


def test_a_question_only_stray_words_matched_goes_to_the_owner(conn, cfg) -> None:
    for number, (title, body) in enumerate(_UNRELATED_TO_THE_SETUP_QUESTION):
        _note(conn, f"unrelated-{number}", title, body)
    knowledge = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, knowledge, device_id="device:daemon")
    resolver = ResolutionResolver(
        (knowledge, EvidenceTier(conn, cfg)), escalation_queue=queue
    )

    result = resolver.resolve(_request(_SETUP_QUESTION))

    assert result.disposition == QueryDisposition.CLARIFICATION
    assert result.basis == AnswerBasis.ESCALATED
    assert result.escalation_id is not None
    assert result.answer is None


def test_evidence_coverage_counts_words_said_together_not_anywhere(conn) -> None:
    spread = _note(
        conn, "spread", None,
        "The invoice arrived on Monday. " + "Nothing else happened that week. " * 6
        + "The refund went through on Friday.",
    )
    together = _note(conn, "together", None, "The refund for that invoice came on Friday.")

    question = "Did the refund for the invoice come through?"
    assert evidence_coverage(conn, question, entry_id=together) == 1.0
    # Every word of it, but never within one window.
    assert evidence_coverage(conn, question, entry_id=spread) < 1.0
    assert evidence_coverage(
        conn, question, entry_id=spread, window_tokens=100
    ) == 1.0


def test_evidence_coverage_reads_through_the_index_stemmer_and_weights_names(
    conn,
) -> None:
    lena = _note(conn, "lena", None, "Lena asked for the launch draft by the 12th.")
    _note(conn, "promise", None, "I promise to water the plants.")
    finished = _note(conn, "finished", None, "The upload finished overnight.")

    # "promise" is the asker's word, not the record's; the name is the subject.
    assert evidence_coverage(conn, "What did I promise Lena?", entry_id=lena) == (
        pytest.approx(2 / 3)
    )
    assert evidence_coverage(
        conn, "What did I promise Lena?", entry_id=lena, name_weight=1.0
    ) == pytest.approx(1 / 2)
    # "finishes" and "finished" are one word to retrieval, so they are one here.
    assert evidence_coverage(conn, "When does the upload finish?", entry_id=finished) == 1.0
    # A word the archive never uses cannot count against any record.
    assert evidence_coverage(conn, "Did the grastelle upload finish?", entry_id=finished) == 1.0
    # A record that is not in the index holds nothing.
    assert evidence_coverage(conn, "When does the upload finish?", entry_id="missing") == 0.0


def test_evidence_coverage_reads_a_conversation_as_its_whole_window(conn) -> None:
    _note(conn, "physio", None, "The physio said to rest the knee for a week.")
    turns = [
        (False, "which database did we settle on for the billing service"),
        (True, "postgres, the migration tooling is better than what dynamo offers"),
    ]
    first = None
    for index, (authored, body) in enumerate(turns):
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=f"db-{index}",
                body=body,
                occurred_start=datetime(2026, 9, 20, 9, index, tzinfo=UTC),
                thread_id="db",
                authored_by_user=authored,
            ),
            source="ai_conversations",
        )
        first = first or entry_id
    build_windows(conn)
    window = conn.execute(
        "SELECT id FROM conversation_windows WHERE thread_id = 'db'"
    ).fetchone()["id"]
    question = "Which database did we pick for billing, postgres or dynamo?"

    # The question and its answer are two turns; the capsule quotes both.
    assert evidence_coverage(conn, question, entry_id=first, window_id=window) == 1.0
    # The first message alone never says which one.
    assert evidence_coverage(conn, question, entry_id=first) < 1.0
    # Words the archive uses elsewhere do not make this window evidence for them.
    assert evidence_coverage(
        conn, "What did the physio say about my knee?", entry_id=first, window_id=window
    ) == 0.0


class _Model:
    """A local model that puts every record at P(on topic) ``p``, or is down."""

    def __init__(self, p: float | None) -> None:
        self.p = p
        self.asked: list[str] = []

    def p_a(self, question, text: str, *, few_shot: bool = True) -> float:
        self.asked.append(text)
        if self.p is None:
            raise ClassifierUnavailable("qwen3.5:4b is not loaded")
        return self.p


def test_evidence_the_model_finds_off_topic_goes_to_the_owner(conn, cfg) -> None:
    # "kayak" is in no record, so it drops out of the coverage count, and a note
    # that says "pay" and "much" in one place passes the coverage check alone.
    electrician = _note(
        conn, "electrician", None, "Had to pay the electrician much more than the quote."
    )
    request = _request("How much did I pay for the kayak?", allow_escalation=False)
    assert evidence_coverage(conn, request.question, entry_id=electrician) == 1.0
    assert EvidenceTier(conn, cfg).try_resolve(request) is not None

    off_topic = _Model(0.02)
    checked = EvidenceTier(conn, cfg, verifier=EvidenceVerifier(off_topic))
    assert checked.try_resolve(request) is None
    assert off_topic.asked == [
        "Question: How much did I pay for the kayak? | Record: "
        "Had to pay the electrician much more than the quote."
    ]

    on_topic = EvidenceTier(conn, cfg, verifier=EvidenceVerifier(_Model(0.97)))
    resolved = on_topic.try_resolve(request)
    assert resolved is not None and resolved.tier == 2

    # A model that cannot answer leaves the coverage check to decide, as before.
    down = EvidenceVerifier(_Model(None))
    assert EvidenceTier(conn, cfg, verifier=down).try_resolve(request) is not None
    assert down.skipped


def test_the_model_is_not_asked_about_evidence_the_coverage_check_refused(conn, cfg) -> None:
    for number, (title, body) in enumerate(_UNRELATED_TO_THE_SETUP_QUESTION):
        _note(conn, f"unrelated-{number}", title, body)
    model = _Model(0.97)
    tier = EvidenceTier(conn, cfg, verifier=EvidenceVerifier(model))
    assert tier.try_resolve(_request(_SETUP_QUESTION, allow_escalation=False)) is None
    assert model.asked == []


def _archive_capsule(reference: str) -> Capsule:
    return Capsule(
        text=f"The record {reference}.",
        source="manual",
        kind="note",
        title=None,
        occurred="2026-09-01T10:00:00+00:00",
        external_id=reference,
        entry_id=f"entry-{reference}",
        score=0.9,
        sensitivity="personal",
        authored_by_user=True,
    )


class _Judge:
    def __init__(self) -> None:
        self.is_ready = True
        self.warmed = 0

    def ready(self) -> bool:
        return self.is_ready

    def warm(self) -> None:
        self.warmed += 1


class _Models:
    def __init__(self, judge: _Judge | None) -> None:
        self.judge = judge

    def get(self):
        return None if self.judge is None else ("embedder", "reranker", self.judge)


def test_tier_two_answers_from_the_records_the_local_model_chose(conn, cfg, monkeypatch) -> None:
    asked: list[object] = []
    judged = [True]

    def scripted_recall(_conn, _cfg, question, **kwargs):
        asked.append(kwargs.get("judge"))
        if asked[-1] is None:
            return Evidence(query=question, capsules=[_archive_capsule("keyword-hit")])
        return Evidence(query=question, capsules=[_archive_capsule("chosen")], judged=judged[0])

    monkeypatch.setattr("varyz.resolution.resolver.recall", scripted_recall)
    monkeypatch.setattr(
        "varyz.resolution.resolver.lexically_grounded", lambda *args, **kwargs: True
    )
    # No record says the question in one place: keyword evidence goes to the owner.
    monkeypatch.setattr(
        "varyz.resolution.resolver.evidence_coverage", lambda *args, **kwargs: 0.0
    )
    request = _request("Why did we choose Postgres for Beacon?", allow_escalation=False)
    assert EvidenceTier(conn, cfg).try_resolve(request) is None
    assert EvidenceTier(conn, cfg, models=_Models(None)).try_resolve(request) is None

    judge = _Judge()
    tier = EvidenceTier(conn, cfg, models=_Models(judge))
    resolved = tier.try_resolve(request)
    assert resolved is not None and resolved.tier == 2
    assert "[manual: chosen]" in (resolved.answer or "")
    assert asked[-1] is judge

    # A judge that failed leaves the cross-encoder's cut; keywords answer instead.
    asked.clear()
    judged[0] = False
    assert tier.try_resolve(request) is None
    assert asked == [judge, None]

    # A judge still loading is not waited for, and loads for the next question.
    asked.clear()
    judge.is_ready = False
    assert tier.try_resolve(request) is None
    assert asked == [None]
    assert judge.warmed == 1


def test_retrieval_models_load_nothing_without_a_judge(cfg, monkeypatch) -> None:
    import varyz.retrieval.loaded_models as loaded_models

    class _Reranker:
        loads = 0

        def load(self) -> None:
            self.loads += 1

    reranker = _Reranker()
    judges: list[object] = [None]
    asked: list[str] = []

    def load_judge(_cfg):
        asked.append("judge")
        return judges[0]

    monkeypatch.setattr("varyz.retrieval.rerank.load_reranker", lambda _cfg: reranker)
    monkeypatch.setattr("varyz.retrieval.judge.load_judge", load_judge)
    monkeypatch.setattr("varyz.pipeline.embed.Embedder", lambda _cfg: "embedder")
    models = loaded_models.RetrievalModels(cfg)
    assert models.wait(10) is None
    assert models.get() is None
    # Not asked again until RECHECK_SECONDS have passed.
    assert asked == ["judge"]
    assert reranker.loads == 0

    judges[0] = _Judge()
    monkeypatch.setattr(loaded_models, "RECHECK_SECONDS", 0.0)
    try:
        # The question that finds them unloaded does not wait while they load.
        assert models.get() is None
        assert models.wait(10) == ("embedder", reranker, judges[0])
        assert models.get() == ("embedder", reranker, judges[0])
        assert reranker.loads == 1
        models.release()
        assert models._loaded is None
    finally:
        models.release()


def test_other_requests_go_on_while_tier_two_reads(cfg, monkeypatch) -> None:
    import threading

    both_reading = threading.Event()
    finish = threading.Event()
    read_through: list[sqlite3.Connection] = []

    def slow_recall(conn, _cfg, question, **kwargs):
        read_through.append(conn)
        if len(read_through) == 2:
            both_reading.set()
        assert finish.wait(10)
        return Evidence(query=question, abstained=True)

    monkeypatch.setattr("varyz.resolution.resolver.recall", slow_recall)
    monkeypatch.setattr("varyz.resolution.resolver.lexically_grounded", lambda *a, **k: True)
    daemon = build_resolution_daemon(
        cfg.db_path, config=cfg, host="127.0.0.1", port=0, device_id="device:threads"
    )
    answers: list[Resolution] = []
    with daemon:
        host, port = daemon.address

        def ask(question: str) -> None:
            client = ResolutionClient(host, port)
            answers.append(client.resolve(_request(question, allow_escalation=False)))

        agents = [
            threading.Thread(target=ask, args=(question,))
            for question in ("What did we decide about the renderer?", "Who owns billing?")
        ]
        for agent in agents:
            agent.start()
        try:
            # Two agents' questions are read at once, each through a connection
            # of its own, and neither holds the daemon's turn with its own.
            assert both_reading.wait(10)
            assert daemon._owned_connection not in read_through
            assert read_through[0] is not read_through[1]
            assert daemon.service.lock.acquire(blocking=False)
            daemon.service.lock.release()
            assert ResolutionClient(host, port, timeout=5.0).status().running
        finally:
            finish.set()
            for agent in agents:
                agent.join(10)
    assert [answer.disposition for answer in answers] == [QueryDisposition.NOT_KNOWN] * 2
    # Their connections are closed once the questions are answered.
    for connection in read_through:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_tiers_on_the_shared_connection_take_turns(conn) -> None:
    import threading

    inside = threading.Event()
    finish = threading.Event()

    class SharedTier:
        tier = 2

        def try_resolve(self, request):
            inside.set()
            assert finish.wait(10)

    class OwnTier(SharedTier):
        own_connection = True

    for tier, holds in ((SharedTier(), True), (OwnTier(), False)):
        inside.clear()
        finish.clear()
        resolver = ResolutionResolver((tier,))
        asking = threading.Thread(
            target=resolver.resolve, args=(_request("Anything?", allow_escalation=False),)
        )
        asking.start()
        try:
            assert inside.wait(10)
            free = resolver.lock.acquire(blocking=False)
            if free:
                resolver.lock.release()
            assert free is not holds
        finally:
            finish.set()
            asking.join(10)
