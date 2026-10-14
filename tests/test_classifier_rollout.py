"""The local model where it replaced a wording rule: the hook's work-or-question
call, the stop gate's small-change skip, rework, the scope of an owner answer, the
second look at a reworded ask, and the look at archive passages before tier two
answers with them.

Offline: every model and embedding is a fake. The measured accuracy and latency
live in scripts/classifier_rollout_bench.py and
reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md, not here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tests import test_workflow as wf
from varyz import hooks
from varyz.config import Config, WorkflowsConfig
from varyz.db.connection import open_db
from varyz.personal_model.query import AnswerBasis
from varyz.resolution import evidence_verify, paraphrase_verify, prompt_kind, scope, task_size
from varyz.resolution import local_classifier as local_classifier_module
from varyz.resolution.evidence_verify import EvidenceVerifier
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.local_classifier import (
    BinaryClassifier,
    ClassifierUnavailable,
    LocalModel,
    Verdict,
)
from varyz.resolution.models import Caller, ResolutionRequest
from varyz.resolution.paraphrase_verify import ParaphraseVerifier
from varyz.resolution.scope import GLOBAL_SCOPE, suggest_scope
from varyz.resolution.workflows import Sizing

# Captured before the suite's autouse fixture pins them to None.
REAL_PROMPT_CLASSIFIER = prompt_kind.prompt_classifier_from_config
REAL_SCOPE_MODEL = scope.scope_model_from_config
REAL_VERIFIER = paraphrase_verify.paraphrase_verifier_from_config
REAL_EVIDENCE_VERIFIER = evidence_verify.evidence_verifier_from_config
REAL_GATE_SIZER = task_size.gate_sizer_from_config
REAL_SIZER = task_size.sizer_from_config


class _Model:
    """P(A) from ``answer(text)``, or unavailable. Records what it was asked."""

    def __init__(self, answer: Callable[[str], float] | None = None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def p_a(self, question: Any, text: str, *, few_shot: bool = True) -> float:
        self.asked.append(text)
        if self.answer is None:
            raise ClassifierUnavailable("qwen3.5:4b is not loaded")
        return self.answer(text)


# -- work or question ----------------------------------------------------------


def test_the_classifier_decides_work_or_question_once_the_prompt_is_long_enough() -> None:
    says_work = BinaryClassifier(
        prompt_kind.WORK_QUESTION, lambda text: False, model=_Model(lambda text: 0.9)
    )
    says_question = BinaryClassifier(
        prompt_kind.WORK_QUESTION, lambda text: True, model=_Model(lambda text: 0.1)
    )
    # The rule reads "can you…?" as a question; the model reads it as a request.
    assert hooks.is_task("can you rename the users endpoint to accounts?", says_work)
    assert not hooks.is_task("Build a landing page for the waitlist", says_question)
    # Too short to be a task, or written by the client: never asked.
    assert not hooks.is_task("do it", says_work)
    assert not hooks.is_task("<task-notification> finished the background build now", says_work)
    assert says_work.model.asked == ["can you rename the users endpoint to accounts?"]


def test_a_cold_model_leaves_work_or_question_to_the_rule() -> None:
    cold = BinaryClassifier(
        prompt_kind.WORK_QUESTION, lambda text: not hooks.is_question(text), model=_Model()
    )
    assert hooks.is_task("Build a landing page for the waitlist", cold)
    assert not hooks.is_task("why is the website build so slow on windows", cold)
    assert cold.skipped


def test_the_prompt_classifier_follows_config() -> None:
    cfg = Config()
    cfg.workflows.prompt_classifier = "rule"
    assert REAL_PROMPT_CLASSIFIER(cfg) is None
    cfg.workflows.prompt_classifier = "model"
    chain = REAL_PROMPT_CLASSIFIER(cfg)
    assert isinstance(chain, BinaryClassifier)
    # The hook never waits for a load; the embedding costs more than it earns there.
    assert chain.model.when_cold == "warm"
    assert chain.centroids is None


# -- the stop gate skips a small change ----------------------------------------


@pytest.fixture
def gate(
    tmp_path: Path, config_path: Path, db: sqlite3.Connection
) -> Iterator[tuple[Any, dict[str, str], str]]:
    wf._website_owner(db)
    cfg = wf._cfg(config_path)
    event = {"session_id": "g1", "cwd": str(tmp_path / "persona")}
    hooks.user_prompt(cfg, {**event, "prompt": "Build the landing page for persona"})
    edited = wf._transcript(tmp_path / "edit.jsonl", wf.PROMPT, wf.EDIT, wf.RESULT)
    yield cfg, event, edited


# Borrowed from test_workflow so the gate fixture can build the owner.
config_path = wf.config_path
db = wf.db


class _Sizer:
    def __init__(self, size: str | None) -> None:
        self.size = size
        self.calls = 0

    def __call__(self, task: str, steps: Any) -> Sizing:
        self.calls += 1
        if self.size is None:
            raise ClassifierUnavailable("qwen3.5:4b is not loaded")
        return Sizing(self.size, "model")


def test_a_small_change_the_loaded_model_names_skips_the_done_gate(
    monkeypatch: pytest.MonkeyPatch, gate: tuple[Any, dict[str, str], str]
) -> None:
    cfg, event, edited = gate
    sizer = _Sizer("small")
    monkeypatch.setattr(task_size, "gate_sizer_from_config", lambda cfg: sizer)
    assert hooks.stop(cfg, {**event, "transcript_path": edited}) is None
    assert hooks.stop(cfg, {**event, "transcript_path": edited}) is None
    assert sizer.calls == 1  # asked once per task, then remembered


@pytest.mark.parametrize("size", ["new", None])
def test_new_work_or_a_cold_model_still_gets_the_done_gate(
    monkeypatch: pytest.MonkeyPatch, gate: tuple[Any, dict[str, str], str], size: str | None
) -> None:
    cfg, event, edited = gate
    monkeypatch.setattr(task_size, "gate_sizer_from_config", lambda cfg: _Sizer(size))
    blocked = hooks.stop(cfg, {**event, "transcript_path": edited})
    assert blocked is not None and blocked["decision"] == "block"


def test_the_owner_can_keep_the_done_gate_on_small_changes(
    monkeypatch: pytest.MonkeyPatch, gate: tuple[Any, dict[str, str], str]
) -> None:
    cfg, event, edited = gate
    cfg.agent.done_gate_small_changes = "check"
    sizer = _Sizer("small")
    monkeypatch.setattr(task_size, "gate_sizer_from_config", lambda cfg: sizer)
    assert hooks.stop(cfg, {**event, "transcript_path": edited})["decision"] == "block"
    assert sizer.calls == 0


def test_the_gate_sizer_is_the_model_alone_and_never_loads_it() -> None:
    cfg = Config()
    cfg.workflows.task_classifier = "embedding"
    assert REAL_GATE_SIZER(cfg) is None
    cfg.workflows.task_classifier = "model"
    sizer = REAL_GATE_SIZER(cfg)
    assert sizer is not None and sizer.when_cold == "skip"


# -- rework ----------------------------------------------------------------------


def test_the_redo_chain_is_the_model_then_the_verb_list() -> None:
    cfg = Config()
    cfg.workflows.redo_classifier = "rule"
    assert task_size.redo_classifier_from_config(cfg) is None
    cfg.workflows.redo_classifier = "model"
    chain = task_size.redo_classifier_from_config(cfg, model=_Model(lambda text: 0.8))
    assert chain is not None and chain.centroids is None
    assert chain("the screens need another pass from the top").is_a
    cold = task_size.redo_classifier_from_config(cfg, model=_Model())
    assert cold("redo the logo, it feels generic") == Verdict(True, "rule")
    assert cold("make the logo") == Verdict(False, "rule")


# -- scope of an owner answer ------------------------------------------------------


def test_the_model_widens_scope_only_when_sure() -> None:
    question, answer = "Which analytics?", "Plausible."
    sure = suggest_scope(question, answer, "lumen", model=_Model(lambda text: 0.95))
    assert (sure.scope, sure.confident) == (GLOBAL_SCOPE, True)
    unsure = suggest_scope(question, answer, "lumen", model=_Model(lambda text: 0.7))
    assert (unsure.scope, unsure.confident) == ("lumen", False)
    assert "fail narrow" in unsure.reason
    one_off = suggest_scope(question, answer, "lumen", model=_Model(lambda text: 0.02))
    assert (one_off.scope, one_off.confident) == ("lumen", True)


def test_explicit_wording_decides_scope_before_the_model() -> None:
    model = _Model(lambda text: 0.99)
    named = suggest_scope("Colour for lumen?", "#6d5efc", "lumen", model=model)
    assert named.scope == "lumen"
    general = suggest_scope("Font?", "Inter, in all my projects.", "lumen", model=_Model())
    assert general.scope == GLOBAL_SCOPE
    assert model.asked == []


def test_a_cold_scope_model_falls_back_to_the_wording() -> None:
    cold = suggest_scope("Should websites get a favicon?", "Always, in all my projects.",
                         "lumen", model=_Model())
    assert cold.scope == GLOBAL_SCOPE
    assert suggest_scope("Which analytics?", "Plausible.", "lumen", model=_Model()).scope == (
        "lumen"
    )


def test_the_scope_model_follows_config() -> None:
    cfg = Config()
    cfg.workflows.scope_classifier = "rule"
    assert REAL_SCOPE_MODEL(cfg) is None
    cfg.workflows.scope_classifier = "model"
    assert isinstance(REAL_SCOPE_MODEL(cfg), LocalModel)


# -- the second look at a reworded ask ---------------------------------------------

VOCABULARY = ("database", "data", "records", "postgres", "host", "cloud", "fly", "kestrel",
              "lumen", "store", "keep")


def _embed(texts: list[str]) -> list[list[float]]:
    return [
        [float(word in text.casefold()) for word in VOCABULARY] + [0.01] for text in texts
    ]


def _answers_if(*words: str) -> Callable[[str], float]:
    """P(answers) high when the stored decision in the pair names any of ``words``."""

    def p(pair: str) -> float:
        decision = pair.split(" | New question:", 1)[0].casefold()
        return 0.97 if any(word in decision for word in words) else 0.05

    return p


def test_the_verifier_answers_only_on_a_single_clear_yes() -> None:
    candidates = [
        ("k1", "Which database does Kestrel use?", "Postgres on Supabase."),
        ("k2", "Which cloud provider should host apps?", "Fly.io."),
        ("k3", "Which database for Lumen?", "SQLite."),
    ]
    one = ParaphraseVerifier(_Model(_answers_if("kestrel")), _embed)
    assert one.pick("Where does Kestrel keep its records?", candidates) == "k1"
    assert [key for key, _ in one.last][0] == "k1"
    two = ParaphraseVerifier(_Model(_answers_if("database")), _embed)
    assert two.pick("Where does Kestrel keep its records?", candidates) is None
    below = ParaphraseVerifier(_Model(lambda pair: 0.85), _embed)
    assert below.pick("Where does Kestrel keep its records?", candidates) is None
    cold = ParaphraseVerifier(_Model(), _embed)
    assert cold.pick("Where does Kestrel keep its records?", candidates) is None
    assert cold.skipped
    assert one.pick("anything", []) is None


def test_the_verifier_asks_only_about_the_nearest_few() -> None:
    model = _Model(lambda pair: 0.05)
    verifier = ParaphraseVerifier(model, _embed, top_k=2)
    candidates = [(f"k{i}", f"Question {i} about lumen", "x") for i in range(6)]
    candidates.append(("db", "Which database does Kestrel use?", "Postgres."))
    verifier.pick("Which database holds Kestrel records?", candidates)
    assert len(model.asked) == 2
    assert "Kestrel use?" in model.asked[0]


def test_verified_duplicate_answers_are_not_conflicting_decisions() -> None:
    verifier = ParaphraseVerifier(_Model(lambda pair: 0.97), _embed)
    candidates = [
        ("original", "Which database for Kestrel?", "SQLite."),
        ("imported", "What database does Kestrel use?", "SQLite."),
    ]
    assert verifier.pick("Which database for Kestrel?", candidates) in {"original", "imported"}
    candidates[1] = ("conditional", "Which database for Kestrel?", "SQLite, if offline.")
    assert verifier.pick("Which database for Kestrel?", candidates) is None
    candidates[1] = ("conditional", "Which database for Kestrel?", "SQLite (only if offline).")
    assert verifier.pick("Which database for Kestrel?", candidates) is None
    candidates[1] = ("explained", "Which database for Kestrel?", "SQLite. (already installed)")
    assert verifier.pick("Which database for Kestrel?", candidates) == "explained"


AGENT = Caller(agent_id="agent:test", display_name="Test agent", purpose="coding",
               device_id="device:agent")


def _store(conn: sqlite3.Connection, verifier: Any) -> SQLiteKnowledgeStore:
    store = SQLiteKnowledgeStore(conn, verifier=verifier)
    for question, answer in (
        ("Which database does Kestrel use?", "Postgres hosted on Supabase."),
        ("Which cloud provider should be used for hosting?", "Fly.io for applications."),
    ):
        store.remember(question=question, answer=answer, caller=wf.OWNER,
                       basis=AnswerBasis.EXPLICIT, source="manual", device_id=wf.DEVICE)
    return store


def _ask(store: SQLiteKnowledgeStore, question: str) -> Any:
    return store.try_resolve(ResolutionRequest(question=question, caller=AGENT, purpose="coding"))


def test_the_store_asks_the_verifier_only_when_the_strict_matcher_finds_nothing(
    tmp_path: Path,
) -> None:
    conn = open_db(tmp_path / "varyz.db")
    try:
        model = _Model(_answers_if("kestrel"))
        store = _store(conn, ParaphraseVerifier(model, _embed))
        exact = _ask(store, "Which database does Kestrel use?")
        assert (exact.answer, exact.confidence) == ("Postgres hosted on Supabase.", 1.0)
        assert model.asked == []

        verified = _ask(store, "Where does Kestrel keep its records?")
        assert (verified.answer, verified.confidence) == ("Postgres hosted on Supabase.", 0.7)

        # A decision naming Kestrel is never offered for a question about Lumen.
        model.asked.clear()
        assert _ask(store, "Where does Lumen keep its records?") is None
        assert all("kestrel" not in pair.casefold() for pair in model.asked)
        # Why and when are the owner's to explain, not the verifier's to match.
        assert _ask(store, "Why does Kestrel keep its records there?") is None

        without = _store(conn, None)
        assert _ask(without, "Where does Kestrel keep its records?") is None
    finally:
        conn.close()


def test_the_verifier_follows_config() -> None:
    cfg = Config()
    cfg.workflows.paraphrase_verifier = "off"
    assert REAL_VERIFIER(cfg) is None
    cfg.workflows.paraphrase_verifier = "model"
    assert isinstance(REAL_VERIFIER(cfg), ParaphraseVerifier)


def test_verified_project_choice_outranks_general_default(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "varyz.db")
    try:
        verifier = ParaphraseVerifier(_Model(lambda pair: 0.97), _embed)
        store = SQLiteKnowledgeStore(conn, verifier=verifier)
        for scope, answer in (("all", "Public file"), ("kestrel", "Private file")):
            store.remember(question="Where should the planning document live?",
                           answer=answer, scope=scope, caller=wf.OWNER,
                           basis=AnswerBasis.EXPLICIT, source="manual", device_id=wf.DEVICE)
        result = store.try_resolve(ResolutionRequest(
            question="Where should the planning document reside?", project="kestrel",
            caller=AGENT, purpose="coding"))
        assert result.answer == "Private file"
        store.remember(question="Which location for planning documents?",
                       answer="Public file", scope="kestrel", caller=wf.OWNER,
                       basis=AnswerBasis.EXPLICIT, source="manual", device_id=wf.DEVICE)
        assert store.try_resolve(ResolutionRequest(
            question="Where should the planning document reside?", project="kestrel",
            caller=AGENT, purpose="coding")) is None
    finally:
        conn.close()


# -- the look at archive passages before tier two answers ----------------------------


def test_the_evidence_verifier_stops_at_the_first_record_on_topic() -> None:
    records = [f"Record {number} about the kayak" for number in range(5)]
    first = _Model(lambda pair: 0.97)
    assert EvidenceVerifier(first).on_topic("How much was the kayak?", records) is True
    assert len(first.asked) == 1
    assert first.asked[0].startswith("Question: How much was the kayak? | Record: Record 0")
    at_bar = _Model(lambda pair: evidence_verify.THRESHOLD)
    assert EvidenceVerifier(at_bar).on_topic("How much was the kayak?", records) is True


def test_the_evidence_verifier_reads_only_the_top_few_and_says_when_it_could_not() -> None:
    records = [f"Record {number}" for number in range(5)]
    none = _Model(lambda pair: 0.1)
    verifier = EvidenceVerifier(none)
    assert verifier.on_topic("How much was the kayak?", records) is False
    assert len(none.asked) == evidence_verify.TOP_K == 3
    assert verifier.last == [0.1, 0.1, 0.1]
    assert verifier.on_topic("How much was the kayak?", []) is False

    cold = EvidenceVerifier(_Model())
    assert cold.on_topic("How much was the kayak?", records) is None
    assert cold.skipped


def test_the_evidence_verifier_follows_config() -> None:
    cfg = Config()
    cfg.workflows.evidence_verifier = "off"
    assert REAL_EVIDENCE_VERIFIER(cfg) is None
    cfg.workflows.evidence_verifier = "model"
    assert isinstance(REAL_EVIDENCE_VERIFIER(cfg), EvidenceVerifier)


# -- a cold model: wait, warm in the background, or skip -------------------------------


class _Ollama:
    def __init__(self) -> None:
        self.loaded: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        body: dict[str, Any] = {"models": self.loaded}
        if url.endswith("/api/tags"):
            body = {"models": [{"name": "qwen3.5:4b", "size": 3_400_000_000}]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def cold_ollama(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    fake = _Ollama()
    spawned: list[list[str]] = []
    monkeypatch.setattr(local_classifier_module.httpx, "get", fake.get)

    class _Open:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        local_classifier_module.socket, "create_connection", lambda *a, **k: _Open()
    )
    monkeypatch.setattr(
        local_classifier_module.subprocess, "Popen", lambda command, **k: spawned.append(command)
    )
    return SimpleNamespace(ollama=fake, spawned=spawned)


def _local(when_cold: str) -> LocalModel:
    return LocalModel("qwen3.5:4b", gpu_memory=lambda: (1_000, 12_000), when_cold=when_cold)


def test_a_cold_model_waits_warms_or_is_skipped_as_asked(cold_ollama: SimpleNamespace) -> None:
    _local("load").ready()
    assert cold_ollama.spawned == []

    with pytest.raises(ClassifierUnavailable, match="loading for the next question"):
        _local("warm").ready()
    [command] = cold_ollama.spawned
    assert command[-4:] == ["http://127.0.0.1:11434", "qwen3.5:4b", task_size.KEEP_ALIVE, "8192"]

    with pytest.raises(ClassifierUnavailable, match="is not loaded"):
        _local("skip").ready()
    assert len(cold_ollama.spawned) == 1

    cold_ollama.ollama.loaded.append({"name": "qwen3.5:4b"})
    for when_cold in ("warm", "skip"):
        _local(when_cold).ready()  # a loaded model answers in every mode
    assert len(cold_ollama.spawned) == 1


def test_warming_never_crowds_the_card(cold_ollama: SimpleNamespace) -> None:
    full = LocalModel("qwen3.5:4b", gpu_memory=lambda: (9_000, 12_000), when_cold="warm")
    with pytest.raises(ClassifierUnavailable, match="free"):
        full.ready()
    assert cold_ollama.spawned == []


def test_how_long_the_model_stays_loaded_follows_config(cold_ollama: SimpleNamespace) -> None:
    cfg = Config()
    assert cfg.workflows.classifier_keep_alive == "10m"
    cfg.workflows.classifier_keep_alive = "90s"
    model = local_classifier_module.local_model_from_config(
        cfg, gpu_memory=lambda: (1_000, 12_000), when_cold="warm"
    )
    with pytest.raises(ClassifierUnavailable):
        model.ready()
    assert cold_ollama.spawned[-1][-2] == "90s"  # the background load
    assert REAL_GATE_SIZER(cfg).keep_alive == "90s"
    assert REAL_SIZER(cfg).sizers[0].keep_alive == "90s"
    # Never held for ever, and never a value Ollama would read differently.
    for bad in ("-1", "0", "10", "forever"):
        with pytest.raises(ValidationError):
            WorkflowsConfig(classifier_keep_alive=bad)
