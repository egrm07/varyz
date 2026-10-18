"""The local task classifier: new work or a small change, decided on this machine.

Offline: Ollama and the embedding model are replaced by fakes. The measured
accuracy lives in scripts/task_size_bench.py and its report, not here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from varyz.config import Config, WorkflowsConfig
from varyz.db.connection import open_db
from varyz.resolution import local_classifier as local_classifier_module
from varyz.resolution import task_size as task_size_module
from varyz.resolution.briefing import start_task
from varyz.resolution.models import Caller
from varyz.resolution.task_size import (
    EXAMPLES,
    FEW_SHOT,
    ChainSizer,
    EmbeddingSizer,
    ModelSizer,
    SizerUnavailable,
    letter_messages,
    loaded_mib,
)
from varyz.resolution.workflows import (
    Sizing,
    SQLiteWorkflowStore,
    guide,
    parse_steps,
)

# Taken before the suite's autouse fixture swaps them for the lexicon.
REAL_SIZER_FROM_CONFIG = task_size_module.sizer_from_config
REAL_CLASSIFIER_STATUS = task_size_module.classifier_status
FIXTURES = Path(__file__).resolve().parents[1] / "scripts" / "fixtures"
OWNER = Caller(
    agent_id="owner:local", display_name="Local owner", purpose="owner_knowledge",
    device_id="device:test", transport="stdio",
)
STEPS = parse_steps(["colour", "logo", "page content"])


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


class _Ollama:
    """An Ollama that answers /api/ps, /api/tags and /api/chat from memory."""

    def __init__(self, *, loaded: bool = True, pulled: bool = True,
                 letters: dict[str, float] | None = None) -> None:
        self.loaded = loaded
        self.pulled = pulled
        self.size = 3_400_000_000
        self.letters = letters if letters is not None else {"A": -0.05, "B": -3.0}
        self.chats: list[dict[str, Any]] = []

    def get(self, url: str, **_: Any) -> httpx.Response:
        request = httpx.Request("GET", url)
        entry = {"name": "qwen3.5:4b", "size": self.size}
        if url.endswith("/api/ps"):
            return httpx.Response(200, json={"models": [entry] if self.loaded else []},
                                  request=request)
        return httpx.Response(200, json={"models": [entry] if self.pulled else []},
                              request=request)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.chats.append(kwargs["json"])
        body = {
            "message": {"content": "A"},
            "logprobs": [{
                "token": "A", "logprob": -0.05,
                "top_logprobs": [{"token": t, "logprob": p} for t, p in self.letters.items()],
            }],
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> _Ollama:
    fake = _Ollama()
    monkeypatch.setattr(local_classifier_module.httpx, "get", fake.get)
    monkeypatch.setattr(local_classifier_module.httpx, "post", fake.post)

    class _Open:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        local_classifier_module.socket, "create_connection", lambda *a, **k: _Open()
    )
    return fake


def _sizer(**kwargs: Any) -> ModelSizer:
    kwargs.setdefault("gpu_memory", lambda: (1_000, 12_000))
    return ModelSizer("qwen3.5:4b", url="http://localhost:11434", **kwargs)


# -- the model ----------------------------------------------------------------


def test_the_model_answers_one_letter_from_its_log_probabilities(ollama: _Ollama) -> None:
    assert _sizer()("Build the pricing page", STEPS) == Sizing("new", "model")
    [chat] = ollama.chats
    # The window `recall`'s judge loads the same model with: a different one
    # makes Ollama reload the model at every switch between the two.
    assert chat["options"] == {"temperature": 0.0, "num_predict": 1, "num_ctx": 8192}
    assert chat["logprobs"] is True and chat["think"] is False
    # The eight authored examples, then the task.
    assert len(chat["messages"]) == 1 + 2 * len(FEW_SHOT) + 1
    assert chat["messages"][-1]["content"].startswith("Request: Build the pricing page")

    ollama.letters = {"B": -0.02, "A": -4.0}
    assert _sizer()("Make the button blue", STEPS).size == "small"


def test_the_model_is_not_loaded_onto_a_crowded_card(ollama: _Ollama) -> None:
    ollama.loaded = False
    # 12 GB card, 8 GB in use: 3.2 GB more leaves under 30% free.
    with pytest.raises(SizerUnavailable, match="leave the GPU under 30% free"):
        _sizer(gpu_memory=lambda: (8_000, 12_000))("Build the page", STEPS)
    assert ollama.chats == []
    # Room to spare: it loads.
    assert _sizer()("Build the page", STEPS).by == "model"
    # Measured: a 7.6 GB model took 8.2 GB of this 12 GB card, 24% left free.
    # Counting the file size alone would have let it load; counting what loading
    # adds does not.
    ollama.size = 7_600_000_000
    with pytest.raises(SizerUnavailable, match="under 30% free"):
        _sizer(gpu_memory=lambda: (1_020, 12_227))("Build the page", STEPS)
    assert loaded_mib(7_600_000_000) >= 9_266 - 1_020
    ollama.size = 3_400_000_000
    # Already loaded: nothing more to put on the card, so no check.
    ollama.loaded = True
    assert _sizer(gpu_memory=lambda: (11_900, 12_000))("Build the page", STEPS).by == "model"


def test_the_model_steps_aside_when_it_is_not_there(
    ollama: _Ollama, monkeypatch: pytest.MonkeyPatch
) -> None:
    ollama.loaded = ollama.pulled = False
    with pytest.raises(SizerUnavailable, match="not pulled"):
        _sizer()("Build the page", STEPS)

    def refused(*_: object, **__: object) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(local_classifier_module.socket, "create_connection", refused)
    with pytest.raises(SizerUnavailable, match="not running"):
        _sizer()("Build the page", STEPS)


def test_an_answer_that_is_neither_letter_is_no_answer(
    ollama: _Ollama, monkeypatch: pytest.MonkeyPatch
) -> None:
    def neither(url: str, **kwargs: Any) -> httpx.Response:
        body = {"message": {"content": "The"},
                "logprobs": [{"token": "The", "logprob": -0.1, "top_logprobs": []}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(local_classifier_module.httpx, "post", neither)
    with pytest.raises(SizerUnavailable, match="neither A nor B"):
        _sizer()("Build the page", STEPS)


def test_the_owners_prompt_never_goes_to_another_machine() -> None:
    with pytest.raises(ValueError, match="this machine only"):
        ModelSizer("qwen3.5:4b", url="http://10.0.0.5:11434")
    with pytest.raises(ValidationError, match="must be on this machine"):
        WorkflowsConfig(classifier_url="https://ollama.example.com")
    assert WorkflowsConfig(classifier_url="http://127.0.0.1:11434").classifier_url


# -- the embedding and the chain ----------------------------------------------


def _fake_embed(calls: list[int]) -> Any:
    labels = dict(EXAMPLES)

    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        out = []
        for text in texts:
            if text in labels:
                out.append([1.0, 0.0] if labels[text] == "new" else [0.0, 1.0])
            else:
                out.append([0.9, 0.1] if "build" in text.lower() else [0.2, 0.8])
        return out

    return embed


def test_the_embedding_takes_the_nearer_authored_centroid() -> None:
    calls: list[int] = []
    sizer = EmbeddingSizer(_fake_embed(calls))
    assert sizer("Build a careers page", STEPS) == Sizing("new", "embedding")
    assert sizer("the footer is grey", STEPS) == Sizing("small", "embedding")
    assert calls == [len(EXAMPLES), 1, 1]  # the examples are embedded once


def test_the_chain_falls_back_in_order_and_ends_at_the_lexicon() -> None:
    def down(task: str, steps: Any) -> Sizing:
        raise SizerUnavailable("Ollama is not running")

    def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no model files")

    chain = ChainSizer([down, EmbeddingSizer(_fake_embed([]))])
    assert chain("Build a careers page", STEPS).by == "embedding"
    assert chain.skipped == ["Ollama is not running"]

    last = ChainSizer([down, EmbeddingSizer(broken)])
    assert last("Fix the footer", STEPS) == Sizing("small", "lexicon")


def test_the_configured_classifier_decides_the_chain() -> None:
    def kinds(setting: str) -> list[str]:
        cfg = Config(workflows=WorkflowsConfig.model_validate({"task_classifier": setting}))
        return [type(s).__name__ for s in REAL_SIZER_FROM_CONFIG(cfg).sizers]

    assert kinds("model") == ["ModelSizer", "EmbeddingSizer"]
    assert kinds("embedding") == ["EmbeddingSizer"]
    assert kinds("lexicon") == []


def test_doctor_says_which_classifier_answers_without_loading_it(
    ollama: _Ollama, monkeypatch: pytest.MonkeyPatch
) -> None:
    def status(**settings: Any) -> tuple[bool, str]:
        cfg = Config(workflows=WorkflowsConfig.model_validate(settings))
        return REAL_CLASSIFIER_STATUS(cfg, gpu_memory=lambda: (1_000, 12_000))

    assert status() == (True, "qwen3.5:4b through Ollama on this machine (98.6% held out)")
    ollama.loaded = ollama.pulled = False
    ok, detail = status()
    assert ok and detail.startswith("local embedding answers for now")
    assert "`ollama pull qwen3.5:4b`" in detail
    assert status(task_classifier="lexicon")[1].startswith("verb list")
    assert ollama.chats == []  # probed, never asked, so never loaded


def test_the_shipped_examples_are_the_dev_set_and_not_the_held_out_set() -> None:
    dev = json.loads((FIXTURES / "task_size_dev.json").read_text(encoding="utf-8"))
    held = json.loads((FIXTURES / "task_size_held_out.json").read_text(encoding="utf-8"))
    assert [(item["task"], item["label"]) for item in dev["items"]] == list(EXAMPLES)
    shipped = {task.casefold() for task, _ in EXAMPLES}
    assert not shipped & {item["task"].casefold() for item in held["items"]}
    assert {task for task, _ in FEW_SHOT} <= {task for task, _ in EXAMPLES}
    assert letter_messages("x", few_shot=False)[0]["role"] == "system"


# -- where it is asked --------------------------------------------------------


class _Counting:
    def __init__(self, size: str) -> None:
        self.size = size
        self.calls = 0

    def __call__(self, task: str, steps: Any) -> Sizing:
        self.calls += 1
        return Sizing(self.size, "model")


def test_the_classifier_decides_the_nudge_and_is_asked_only_when_it_matters(
    conn: sqlite3.Connection,
) -> None:
    SQLiteWorkflowStore(conn).record(
        name="website", steps=["colour", "logo", "page content"], caller=OWNER,
        device_id="device:test", source="manual", domain="website",
    )
    [workflow] = SQLiteWorkflowStore(conn).current()

    # The lexicon calls this new work; the classifier says small, and it decides.
    small = _Counting("small")
    task = "the hero on the landing page feels cramped"
    item = guide(workflow, task, project="persona", sizer=small)
    assert (item["work"], item["sized_by"], item["nudge"]) == ("small", "model", False)
    assert "just do it" in item["guidance"]

    new = _Counting("new")
    [item] = start_task(conn, "Build the landing page", project="persona",
                        sizer=new)["owner_workflows"]
    assert (item["work"], item["sized_by"], item["nudge"]) == ("new", "model", True)

    # At the first step nothing is open before it: no size is needed, none asked.
    idle = _Counting("new")
    item = guide(workflow, "Pick the brand colour", project="persona", sizer=idle)
    assert idle.calls == 0 and item["work"] is None and item["nudge"] is False


def test_the_suite_never_reaches_a_live_classifier() -> None:
    chain = task_size_module.sizer_from_config(Config())
    assert isinstance(chain, ChainSizer) and chain.sizers == ()
