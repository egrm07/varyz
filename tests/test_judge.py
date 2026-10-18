"""The judged `recall` path, with a scripted local model so nothing is served.

Which records a real model picks is measured by `scripts/recall_bench.py`.
What is checked here is what `recall` does with the picks: uses them, follows
them, falls back when the model fails, and never follows or stands in for a
withheld record.
"""

import json
from datetime import UTC, datetime

import pytest

import varyz.retrieval.answer as answer_module
import varyz.retrieval.judge as judge_module
from tests import test_rerank
from tests.test_rerank import ScriptedReranker, refs
from varyz.config import Config
from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.mcp_server import VaryzServer
from varyz.pipeline.windows import build_windows
from varyz.resolution.local_classifier import ClassifierUnavailable
from varyz.resolution.models import ResolutionRequest
from varyz.resolution.resolver import EvidenceTier
from varyz.retrieval.answer import recall
from varyz.retrieval.judge import Judge, load_judge, render
from varyz.trust import register_agent


class ScriptedModel:
    """Picks every numbered record whose line contains one of `phrases`."""

    def __init__(self, *phrases: str, reply: str | None = None, fail: bool = False):
        self.phrases = phrases
        self.reply = reply
        self.fail = fail
        self.prompts: list[str] = []
        self.histories: list[list[dict[str, str]]] = []

    def complete(self, system: str, user: str, history=()) -> str:
        self.prompts.append(user)
        self.histories.append(list(history))
        if self.fail:
            raise TimeoutError("the model did not answer")
        if self.reply is not None:
            return self.reply
        picked = []
        said = [turn["content"] for turn in history if turn["role"] == "user"]
        for line in "\n".join([*said, user]).splitlines():
            if line.startswith("[") and any(phrase in line for phrase in self.phrases):
                picked.append(int(line[1 : line.index("]")]))
        return json.dumps({"records": picked})


DESK = "Can I return the Flexidesk desk?"
archive = test_rerank.archive


def test_the_records_the_model_picks_are_the_answer(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": 1.0})
    judge = Judge(ScriptedModel("sent back"))
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=judge)
    assert refs(evidence) == ["policy"]
    # Tier 2 answers from judged records without the word-coverage check.
    assert evidence.judged
    assert not recall(conn, cfg, DESK, reranker=reranker).judged


def test_a_model_that_picks_nothing_is_an_abstention(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5})
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=Judge(ScriptedModel()))
    assert evidence.abstained
    assert refs(evidence) == []


def test_a_model_that_fails_leaves_the_rerankers_cut(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": -5.0})
    judge = Judge(ScriptedModel("tidier desk", fail=True))
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=judge)
    assert refs(evidence) == ["delivery", "policy"]
    assert not evidence.judged


def test_a_chosen_record_is_followed_to_the_one_it_belongs_with(archive, monkeypatch):
    cfg, conn = archive
    # The first stage finds only the delivery notice and nothing is chained;
    # the returns policy shares its rare word "Flexidesk" and nothing else.
    monkeypatch.setattr(answer_module, "_CANDIDATE_POOL", 1)
    monkeypatch.setattr(answer_module, "_CHAIN_SEEDS", 0)
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": -9.0})
    model = ScriptedModel("standing desk", "sent back")
    question = "When does the standing desk arrive, and until when can it go back?"
    evidence = recall(conn, cfg, question, reranker=reranker, judge=Judge(model))
    assert len(model.prompts) == 2
    assert "sent back" not in model.prompts[0]
    assert sorted(refs(evidence)) == ["delivery", "policy"]


def test_a_withheld_pick_is_reported_and_never_followed(archive, monkeypatch):
    cfg, conn = archive
    followed: list[str] = []
    original = answer_module._records_sharing_rare_words

    def spy(conn, hit):
        followed.append(hit.entry.external_id)
        return original(conn, hit)

    monkeypatch.setattr(answer_module, "_records_sharing_rare_words", spy)
    reranker = ScriptedReranker({"diagnosis": 3.0, "knee felt fine": 2.0})
    judge = Judge(ScriptedModel("diagnosis"))
    evidence = recall(
        conn, cfg, "What did the clinic say about my knee?", reranker=reranker, judge=judge
    )
    assert refs(evidence) == []
    assert evidence.withheld_sensitive == 1
    assert "diagnosis" not in followed


def test_without_a_judge_a_withheld_best_record_gets_no_stand_in(archive):
    cfg, conn = archive
    # The running log is close enough to the clinic letter to survive the
    # cut, but it only mentions the clinic; the letter is the answer.
    reranker = ScriptedReranker({"diagnosis": 3.0, "knee felt fine": 0.5})
    evidence = recall(conn, cfg, "What did the clinic say about my knee?", reranker=reranker)
    assert refs(evidence) == []
    assert evidence.withheld_sensitive >= 1


def test_the_model_sees_the_records_as_numbered_quoted_lines(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0})
    model = ScriptedModel("standing desk")
    recall(conn, cfg, DESK, reranker=reranker, judge=Judge(model))
    prompt = model.prompts[0]
    assert prompt.startswith(f"Question: {DESK}")
    line = next(line for line in prompt.splitlines() if "standing desk" in line)
    number, rest = line.split(" ", 1)
    assert number.startswith("[") and number.endswith("]")
    assert rest.startswith("2026-03-02 gmail: Your Flexidesk order has shipped / ")
    assert line.count("Your Flexidesk order has shipped") == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("not json", None),
        ('{"records": "1"}', None),
        ('{"other": [1]}', None),
        ('{"records": []}', []),
        ('{"records": [true, 0, 4, 2, 2, "1"]}', ["b"]),
        ('{"records": [3, 1]}', ["a", "c"]),
    ],
)
def test_a_reply_is_read_strictly(reply, expected):
    hits = _hits("abc")
    reading = Judge(ScriptedModel(reply=reply)).read("q", hits)
    assert (None if reading is None else [hit.entry.id for hit in reading.picks]) == expected
    assert render("q", hits).splitlines()[3] == "[1] undated notes: a"


def test_the_second_look_continues_the_conversation():
    first, more = _hits("abc"), _hits("de")
    model = ScriptedModel(reply='{"records": [1, 3]}')
    reading = Judge(model).read("q", first)
    assert reading is not None
    model.reply = '{"records": [1, 2, 4, 5]}'
    again = Judge(model).read_more(reading, more)
    assert again is not None
    # The model is not shown the first records again: it already read them.
    assert model.histories[1] == [
        {"role": "user", "content": model.prompts[0]},
        {"role": "assistant", "content": '{"records": [1, 3]}'},
    ]
    assert model.prompts[1].splitlines()[1:3] == ["[4] undated notes: d", "[5] undated notes: e"]
    # "b" was read and passed over the first time; only a record picked
    # before or shown now can be picked now.
    assert [hit.entry.id for hit in again.picks] == ["a", "d", "e"]
    assert Judge(ScriptedModel(fail=True)).read_more(reading, more) is None


def test_the_judge_reads_while_the_reranker_scores(archive):
    import threading

    cfg, conn = archive
    threads: list[str] = []

    class Watched(ScriptedModel):
        def complete(self, system, user, history=()):
            threads.append(threading.current_thread().name)
            return super().complete(system, user, history)

    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5})
    recall(conn, cfg, DESK, reranker=reranker, judge=Judge(Watched("sent back")))
    assert threads and all(name.startswith("varyz-judge") for name in threads)


def test_a_judge_that_breaks_leaves_the_rerankers_cut(archive):
    cfg, conn = archive

    class Broken(Judge):
        def read(self, question, hits):
            raise RuntimeError("the judge broke")

    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": -5.0})
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=Broken(ScriptedModel()))
    assert refs(evidence) == ["delivery", "policy"]


def test_a_chained_record_the_reranker_trusts_is_shown_to_the_judge(archive, monkeypatch):
    cfg, conn = archive
    # The judge reads only the delivery notice; chaining finds the policy
    # while it reads, and the reranker puts the policy first.
    monkeypatch.setattr(answer_module, "_CANDIDATE_POOL", 1)
    monkeypatch.setattr(
        answer_module,
        "_records_sharing_rare_words",
        lambda conn, hit, **kwargs: [],
    )
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 3.0})
    model = ScriptedModel("standing desk", "sent back")
    question = "When does the standing desk arrive?"
    evidence = recall(conn, cfg, question, reranker=reranker, judge=Judge(model))
    assert len(model.prompts) == 2
    assert "sent back" in model.prompts[1]
    assert sorted(refs(evidence)) == ["delivery", "policy"]


def test_a_judge_that_picks_too_much_keeps_only_what_the_reranker_backs(archive, monkeypatch):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": -5.0})
    model = ScriptedModel("standing desk", "sent back", "tidier desk")
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=Judge(model))
    assert "noise" in refs(evidence)
    monkeypatch.setattr(answer_module, "_MOST_PICKS", 2)
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=Judge(model))
    assert refs(evidence) == ["delivery", "policy"]


class LoadingModel(ScriptedModel):
    """A model that is not on the card until something asks it a question."""

    def __init__(self, *phrases: str, is_loaded: bool = False):
        super().__init__(*phrases)
        self.is_loaded = is_loaded

    def loaded(self) -> bool:
        return self.is_loaded

    def complete(self, system: str, user: str, history=()) -> str:
        self.is_loaded = True
        return super().complete(system, user, history)


def test_a_model_still_to_load_is_not_waited_for_but_loaded_for_next_time(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": -5.0})
    model = LoadingModel("sent back")
    judge = Judge(model)
    first = recall(conn, cfg, DESK, reranker=reranker, judge=judge)
    # The reranker's cut, at once.
    assert refs(first) == ["delivery", "policy"]
    judge._warming.join(timeout=5)
    assert model.is_loaded
    assert not any(line.startswith("[") for prompt in model.prompts for line in prompt.splitlines())
    second = recall(conn, cfg, DESK, reranker=reranker, judge=judge)
    assert refs(second) == ["policy"]


def test_a_judge_is_not_ready_while_it_loads():
    judge = Judge(LoadingModel())
    assert not judge.ready()
    judge._warming = type("Busy", (), {"is_alive": lambda self: True})()
    judge.provider.is_loaded = True
    assert not judge.ready()
    judge._warming = None
    assert judge.ready()


def test_a_model_that_cannot_say_whether_it_is_loaded_is_not_waited_for():
    class Unsure(ScriptedModel):
        def loaded(self):
            raise OSError("no answer")

    assert not Judge(Unsure()).ready()
    assert Judge(ScriptedModel()).ready()


def _hits(names):
    from datetime import UTC, datetime

    from varyz.domain import Entry, SearchHit, Sensitivity

    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    return [
        SearchHit(
            entry=Entry(
                id=name,
                source="notes",
                external_id=name,
                kind="note",
                title=name,
                body=name,
                content_hash=name,
                sensitivity=Sensitivity.PERSONAL,
                captured_at=now,
                ingested_at=now,
            ),
            score=0.0,
        )
        for name in names
    ]


def test_judge_off_or_disabled_loads_nothing(tmp_path, monkeypatch):
    cfg = Config(base_dir=tmp_path)
    cfg.retrieval.judge = "off"
    assert load_judge(cfg) is None
    cfg.retrieval.judge = "on"
    monkeypatch.setenv("VARYZ_DISABLE_JUDGE", "1")
    assert load_judge(cfg) is None


def test_auto_waits_for_an_installed_model_on_a_gpu(tmp_path, monkeypatch):
    cfg = Config(base_dir=tmp_path)
    assert cfg.retrieval.judge == "auto"
    assert load_judge(cfg) is None  # the suite reports no local model installed
    cfg.retrieval.judge = "on"
    assert isinstance(load_judge(cfg), Judge)

    cfg.retrieval.judge = "auto"
    monkeypatch.setattr("varyz.llm.local_runtime.ManagedLlamaRuntime.available", lambda self: True)
    monkeypatch.setattr(judge_module, "_runs_on_cpu", lambda cfg: True)
    assert load_judge(cfg) is None
    monkeypatch.setattr(judge_module, "_runs_on_cpu", lambda cfg: False)
    assert isinstance(load_judge(cfg), Judge)


def test_auto_falls_back_to_the_classifier_model_on_a_gpu(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(judge_module.platform, "machine", lambda: "x86_64")
    cfg = Config(base_dir=tmp_path)
    cfg.workflows.classifier_keep_alive = "90s"
    assert cfg.retrieval.judge == "auto"
    # The suite reports neither the managed model nor Ollama's installed.
    assert load_judge(cfg) is None

    monkeypatch.setattr("varyz.llm.ollama_local.OllamaProvider.available", lambda self: True)
    monkeypatch.setattr("varyz.resolution.local_classifier.gpu_memory_mib", lambda: None)
    assert load_judge(cfg) is None  # no GPU: forty records on a processor

    monkeypatch.setattr(
        "varyz.resolution.local_classifier.gpu_memory_mib", lambda: (1_000, 12_000)
    )
    judge = load_judge(cfg)
    assert judge is not None
    ollama = judge.provider._ollama
    assert ollama.model == cfg.workflows.classifier_model
    # The classifiers' own window and keep-alive: one loaded model serves both.
    assert ollama.context_window == 8192
    assert ollama.keep_alive == "90s"
    assert ollama.format_schema == judge_module.JUDGE_SCHEMA
    assert ollama.request_timeout_seconds == 60.0


@pytest.mark.parametrize("system,machine,expected", [
    ("Darwin", "arm64", True),
    ("Darwin", "aarch64", True),
    ("Darwin", "x86_64", False),
    ("Linux", "arm64", False),
    ("Windows", "AMD64", False),
])
def test_classifier_judge_recognizes_apple_gpu_without_nvidia_memory(
    tmp_path, monkeypatch, system, machine, expected
):
    monkeypatch.setattr(judge_module.platform, "system", lambda: system)
    monkeypatch.setattr(judge_module.platform, "machine", lambda: machine)
    monkeypatch.setattr("varyz.resolution.local_classifier.gpu_memory_mib", lambda: None)
    monkeypatch.setattr("varyz.llm.ollama_local.OllamaProvider.available", lambda self: True)
    assert isinstance(judge_module._classifier_judge(Config(base_dir=tmp_path)), Judge) is expected
    monkeypatch.setattr("varyz.llm.ollama_local.OllamaProvider.available", lambda self: False)
    assert judge_module._classifier_judge(Config(base_dir=tmp_path)) is None


def test_the_classifier_judge_is_not_asked_when_it_would_crowd_the_card(
    tmp_path, monkeypatch
):
    cfg = Config(base_dir=tmp_path)
    monkeypatch.setattr("varyz.llm.ollama_local.OllamaProvider.available", lambda self: True)
    monkeypatch.setattr(
        "varyz.resolution.local_classifier.gpu_memory_mib", lambda: (1_000, 12_000)
    )
    judge = load_judge(cfg)
    assert judge is not None

    def crowded() -> None:
        raise ClassifierUnavailable("loading qwen3.5:4b would leave the GPU under 30% free")

    asked: list[str] = []
    monkeypatch.setattr(judge.provider._gate, "ready", crowded)
    monkeypatch.setattr(
        judge.provider._ollama, "complete", lambda *a, **k: asked.append("asked") or "{}"
    )
    assert judge.read(DESK, _hits(["delivery"])) is None
    assert asked == []


def test_the_judge_asks_briefly_in_a_small_window(tmp_path, monkeypatch):
    cfg = Config(base_dir=tmp_path)
    cfg.local_model.backend = "ollama"
    cfg.local_model.context_window = 32768
    cfg.retrieval.judge = "on"
    judge = load_judge(cfg)
    assert judge is not None
    assert judge.provider.request_timeout_seconds == 60.0
    assert judge.provider.max_tokens == judge_module._REPLY_TOKENS
    assert judge.provider.context_window == 8192
    assert cfg.local_model.request_timeout_seconds == 600.0


def test_the_judges_server_keeps_nothing_in_host_memory(tmp_path):
    cfg = Config(base_dir=tmp_path)
    cfg.local_model.backend = "managed"
    cfg.retrieval.judge = "on"
    judge = load_judge(cfg)
    assert judge is not None
    settings = judge.provider.runtime.settings
    assert settings.cuda_unified_memory is False
    assert settings.prompt_cache_ram_mib == 0
    assert settings.parallel_slots == 1
    # The compiler's own server keeps what it needs.
    assert cfg.local_model.cuda_unified_memory is True
    assert cfg.local_model.prompt_cache_ram_mib == 4096


def test_the_server_hands_recall_its_judge(archive, monkeypatch):
    cfg, _ = archive
    monkeypatch.setenv("VARYZ_DISABLE_EMBEDDINGS", "0")
    model = ScriptedModel("sent back")
    server = VaryzServer()
    server._cfg = cfg
    server._embedder_loaded = True
    server._reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 1.0})
    server._reranker_loaded = True
    server._judge = Judge(model)
    server._judge_loaded = True
    result = server.call("recall", {"question": DESK})
    assert model.prompts
    assert "policy" in str(result)
    assert "'reference': 'delivery'" not in str(result)


def test_the_server_loads_no_judge_without_a_reranker(archive, monkeypatch):
    cfg, _ = archive
    monkeypatch.setattr(judge_module, "load_judge", lambda cfg: pytest.fail("loaded"))
    server = VaryzServer()
    server._cfg = cfg
    server._reranker = None
    server._reranker_loaded = True
    assert server.judge is None


def test_a_judge_lets_a_fairly_sure_reranker_overrule_the_distance_gate(archive, monkeypatch):
    cfg, conn = archive
    monkeypatch.setattr(answer_module, "is_supported", lambda *args, **kwargs: False)
    question = "How did my new desk end up being delivered to the house?"
    reranker = ScriptedReranker({"standing desk": -7.0})
    assert recall(conn, cfg, question, reranker=reranker).abstained
    judge = Judge(ScriptedModel("standing desk"))
    evidence = recall(conn, cfg, question, reranker=reranker, judge=judge)
    assert refs(evidence) == ["delivery"]


class _Loaded:
    """`RetrievalModels` with the reranker and judge already loaded."""

    def __init__(self, reranker, judge):
        self.loaded = (None, reranker, judge)

    def get(self):
        return self.loaded


def _off_topic_chat(conn) -> None:
    # A cleared agent conversation that shares "desk" with the question and is
    # about something else: the shape of the Codex and Claude snippets an away
    # ask came back "answered" with on 2026-10-02.
    turns = [
        (True, "tidy the desk app sidebar, the icons are crowded"),
        (False, "Moved the desk app icons into one row and dropped the labels."),
    ]
    for index, (authored, body) in enumerate(turns):
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE, external_id=f"sidebar-{index}", body=body,
                occurred_start=datetime(2026, 3, 3, 9, index, tzinfo=UTC),
                thread_id="sidebar", authored_by_user=authored,
            ),
            source="ai_conversations", sensitivity=Sensitivity.SENSITIVE,
        )
    build_windows(conn)
    window = conn.execute(
        "SELECT id FROM conversation_windows WHERE thread_id = 'sidebar'"
    ).fetchone()["id"]
    with conn:
        conn.execute(
            "INSERT INTO conversation_window_labels"
            " (window_id, label, agreed, votes_json, prompt_version, content_hash, created_at)"
            " VALUES (?, 'personal', 1, '[]', 'window-v1', 'h', '2026-03-03')",
            (window,),
        )


def test_a_conversation_the_judge_never_read_does_not_answer_for_it(archive):
    cfg, conn = archive
    _off_topic_chat(conn)
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5})
    caller = register_agent(conn, agent_id="agent:test", display_name="Test").caller
    request = ResolutionRequest(
        question=DESK, caller=caller, purpose=caller.purpose, allow_escalation=False,
    )

    # The judge read the passages and picked none. The conversation channel
    # still fills the empty slots, under the judged flag the passages earned.
    picked_nothing = Judge(ScriptedModel())
    evidence = recall(conn, cfg, DESK, reranker=reranker, judge=picked_nothing)
    assert evidence.judged
    assert [capsule.kind for capsule in evidence.capsules] == ["conversation"]
    # So it is held to the coverage check, as any unjudged record is, and the
    # question goes to the owner instead of out as an answer.
    tier = EvidenceTier(conn, cfg, models=_Loaded(reranker, picked_nothing))
    assert tier.try_resolve(request) is None

    # A record the judge did pick still carries the conversation beside it.
    picked = Judge(ScriptedModel("sent back"))
    tier = EvidenceTier(conn, cfg, models=_Loaded(reranker, picked))
    resolved = tier.try_resolve(request)
    assert resolved is not None and resolved.tier == 2
    assert "Flexidesk returns" in (resolved.answer or "")
