"""The search models, fetched so a stopped download carries on where it stopped.

Nothing here reaches the Hub: its listing is a fake and its files come from
an in-memory server that honours byte ranges as the real one does.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from varyz.pipeline import model_download
from varyz.pipeline.model_download import (
    PHASE,
    STAGING,
    HubFile,
    HubModel,
    Wanted,
    fetch_search_models,
    hub_model,
    wanted_models,
)

COMMIT = "c0ffee" * 6 + "abcd"
# Larger than the 1 MB chunk `download_verified` writes, so a pause lands mid-file.
WEIGHTS = bytes(range(256)) * 10_000
CONFIG = b'{"model_type": "bert"}'


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Hub:
    """Two repositories, as the Hub lists them and serves their files."""

    def __init__(self, monkeypatch, *, served: dict[str, bytes] | None = None) -> None:
        self.files = {
            "org/embed": {"config.json": CONFIG, "model.onnx": WEIGHTS[:300_000]},
            "org/rerank": {"config.json": CONFIG, "onnx/model.onnx": WEIGHTS},
        }
        self.served = served or {}
        self.asked: list[str] = []
        self.requests: list[tuple[str, str | None]] = []
        client = httpx.Client(transport=httpx.MockTransport(self._serve))

        def stream(method, url, **kwargs):
            assert kwargs["timeout"] is not None
            return client.stream(method, url, headers=kwargs.get("headers"))

        def get(url, **kwargs):
            assert kwargs["timeout"] is not None
            return client.get(url)

        monkeypatch.setattr(httpx, "stream", stream)
        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(model_download, "wanted_models", lambda cfg: [
            Wanted("org/embed", ("model.onnx",)), Wanted("org/rerank", ("onnx/model.onnx",)),
        ])
        monkeypatch.setattr(model_download, "hub_model", self.listing)
        monkeypatch.setattr(model_download, "file_url",
                            lambda repo, name, commit: f"https://hub.test/{repo}/{commit}/{name}")

    def listing(self, want: Wanted) -> HubModel:
        self.asked.append(want.repo)
        files = []
        for name, data in self.files[want.repo].items():
            large = name.endswith(".onnx")
            files.append(HubFile(name, len(data), _blob_id(data),
                                 hashlib.sha256(data).hexdigest() if large else None))
        return HubModel(want.repo, COMMIT, tuple(files))

    def _serve(self, request: httpx.Request) -> httpx.Response:
        _, repo_owner, repo_name, commit, name = request.url.path.split("/", 4)
        assert commit == COMMIT
        repo = f"{repo_owner}/{repo_name}"
        data = self.served.get(name, self.files[repo][name])
        wanted = request.headers.get("Range")
        self.requests.append((f"{repo}/{name}", wanted))
        if wanted:
            start = int(wanted.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)


@pytest.fixture
def models(cfg):
    return cfg.data_dir / "models"


def test_the_models_land_where_fastembed_looks_for_them(cfg, models, monkeypatch):
    from huggingface_hub import snapshot_download

    hub = Hub(monkeypatch)
    heard: list[tuple[str, int, int]] = []

    assert fetch_search_models(cfg, lambda *report: heard.append(report)) is True

    for repo, files in hub.files.items():
        # How fastembed looks first: the cache alone, no network.
        found = snapshot_download(repo_id=repo, cache_dir=str(models), local_files_only=True)
        assert found == str(models / f"models--{repo.replace('/', '--')}" / "snapshots" / COMMIT)
        for name, data in files.items():
            assert (models / found / name).read_bytes() == data
    assert not (models / STAGING).exists()
    total = sum(len(data) for files in hub.files.values() for data in files.values())
    assert heard[-1] == (PHASE, total, total)
    assert all(phase == PHASE and done <= total for phase, done, _ in heard)


def test_a_paused_download_carries_on_from_what_arrived(cfg, models, monkeypatch):
    from varyz.retrieval.rerank import Reranker

    hub = Hub(monkeypatch)
    embed_bytes = sum(len(data) for data in hub.files["org/embed"].values())

    class Paused(Exception):
        pass

    def pause_mid_file(phase, done, total):
        if done > embed_bytes + len(CONFIG) + 1:  # into the reranker's weights
            raise Paused

    with pytest.raises(Paused):
        fetch_search_models(cfg, pause_mid_file)

    staged = models / STAGING / "models--org--rerank" / "snapshots" / COMMIT
    part = staged / "onnx" / "model.onnx.part"
    assert part.stat().st_size == 1024 * 1024  # the one whole chunk written
    # The finished model is in place; the paused one is not, so nothing loads half of it.
    assert (models / "models--org--embed").is_dir()
    assert not Reranker(cfg, "org/rerank").is_downloaded()

    hub.requests.clear()
    heard: list[int] = []
    assert fetch_search_models(cfg, lambda phase, done, total: heard.append(done)) is True

    assert hub.requests == [("org/rerank/onnx/model.onnx", f"bytes={1024 * 1024}-")]
    assert hub.asked == ["org/embed", "org/rerank", "org/rerank"]
    final = models / "models--org--rerank" / "snapshots" / COMMIT / "onnx" / "model.onnx"
    assert final.read_bytes() == WEIGHTS
    # Only the reranker is left to count, and its weights from what was kept.
    assert heard[0] == len(CONFIG)
    assert min(heard[1:]) > len(CONFIG) + 1024 * 1024
    assert not (models / STAGING).exists()


def test_models_already_cached_are_not_asked_for(cfg, models, monkeypatch):
    hub = Hub(monkeypatch)
    for repo in ("org/embed", "org/rerank"):
        (models / f"models--{repo.replace('/', '--')}").mkdir(parents=True)
    # Left by a run that fastembed then finished.
    leftover = models / STAGING / "models--org--rerank" / "snapshots" / COMMIT
    leftover.mkdir(parents=True)

    assert fetch_search_models(cfg) is True

    assert hub.asked == [] and hub.requests == []
    assert not (models / STAGING / "models--org--rerank").exists()


def test_a_file_that_does_not_check_out_is_left_to_fastembed_and_not_kept(
    cfg, models, monkeypatch, capsys
):
    Hub(monkeypatch, served={"onnx/model.onnx": WEIGHTS[:-1] + b"\x00"})

    assert fetch_search_models(cfg) is False

    assert "Could not fetch the search models ahead" in capsys.readouterr().err
    staged = models / STAGING / "models--org--rerank" / "snapshots" / COMMIT / "onnx"
    # Kept, it would fail the same check on every run after.
    assert list(staged.iterdir()) == []
    assert not (models / "models--org--rerank").exists()


def test_a_small_file_is_checked_against_its_git_id(cfg, models, monkeypatch):
    Hub(monkeypatch, served={"config.json": b'{"model_type": "evil"}'})

    assert fetch_search_models(cfg) is False
    assert not (models / "models--org--embed").exists()


def test_a_hub_out_of_reach_leaves_the_models_to_fastembed(cfg, monkeypatch, capsys):
    def unreachable(want):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(model_download, "wanted_models",
                        lambda cfg: [Wanted("org/embed", ("model.onnx",))])
    monkeypatch.setattr(model_download, "hub_model", unreachable)

    assert fetch_search_models(cfg) is False
    assert "no route to host" in capsys.readouterr().err


def test_the_listing_is_read_with_a_timeout_and_must_name_the_weights(monkeypatch):
    calls = []

    def info(repo, *, files_metadata, timeout):
        calls.append((repo, files_metadata, timeout))
        return SimpleNamespace(sha=COMMIT, siblings=[
            SimpleNamespace(rfilename="config.json", size=22, blob_id="b1", lfs=None),
            SimpleNamespace(rfilename="README.md", size=5, blob_id="b2", lfs=None),
            SimpleNamespace(rfilename="onnx/model.onnx", size=None, blob_id="b3",
                            lfs=SimpleNamespace(size=1234, sha256="f" * 64)),
        ])

    monkeypatch.setattr("huggingface_hub.model_info", info)

    model = hub_model(Wanted("org/rerank", ("onnx/model.onnx",)))

    assert calls == [("org/rerank", True, model_download._TIMEOUT)]
    assert model.commit == COMMIT
    assert model.files == (
        HubFile("config.json", 22, "b1", None),
        HubFile("onnx/model.onnx", 1234, "b3", "f" * 64),
    )
    with pytest.raises(ValueError, match="does not list model.onnx"):
        hub_model(Wanted("org/rerank", ("model.onnx",)))


def test_the_models_wanted_follow_the_config(cfg, monkeypatch):
    repos = [want.repo for want in wanted_models(cfg)]
    assert repos == [
        "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q", "BAAI/bge-reranker-base",
    ]

    cfg.retrieval.rerank = "off"
    assert [want.repo for want in wanted_models(cfg)] == repos[:1]

    monkeypatch.setenv("VARYZ_DISABLE_EMBEDDINGS", "1")
    assert wanted_models(cfg) == []
