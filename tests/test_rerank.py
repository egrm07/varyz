"""The reranked `recall` path, driven by a scripted reranker so no model loads.

The scores a cross-encoder gives are the one thing these tests do not check;
`scripts/recall_bench.py` measures those. What is checked is what `recall`
does with them: what it refuses, what it follows, what it cuts, and what it
never hands over in place of a withheld record.
"""

import threading
import time
from datetime import UTC, datetime

import pytest

import varyz.retrieval.answer as answer_module
from varyz.config import Config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.mcp_server import VaryzServer
from varyz.retrieval.answer import recall
from varyz.retrieval.rerank import (
    CALIBRATIONS,
    CPU_THREADS,
    DEFAULT_RERANK_MODEL,
    RerankBudgetSpent,
    RerankCalibration,
    Reranker,
    _providers,
    asks_for_current_state,
    fetch_reranker,
    load_reranker,
    machine_slot,
)

CALIBRATION = RerankCalibration(margin=4.0, confident=-6.0, floor=-8.0)


class ScriptedReranker:
    """Scores a passage by the first scripted phrase it contains."""

    def __init__(self, script: dict[str, float], default: float = -11.0):
        self.script = script
        self.default = default
        self.calibration = CALIBRATION
        self.calls = 0

    def score(self, question: str, passages: list[str]) -> list[float]:
        self.calls += 1
        out = []
        for passage in passages:
            hit = [score for phrase, score in self.script.items() if phrase in passage]
            out.append(hit[0] if hit else self.default)
        return out


RECORDS = [
    (
        "delivery",
        "Your Flexidesk order has shipped",
        "The standing desk will arrive on Tuesday. Order FD-100 from Flexidesk.",
        "2026-03-02",
        Sensitivity.PERSONAL,
    ),
    (
        "policy",
        "Flexidesk returns",
        "Anything bought from Flexidesk can be sent back within 30 days of delivery.",
        "2026-03-01",
        Sensitivity.PERSONAL,
    ),
    (
        "gym-old",
        "Gym membership",
        "The gym membership costs 30 a month on the basic plan.",
        "2025-01-10",
        Sensitivity.PERSONAL,
    ),
    (
        "gym-new",
        "Gym membership changed",
        "The gym membership moved to the premium plan at 45 a month.",
        "2026-02-10",
        Sensitivity.PERSONAL,
    ),
    (
        "diagnosis",
        "Clinic letter",
        "The clinic confirmed the diagnosis and the treatment plan for the knee.",
        "2026-01-05",
        Sensitivity.SENSITIVE,
    ),
    (
        "knee-run",
        "Running log",
        "Ran five kilometres, knee felt fine, clinic visit is next week.",
        "2026-01-06",
        Sensitivity.PERSONAL,
    ),
    (
        "noise",
        "Weekly newsletter",
        "Ten tips for a tidier desk and a calmer morning routine.",
        "2026-02-20",
        Sensitivity.PERSONAL,
    ),
]


@pytest.fixture
def archive(tmp_path):
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    with conn:
        for external_id, title, body, day, sensitivity in RECORDS:
            when = datetime.fromisoformat(day).replace(tzinfo=UTC)
            norm = NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=external_id,
                title=title,
                body=body,
                occurred_start=when,
                captured_at=when,
            )
            entry_id, _ = dao.upsert_entry(conn, norm, source="gmail", sensitivity=sensitivity)
            dao.replace_chunks(conn, entry_id, [f"{title}\n\n{body}"])
    yield cfg, conn
    conn.close()


def refs(evidence) -> list[str]:
    return [capsule.external_id for capsule in evidence.capsules]


def test_records_far_below_the_best_are_cut_instead_of_padding_the_answer(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 0.5, "tidier desk": -5.0})
    evidence = recall(conn, cfg, "Can I return the Flexidesk desk?", reranker=reranker)
    assert refs(evidence) == ["delivery", "policy"]
    assert not evidence.abstained


def test_a_record_the_first_search_missed_is_followed_from_the_one_it_found(archive, monkeypatch):
    cfg, conn = archive
    # Only the single best first-stage record is read; the returns policy
    # shares no word with the question and can only arrive by being followed.
    monkeypatch.setattr(answer_module, "_CANDIDATE_POOL", 1)
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 1.0})
    evidence = recall(conn, cfg, "When does the standing desk arrive?", reranker=reranker)
    assert "policy" in refs(evidence)


def test_an_unknown_name_is_refused_before_the_model_is_asked(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 9.0})
    evidence = recall(conn, cfg, "What did I promise Grastelle about the desk?", reranker=reranker)
    assert evidence.abstained
    assert refs(evidence) == []
    assert reranker.calls == 0


def test_nothing_scoring_below_the_floor_is_evidence(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({}, default=-9.5)
    evidence = recall(conn, cfg, "How was the desk delivered?", reranker=reranker)
    assert evidence.abstained
    assert refs(evidence) == []


def test_a_withheld_answer_is_not_replaced_by_whatever_is_left(archive):
    cfg, conn = archive
    # The clinic letter answers; the running log only mentions the clinic.
    reranker = ScriptedReranker({"diagnosis": 3.0, "knee felt fine": -3.0})
    evidence = recall(conn, cfg, "What did the clinic say about my knee?", reranker=reranker)
    assert refs(evidence) == []
    assert evidence.withheld_sensitive >= 1


def test_a_withheld_record_is_never_followed(archive, monkeypatch):
    cfg, conn = archive
    followed: list[str] = []
    original = answer_module.legacy_hybrid_search

    def spy(conn, probe, *args, **kwargs):
        followed.append(probe)
        return original(conn, probe, *args, **kwargs)

    monkeypatch.setattr(answer_module, "legacy_hybrid_search", spy)
    reranker = ScriptedReranker({"diagnosis": 3.0, "knee felt fine": 2.0})
    recall(conn, cfg, "What did the clinic say about my knee?", reranker=reranker)
    assert followed
    assert not any("diagnosis" in probe for probe in followed)


def test_a_question_about_now_puts_the_newest_statement_first(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"basic plan": 2.0, "premium plan": 1.5})
    evidence = recall(conn, cfg, "What does the gym membership cost now?", reranker=reranker)
    assert refs(evidence)[:2] == ["gym-new", "gym-old"]


def test_without_a_calibration_the_reranker_is_not_used(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0})
    reranker.calibration = None
    evidence = recall(conn, cfg, "standing desk delivery", reranker=reranker)
    assert reranker.calls == 0
    assert "delivery" in refs(evidence)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What does the gym cost now?", True),
        ("Am I still on the premium plan?", True),
        ("What is my current phone contract?", True),
        ("Why did we pick Postgres?", False),
        ("What did I know about stillwater?", False),
    ],
)
def test_questions_about_the_present_are_recognised(question, expected):
    assert asks_for_current_state(question) is expected


def test_rerank_off_loads_nothing(tmp_path):
    cfg = Config(base_dir=tmp_path)
    cfg.retrieval.rerank = "off"
    assert load_reranker(cfg) is None


def test_auto_waits_for_the_model_to_be_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    monkeypatch.setattr("varyz.retrieval.rerank.gpu_reachable", lambda: True)
    cfg = Config(base_dir=tmp_path)
    assert cfg.retrieval.rerank == "auto"
    assert load_reranker(cfg) is None
    (tmp_path / "models" / ("models--" + DEFAULT_RERANK_MODEL.replace("/", "--"))).mkdir(
        parents=True
    )
    reranker = load_reranker(cfg)
    assert isinstance(reranker, Reranker)
    assert reranker.model_name == DEFAULT_RERANK_MODEL


def test_auto_leaves_the_model_off_on_a_machine_without_a_card(tmp_path, monkeypatch):
    # Every Mac: on the processor one question cost 25-38 s of CPU.
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    monkeypatch.setattr("varyz.retrieval.rerank.gpu_reachable", lambda: False)
    monkeypatch.setattr(Reranker, "load", lambda self: pytest.fail("fetched"))
    (tmp_path / "models" / ("models--" + DEFAULT_RERANK_MODEL.replace("/", "--"))).mkdir(
        parents=True
    )
    cfg = Config(base_dir=tmp_path)
    assert load_reranker(cfg) is None
    assert fetch_reranker(cfg) is False
    cfg.retrieval.rerank = "on"  # the owner's explicit choice still holds
    assert isinstance(load_reranker(cfg), Reranker)


def test_an_uncalibrated_model_is_not_used(tmp_path):
    cfg = Config(base_dir=tmp_path)
    cfg.retrieval.rerank = "on"
    cfg.retrieval.rerank_model = "someone/uncalibrated-reranker"
    assert load_reranker(cfg) is None


def test_the_default_model_is_calibrated():
    assert DEFAULT_RERANK_MODEL in CALIBRATIONS
    assert Config().retrieval.rerank_model == DEFAULT_RERANK_MODEL


def test_the_server_hands_recall_its_reranker(archive, monkeypatch):
    cfg, _ = archive
    monkeypatch.setenv("VARYZ_DISABLE_EMBEDDINGS", "0")
    reranker = ScriptedReranker({"standing desk": 2.0, "sent back": 1.0})
    server = VaryzServer()
    server._cfg = cfg
    server._embedder_loaded = True
    server._reranker = reranker
    server._reranker_loaded = True
    result = server.call("recall", {"question": "Can I return the Flexidesk desk?"})
    assert reranker.calls >= 1
    assert "delivery" in str(result)


def test_the_index_build_fetches_a_missing_model_once(tmp_path, monkeypatch):
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    monkeypatch.setattr("varyz.retrieval.rerank.gpu_reachable", lambda: True)
    monkeypatch.delenv("VARYZ_DISABLE_EMBEDDINGS", raising=False)
    loads: list[str] = []
    monkeypatch.setattr(Reranker, "load", lambda self: loads.append(self.model_name))
    monkeypatch.setattr("varyz.retrieval.judge.load_judge", lambda cfg: object())
    cfg = Config(base_dir=tmp_path)
    assert fetch_reranker(cfg) is True
    assert loads == [DEFAULT_RERANK_MODEL]
    (tmp_path / "models" / ("models--" + DEFAULT_RERANK_MODEL.replace("/", "--"))).mkdir(
        parents=True
    )
    assert fetch_reranker(cfg) is False
    assert len(loads) == 1


def test_a_failed_fetch_does_not_stop_the_index_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    monkeypatch.setattr("varyz.retrieval.rerank.gpu_reachable", lambda: True)
    monkeypatch.delenv("VARYZ_DISABLE_EMBEDDINGS", raising=False)

    def offline(self):
        raise OSError("no network")

    monkeypatch.setattr(Reranker, "load", offline)
    monkeypatch.setattr("varyz.retrieval.judge.load_judge", lambda cfg: object())
    assert fetch_reranker(Config(base_dir=tmp_path)) is False
    assert "no network" in capsys.readouterr().err


def test_rerank_off_fetches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Reranker, "load", lambda self: pytest.fail("fetched"))
    cfg = Config(base_dir=tmp_path)
    cfg.retrieval.rerank = "off"
    assert fetch_reranker(cfg) is False


def test_a_stand_in_embedder_never_triggers_a_download(archive, monkeypatch, fake_embedder):
    from varyz.pipeline.embed import embed_pending

    cfg, conn = archive
    monkeypatch.setattr(Reranker, "load", lambda self: pytest.fail("fetched"))
    embed_pending(conn, cfg, fake_embedder)


# --- which device the cross-encoder runs on ------------------------------------


class FakeCrossEncoder:
    """Stands in for fastembed's model: scores a passage by its length."""

    opened: list[list[str] | None] = []
    threads: list[int | None] = []
    refuse: set[str] = set()
    inside = 0
    most_inside = 0
    entered: threading.Event
    release: threading.Event

    def __init__(self, model_name, cache_dir=None, providers=None, threads=None):
        FakeCrossEncoder.threads.append(threads)
        if providers and providers[0] in self.refuse:
            raise RuntimeError("the device would not open")
        FakeCrossEncoder.opened.append(providers)
        self.providers = providers

    def rerank(self, question, passages, batch_size=8):
        cls = FakeCrossEncoder
        cls.inside += 1
        cls.most_inside = max(cls.most_inside, cls.inside)
        cls.entered.set()
        cls.release.wait(5)
        cls.inside -= 1
        return [float(len(passage)) for passage in passages]


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """A cross-encoder that loads nothing, on the devices each test lists."""
    import fastembed.rerank.cross_encoder
    import onnxruntime

    import varyz.resolution.local_classifier as local_classifier

    FakeCrossEncoder.opened = []
    FakeCrossEncoder.threads = []
    FakeCrossEncoder.refuse = set()
    FakeCrossEncoder.inside = FakeCrossEncoder.most_inside = 0
    FakeCrossEncoder.entered = threading.Event()
    FakeCrossEncoder.release = threading.Event()
    FakeCrossEncoder.release.set()
    monkeypatch.setattr(fastembed.rerank.cross_encoder, "TextCrossEncoder", FakeCrossEncoder)
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    (tmp_path / "models" / ("models--" + DEFAULT_RERANK_MODEL.replace("/", "--"))).mkdir(
        parents=True
    )

    def use(providers: list[str], nvidia: tuple[int, int] | None):
        monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: providers)
        monkeypatch.setattr(local_classifier, "gpu_memory_mib", lambda: nvidia)
        return Reranker(Config(base_dir=tmp_path))

    return use


# An RTX 5070 with Ollama's qwen3.5:4b loaded: (used, total) MiB.
CARD = (4957, 12227)


def test_an_nvidia_card_reads_the_passages(devices):
    reranker = devices(["DmlExecutionProvider", "CPUExecutionProvider"], CARD)
    assert _providers() == ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert reranker.score("q", ["abc", "a"]) == [3.0, 1.0]
    assert FakeCrossEncoder.opened == [["DmlExecutionProvider", "CPUExecutionProvider"]]


def test_cuda_is_preferred_to_directml(devices):
    devices(["DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"], CARD)
    assert _providers() == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_directml_without_an_nvidia_card_stays_on_the_processor(devices):
    # DirectML also offers integrated graphics, which was never measured.
    reranker = devices(["DmlExecutionProvider", "CPUExecutionProvider"], None)
    assert _providers() is None
    reranker.load()
    assert FakeCrossEncoder.opened == [None]


def test_an_nvidia_card_without_a_gpu_runtime_stays_on_the_processor(devices):
    reranker = devices(["CPUExecutionProvider"], CARD)
    assert _providers() is None
    reranker.load()
    assert FakeCrossEncoder.opened == [None]
    assert FakeCrossEncoder.threads == [CPU_THREADS]  # not every core
    assert reranker.on_gpu is False


class SlowReranker(ScriptedReranker):
    """On the processor, and past any budget once it starts."""

    on_gpu = False

    def score(self, question, passages):
        from varyz.retrieval.rerank import _past_deadline

        self.calls += 1
        time.sleep(0.05)
        if _past_deadline():
            raise RerankBudgetSpent
        return [self.default] * len(passages)


def test_a_slow_second_look_answers_from_search_with_a_note(archive):
    cfg, conn = archive
    reranker = SlowReranker({})
    evidence = recall(conn, cfg, "Flexidesk standing desk", reranker=reranker,
                      budget_seconds=0.01)
    assert evidence.note == answer_module.SLOW_NOTE
    assert "delivery" in refs(evidence)  # search alone still answers
    assert evidence.as_dict()["note"].startswith("The local answer check ran past")


def test_a_second_process_on_the_processor_does_not_wait_for_the_first(archive):
    cfg, conn = archive
    reranker = SlowReranker({})
    with machine_slot(cfg) as mine:
        assert mine
        # Same lock file, another holder: the registry treats it as taken.
        evidence = recall(conn, cfg, "Flexidesk standing desk", reranker=reranker)
    assert reranker.calls == 0
    assert evidence.note == answer_module.BUSY_NOTE
    assert "delivery" in refs(evidence)
    with machine_slot(cfg) as again:
        assert again  # released


def test_a_card_reranker_takes_no_machine_slot(archive):
    cfg, conn = archive
    reranker = ScriptedReranker({"standing desk": 2.0})  # no on_gpu: a stand-in
    with machine_slot(cfg):
        evidence = recall(conn, cfg, "Flexidesk standing desk", reranker=reranker)
    assert reranker.calls >= 1 and evidence.note is None


def test_a_card_without_room_for_it_stays_on_the_processor(devices):
    # 2 GiB more would leave less than 30% of the card free.
    reranker = devices(["DmlExecutionProvider", "CPUExecutionProvider"], (6800, 12227))
    assert _providers() is None
    reranker.load()
    assert FakeCrossEncoder.opened == [None]


def test_a_card_that_will_not_open_falls_back_to_the_processor(devices):
    reranker = devices(["DmlExecutionProvider", "CPUExecutionProvider"], CARD)
    FakeCrossEncoder.refuse = {"DmlExecutionProvider"}
    assert reranker.score("q", ["abc", "a"]) == [3.0, 1.0]
    assert FakeCrossEncoder.opened == [None]
    assert reranker._gpu is False


def _two_at_once(reranker, *, wait_for_second: bool) -> None:
    FakeCrossEncoder.release.clear()
    threads = [threading.Thread(target=reranker.score, args=("q", ["abc"])) for _ in range(2)]
    threads[0].start()
    assert FakeCrossEncoder.entered.wait(5)
    threads[1].start()
    if wait_for_second:
        for _ in range(500):
            if FakeCrossEncoder.inside == 2:
                break
            threading.Event().wait(0.01)
    else:
        threads[1].join(0.2)
    FakeCrossEncoder.release.set()
    for thread in threads:
        thread.join(5)


def test_the_card_reads_one_question_at_a_time(devices):
    reranker = devices(["DmlExecutionProvider", "CPUExecutionProvider"], CARD)
    reranker.load()
    _two_at_once(reranker, wait_for_second=False)
    assert FakeCrossEncoder.most_inside == 1


def test_the_processor_reads_questions_side_by_side(devices):
    reranker = devices(["CPUExecutionProvider"], None)
    reranker.load()
    _two_at_once(reranker, wait_for_second=True)
    assert FakeCrossEncoder.most_inside == 2


def test_auto_skips_the_download_without_a_judge(tmp_path, monkeypatch):
    # Without a judge the reranker changed no answer, so `auto` leaves the
    # 1.1 GB model on the Hub; `on` still fetches it.
    monkeypatch.setenv("VARYZ_EMBEDDING_CACHE", str(tmp_path / "models"))
    monkeypatch.delenv("VARYZ_DISABLE_EMBEDDINGS", raising=False)
    loads: list[str] = []
    monkeypatch.setattr(Reranker, "load", lambda self: loads.append(self.model_name))
    monkeypatch.setattr("varyz.retrieval.judge.load_judge", lambda cfg: None)
    cfg = Config(base_dir=tmp_path)
    assert fetch_reranker(cfg) is False
    assert loads == []
    cfg.retrieval.rerank = "on"
    assert fetch_reranker(cfg) is True
    assert loads == [DEFAULT_RERANK_MODEL]
