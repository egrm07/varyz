"""Forward-only seams for answers that are not only the local owner's.

Authorship and origin on owner knowledge, org/published scopes below the owner's
own answers, the append-only outcome ledger, and routed escalation recipients.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from varyz.db.connection import _iter_migrations, connect, migrate
from varyz.mcp_server import VaryzServer
from varyz.personal_model.query import AnswerBasis, QueryDisposition
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.escalation import NullNotifier, SQLiteEscalationQueue
from varyz.resolution.knowledge import (
    KNOWLEDGE_GENERATION,
    OWNER_ID,
    KnowledgeEntry,
    SQLiteKnowledgeStore,
    knowledge_provenance,
    normalize_scope,
    scope_applies,
)
from varyz.resolution.models import Caller, ResolutionRequest
from varyz.resolution.outcomes import SQLiteOutcomeLedger
from varyz.resolution.ports import Notifier
from varyz.resolution.resolver import ResolutionResolver, SQLiteResolutionLedger

QUESTION = "Which licence should new libraries use?"
STAMP = "2026-09-24T00:00:00+00:00"


def _caller() -> Caller:
    return Caller(
        agent_id="agent:test",
        display_name="Test agent",
        purpose="coding",
        device_id="device:agent",
    )


def _request(
    question: str = QUESTION, *, project: str | None = None, allow_escalation: bool = False
) -> ResolutionRequest:
    return ResolutionRequest(
        question=question,
        caller=_caller(),
        purpose="coding",
        project=project,
        allow_escalation=allow_escalation,
    )


def _remember(
    store: SQLiteKnowledgeStore,
    answer: str,
    *,
    question: str = QUESTION,
    scope: str = "*",
    origin: str = "own",
    author_id: str = OWNER_ID,
    supersedes_id: str | None = None,
) -> str:
    return store.remember(
        question=question,
        answer=answer,
        caller=_caller(),
        basis=AnswerBasis.EXPLICIT,
        source="manual",
        device_id="device:owner",
        supersedes_id=supersedes_id,
        scope=scope,
        origin=origin,
        author_id=author_id,
    )


def _schema_before_0038(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.execute(
        """CREATE TABLE schema_migrations
           (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
    )
    for version, _name, sql in _iter_migrations():
        if version > 37:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, STAMP),
        )
    conn.commit()
    return conn


# -- Seam 1: authorship and origin --------------------------------------------------


def test_0038_defaults_existing_knowledge_and_escalations_to_the_local_owner(
    tmp_path: Path,
) -> None:
    conn = _schema_before_0038(tmp_path / "schema-v37.db")
    try:
        conn.execute(
            """INSERT INTO owner_knowledge
               (id,question,normalized_question,answer,basis,source,
                caller_agent_id,caller_display_name,caller_purpose,caller_transport,
                device_id,monotonic_seq,stated_at,created_at,scope)
               VALUES ('old','Question?','question','Answer','explicit','manual',
                       'owner:local','Local owner','owner_knowledge','stdio',
                       'device:legacy',1,?,?,'*')""",
            (STAMP, STAMP),
        )
        conn.execute(
            """INSERT INTO resolution_escalations
               (id, question, normalized_question, caller_agent_id, caller_display_name,
                caller_purpose, caller_device_id, caller_transport, purpose,
                tried_tiers_json, urgency, status, created_device_id,
                created_monotonic_seq, created_at)
               VALUES ('esc-old', 'Question?', 'question', 'agent:test', 'Test agent',
                       'coding', 'device:agent', 'stdio', 'coding', '[1]', 'normal', 'pending',
                       'device:legacy', 2, ?)""",
            (STAMP,),
        )
        conn.commit()

        assert migrate(conn)[0] == "0038_authorship_outcomes_recipients.sql"

        entry = SQLiteKnowledgeStore(conn).get("old")
        assert entry is not None
        assert (entry.author_id, entry.origin) == (OWNER_ID, "own")
        queue = SQLiteEscalationQueue(conn, SQLiteKnowledgeStore(conn))
        [pending] = queue.pending()
        assert pending["recipient_id"] == OWNER_ID
        assert queue.recipient("esc-old") == OWNER_ID
        # The owner-knowledge ledger stays append-only through the new columns.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE owner_knowledge SET origin = 'org' WHERE id = 'old'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                """INSERT INTO owner_knowledge
                   (id,question,normalized_question,answer,basis,source,
                    caller_agent_id,caller_display_name,caller_purpose,caller_transport,
                    device_id,monotonic_seq,stated_at,created_at,origin)
                   VALUES ('bad','Q?','q','A','explicit','manual','owner:local','Owner',
                           'owner_knowledge','stdio','device:legacy',9,?,?,'stranger')""",
                (STAMP, STAMP),
            )
        conn.rollback()
    finally:
        conn.close()


def test_authorship_round_trips_and_old_payloads_default_to_the_owner(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    entry_id = _remember(store, "MIT.", scope="org:Acme", origin="org", author_id="org:acme")
    entry = store.get(entry_id)
    assert entry is not None
    assert (entry.scope, entry.origin, entry.author_id, entry.own) == (
        "org:acme", "org", "org:acme", False
    )
    payload = entry.to_dict()
    assert (payload["author_id"], payload["origin"]) == ("org:acme", "org")
    assert KnowledgeEntry.from_dict(payload) == entry

    old = {key: value for key, value in payload.items() if key not in {"author_id", "origin"}}
    old["scope"] = "*"
    restored = KnowledgeEntry.from_dict(old)
    assert (restored.author_id, restored.origin) == (OWNER_ID, "own")


def test_origin_must_agree_with_scope_and_blank_authors_are_refused(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    assert normalize_scope("Org:  ACME") == "org:acme"
    assert normalize_scope("published:Dieter") == "published:dieter"
    for empty in ("org:", "published: ", "org:*"):
        with pytest.raises(ValueError, match="needs an id"):
            normalize_scope(empty)
    with pytest.raises(ValueError, match="disagree"):
        _remember(store, "MIT.", scope="*", origin="org")
    with pytest.raises(ValueError, match="disagree"):
        _remember(store, "MIT.", scope="org:acme")
    with pytest.raises(ValueError, match="origin must be"):
        _remember(store, "MIT.", origin="team")
    with pytest.raises(ValueError, match="author_id"):
        _remember(store, "MIT.", author_id="  ")
    assert store.current() == ()


def test_imported_answers_never_replace_the_owners_own(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    own = _remember(store, "Apache-2.0.")
    with pytest.raises(ValueError, match="cannot supersede"):
        _remember(
            store, "MIT.", scope="published:dieter", origin="published", supersedes_id=own
        )
    assert [entry.id for entry in store.current()] == [own]
    # The owner may replace imported taste with an answer of their own.
    org = _remember(store, "BSD.", question="Which licence for tools?", scope="org:acme",
                    origin="org")
    replaced = _remember(store, "MIT.", question="Which licence for tools?", supersedes_id=org)
    assert org not in {entry.id for entry in store.current()}
    assert replaced in {entry.id for entry in store.current()}


# -- Seam 2: org and published precedence ------------------------------------------


def test_precedence_is_own_project_then_general_then_org_then_published(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    project = _remember(store, "AGPL for persona.", scope="persona")
    general = _remember(store, "Apache-2.0.")
    org = _remember(store, "MIT at Acme.", scope="org:acme", origin="org", author_id="org:acme")
    _remember(
        store, "Dieter's pick: BSD.", scope="published:dieter", origin="published",
        author_id="creator:dieter",
    )

    def resolve(project_name: str | None = "persona") -> tuple[str | None, dict | None]:
        result = store.try_resolve(_request(project=project_name))
        assert result is not None
        return result.answer, knowledge_provenance(result)

    answer, provenance = resolve()
    assert answer == "AGPL for persona."
    assert provenance == {
        "knowledge_id": project, "author_id": OWNER_ID, "origin": "own", "scope": "persona"
    }
    # A project's answer never answers for another project.
    assert resolve("memoir")[0] == "Apache-2.0."

    store.retract(project, caller=_caller(), device_id="device:owner")
    assert resolve()[0] == "Apache-2.0."
    store.retract(general, caller=_caller(), device_id="device:owner")
    answer, provenance = resolve()
    assert answer == "MIT at Acme."
    assert provenance is not None and provenance["origin"] == "org"
    assert provenance["author_id"] == "org:acme"
    assert "not the owner's own" in provenance["label"]

    store.retract(org, caller=_caller(), device_id="device:owner")
    result = store.try_resolve(_request(project="persona"))
    assert result is not None and result.answer == "Dieter's pick: BSD."
    # Published taste is reported, never the owner's explicit word.
    assert result.basis == AnswerBasis.REPORTED
    provenance = knowledge_provenance(result)
    assert provenance is not None
    assert provenance["origin"] == "published"
    assert provenance["author_id"] == "creator:dieter"
    assert provenance["label"] == (
        "Published taste from published:dieter, not the owner's own answer"
    )


def test_the_owners_paraphrased_answer_beats_an_exact_imported_one(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    question = "Which package manager should Python projects use?"
    _remember(store, "pip-tools.", question=question, scope="org:acme", origin="org")
    _remember(store, "uv.", question="Which package manager do Python projects use?")

    result = store.try_resolve(_request(question))
    assert result is not None
    assert result.answer == "uv."
    assert result.basis == AnswerBasis.EXPLICIT
    assert result.confidence == 0.8
    provenance = knowledge_provenance(result)
    assert provenance is not None and provenance["origin"] == "own"
    assert "label" not in provenance


def test_ambiguous_own_answers_escalate_instead_of_falling_through(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    question = "Which package manager should Python projects use?"
    _remember(store, "uv.", question="Which package manager do Python projects use?")
    _remember(store, "pip.", question="Package manager for Python projects?")
    _remember(store, "conda.", question=question, scope="published:dieter", origin="published")

    assert store.try_resolve(_request(question)) is None


def test_project_answers_stay_isolated_even_with_imported_fallbacks(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _remember(store, "Teal.", question="What accent colour for the site?", scope="persona")
    _remember(
        store, "Orange.", question="What accent colour for the site?",
        scope="published:dieter", origin="published",
    )

    memoir = store.try_resolve(_request("What accent colour for the site?", project="memoir"))
    assert memoir is not None and memoir.answer == "Orange."
    assert memoir.basis == AnswerBasis.REPORTED
    persona = store.try_resolve(_request("What accent colour for the site?", project="persona"))
    assert persona is not None and persona.answer == "Teal."


def test_imported_scopes_never_join_the_owners_way_of_working() -> None:
    assert scope_applies("*", None, QUESTION)
    assert not scope_applies("org:acme", "acme", "What does org:acme use?")
    assert not scope_applies("published:dieter", None, "published:dieter")


# -- Seam 3: resolution outcomes ----------------------------------------------------


def _ledger_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row["id"] for row in conn.execute(
            "SELECT id FROM resolution_ledger ORDER BY monotonic_seq"
        )
    ]


def test_outcomes_are_append_only_device_stamped_and_queryable(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    _remember(store, "Apache-2.0.")
    resolver = ResolutionResolver(
        (store,), ledger=SQLiteResolutionLedger(conn, device_id="device:daemon")
    )
    resolver.resolve(_request())
    [ledger_id] = _ledger_ids(conn)
    outcomes = SQLiteOutcomeLedger(conn, device_id="device:daemon")

    first = outcomes.record(ledger_id, "adopted", source="test")
    second = outcomes.record(ledger_id, "corrected", source="test", device_id="device:owner")

    recorded = outcomes.for_resolution(ledger_id)
    assert [item.id for item in recorded] == [first, second]
    assert [item.outcome for item in recorded] == ["adopted", "corrected"]
    assert recorded[0].device_id == "device:daemon"
    assert recorded[0].recorded_at.utcoffset() is not None
    assert recorded[0].to_dict()["recorded_at"].endswith("+00:00")
    daemon_seq = conn.execute(
        "SELECT last_seq FROM resolution_device_sequences WHERE device_id = 'device:daemon'"
    ).fetchone()[0]
    # The per-device sequence is shared with the ledger it annotates.
    assert recorded[0].monotonic_seq == daemon_seq
    assert [item.id for item in outcomes.query(outcome="corrected")] == [second]
    assert outcomes.query(source="elsewhere") == ()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE resolution_outcomes SET outcome = 'overridden'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM resolution_outcomes")
    conn.rollback()
    with pytest.raises(ValueError, match="outcome must be"):
        outcomes.record(ledger_id, "ignored", source="test")
    with pytest.raises(ValueError, match="source"):
        outcomes.record(ledger_id, "adopted", source=" ")
    with pytest.raises(KeyError, match="unknown resolution"):
        outcomes.record("missing", "adopted", source="test")
    assert len(outcomes.query()) == 2


def test_superseding_an_answer_marks_what_it_answered_overridden(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    first = _remember(store, "Apache-2.0.")
    _remember(store, "Yes.", question="Should tests run offline?")
    resolver = ResolutionResolver(
        (store,), ledger=SQLiteResolutionLedger(conn, device_id="device:daemon")
    )
    resolver.resolve(_request())
    resolver.resolve(_request("which licence should new libraries use"))
    resolver.resolve(_request("Should tests run offline?"))
    answered, paraphrased, unrelated = _ledger_ids(conn)
    citations = json.loads(
        conn.execute(
            "SELECT citations_json FROM resolution_ledger WHERE id = ?", (answered,)
        ).fetchone()[0]
    )
    assert citations == [
        {"generation_id": KNOWLEDGE_GENERATION, "record_id": first,
         "entry_ids": [], "claim_ids": []}
    ]

    correction = _remember(store, "MIT.", supersedes_id=first)

    outcomes = SQLiteOutcomeLedger(conn)
    overridden = outcomes.query(outcome="overridden")
    assert [item.ledger_id for item in overridden] == [answered, paraphrased]
    assert {item.knowledge_id for item in overridden} == {correction}
    assert {item.source for item in overridden} == {"supersession"}
    assert {item.device_id for item in overridden} == {"device:owner"}
    assert outcomes.for_resolution(unrelated) == ()
    assert resolver.resolve(_request()).answer == "MIT."


def test_a_refused_supersession_records_no_outcome(conn) -> None:
    store = SQLiteKnowledgeStore(conn)
    first = _remember(store, "Apache-2.0.")
    ResolutionResolver((store,), ledger=SQLiteResolutionLedger(conn)).resolve(_request())
    with pytest.raises(ValueError, match="cannot supersede"):
        _remember(store, "MIT.", scope="org:acme", origin="org", supersedes_id=first)
    assert SQLiteOutcomeLedger(conn).query() == ()


# -- Seam 4: escalation recipients --------------------------------------------------


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def notify(self, escalation_id: str, urgency: str) -> None:
        self.calls.append((escalation_id, urgency))


def test_questions_default_to_the_owner_and_can_be_routed_to_someone_else(conn) -> None:
    knowledge = SQLiteKnowledgeStore(conn)
    notifier = RecordingNotifier()
    queue = SQLiteEscalationQueue(conn, knowledge, notifier=notifier, device_id="device:daemon")

    owners = queue.enqueue(_request(), tried_tiers=(1,), urgency="normal")
    routed = queue.enqueue(
        _request(), tried_tiers=(1,), urgency="normal", recipient_id="person:ana"
    )
    assert owners != routed
    # The same question for the same person is still one question.
    assert queue.enqueue(
        _request(), tried_tiers=(1,), urgency="normal", recipient_id="person:ana"
    ) == routed
    assert queue.recipient(owners) == OWNER_ID
    assert queue.recipient(routed) == "person:ana"
    assert queue.recipient("missing") is None
    assert [item["recipient_id"] for item in queue.pending()] == [OWNER_ID, "person:ana"]
    assert [item["id"] for item in queue.pending("person:ana")] == [routed]
    with pytest.raises(ValueError, match="recipient_id"):
        queue.enqueue(_request(), tried_tiers=(1,), urgency="normal", recipient_id=" ")

    # The notifier hears only an id and an urgency: never the question or its recipient.
    assert notifier.calls == [(owners, "normal"), (routed, "normal")]
    assert tuple(inspect.signature(Notifier.notify).parameters) == (
        "self", "escalation_id", "urgency"
    )

    answered = queue.answer(routed, "MIT.", device_id="device:ana")
    [entry] = knowledge.current()
    assert (entry.author_id, entry.origin) == ("person:ana", "own")
    assert knowledge_provenance(answered) == {
        "knowledge_id": entry.id, "author_id": "person:ana", "origin": "own", "scope": "*"
    }
    assert answered.escalation_id == routed
    assert queue.poll(routed) == answered
    # One person's answer does not close the question routed to another.
    assert [item["id"] for item in queue.pending()] == [owners]


# -- Surfaces: the daemon and the agent's MCP payload --------------------------------


@pytest.fixture
def daemon_client(tmp_path: Path) -> Iterator[ResolutionClient]:
    daemon = build_resolution_daemon(
        tmp_path / "data" / "varyz.db",
        host="127.0.0.1",
        port=0,
        device_id="device:test",
        notifier=NullNotifier(),
    )
    try:
        with daemon:
            host, port = daemon.address
            yield ResolutionClient(host, port)
    finally:
        daemon.stop()


@pytest.fixture
def server(tmp_path: Path, daemon_client: ResolutionClient) -> Iterator[VaryzServer]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    server = VaryzServer(config, agent_token="", resolution_client=daemon_client)
    try:
        yield server
    finally:
        server.close()


def test_agents_are_told_who_an_answer_came_from(
    server: VaryzServer, daemon_client: ResolutionClient
) -> None:
    owner = Caller(
        agent_id=OWNER_ID, display_name="Owner", purpose="owner_knowledge",
        device_id="device:test",
    )
    daemon_client.remember(
        question=QUESTION, answer="BSD.", caller=owner, device_id="device:test",
        scope="published:dieter", origin="published", author_id="creator:dieter",
    )
    published = server.call("resolve", {"question": QUESTION})
    assert published["disposition"] == QueryDisposition.ANSWERED.value
    assert published["basis"] == "reported"
    assert published["answered_by"]["origin"] == "published"
    assert published["answered_by"]["label"].startswith("Published taste")
    [item] = daemon_client.knowledge()
    assert (item["author_id"], item["origin"]) == ("creator:dieter", "published")

    daemon_client.remember(
        question=QUESTION, answer="Apache-2.0.", caller=owner, device_id="device:test"
    )
    own = server.call("resolve", {"question": QUESTION})
    assert own["answer"] == "Apache-2.0."
    assert own["answered_by"]["origin"] == "own"
    assert own["answered_by"]["author_id"] == OWNER_ID
    assert "label" not in own["answered_by"]
