import pytest

from varyz.llm.ollama_local import OllamaProvider


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


def test_complete_can_disable_thinking_and_records_performance(monkeypatch):
    captured = {}

    def fake_post(url, *, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return FakeResponse(
            {
                "message": {"content": '{"facets":[]}'},
                "load_duration": 10,
                "prompt_eval_count": 20,
                "prompt_eval_duration": 30,
                "eval_count": 4,
                "eval_duration": 5,
            }
        )

    monkeypatch.setattr("varyz.llm.ollama_local.httpx.post", fake_post)
    provider = OllamaProvider(
        "qwen3.5:9b-q4_K_M",
        context_window=8192,
        think=False,
        format_schema={"type": "object"},
    )

    assert provider.complete("system", "user") == '{"facets":[]}'
    assert captured["payload"]["think"] is False
    assert captured["payload"]["format"] == {"type": "object"}
    assert captured["payload"]["options"] == {
        "num_predict": 1024,
        "temperature": 0.0,
        "num_ctx": 8192,
    }
    assert captured["timeout"] == 600.0
    assert provider.metrics == [
        {
            "load_duration": 10,
            "prompt_eval_count": 20,
            "prompt_eval_duration": 30,
            "eval_count": 4,
            "eval_duration": 5,
        }
    ]


def test_complete_uses_configured_request_timeout(monkeypatch):
    captured = {}

    def fake_post(url, *, json, timeout):
        captured["timeout"] = timeout
        return FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("varyz.llm.ollama_local.httpx.post", fake_post)
    provider = OllamaProvider("local", request_timeout_seconds=901)

    assert provider.complete("system", "user") == "ok"
    assert captured["timeout"] == 901


def test_runtime_details_returns_matching_loaded_model(monkeypatch):
    monkeypatch.setattr(
        "varyz.llm.ollama_local.httpx.get",
        lambda *args, **kwargs: FakeResponse(
            {
                "models": [
                    {
                        "name": "qwen3.5:9b-q4_K_M",
                        "size": 7_000_000_000,
                        "size_vram": 7_000_000_000,
                    }
                ]
            }
        ),
    )

    provider = OllamaProvider("qwen3.5:9b-q4_K_M")

    assert provider.runtime_details()["size_vram"] == 7_000_000_000


def test_model_inventory_retries_transient_ollama_server_error(monkeypatch):
    responses = iter(
        (
            FakeResponse({}, status_code=500),
            FakeResponse({"models": [{"name": "gemma4:12b-it-q4_K_M"}]}),
        )
    )
    monkeypatch.setattr(
        "varyz.llm.ollama_local.httpx.get",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr("varyz.llm.ollama_local.time.sleep", lambda _delay: None)

    provider = OllamaProvider("gemma4:12b-it-q4_K_M")

    assert provider._installed_model_names() == ["gemma4:12b-it-q4_K_M"]


@pytest.mark.parametrize(
    ("configured", "used"),
    [
        ("http://localhost:11434", "http://127.0.0.1:11434"),
        ("http://localhost:11434/", "http://127.0.0.1:11434"),
        ("http://localhost", "http://127.0.0.1"),
        ("http://localhostile:11434", "http://localhostile:11434"),
        ("http://gpu-box:11434", "http://gpu-box:11434"),
    ],
)
def test_localhost_is_reached_over_ipv4(configured, used):
    # Windows tries ::1 first and Ollama listens on IPv4 only: two seconds a call.
    assert OllamaProvider("m", url=configured).url == used
