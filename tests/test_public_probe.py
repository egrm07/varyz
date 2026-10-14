"""Public release probe checks discovery separately from account tool access."""

import runpy
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from varyz.cloud.app import Settings, create_app

probe = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/plugin/probe_public.py")
)["probe"]


@pytest.fixture
def public_cloud(tmp_path):
    app = create_app(Settings(
        database=str(tmp_path / "cloud.db"),
        public_url="http://localhost",
        shared_varyz_enabled=True,
        archive_encryption_key=Fernet.generate_key().decode(),
    ))
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def test_public_probe_covers_every_hosted_mcp_surface(public_cloud):
    assert probe(public_cloud, "http://localhost") == [
        "/mcp", "/mcp/context", "/mcp/archive", "/mcp/plugin", "/mcp/shared",
    ]


@pytest.mark.parametrize("fault", ["successful_tool", "missing_challenge", "missing_scope"])
def test_public_probe_rejects_an_unprotected_plugin(public_cloud, monkeypatch, fault):
    original = public_cloud.post

    def altered(url, **kwargs):
        response = original(url, **kwargs)
        message = kwargs.get("json", {})
        if str(url).endswith("/mcp/plugin"):
            data = response.json()
            if message.get("method") == "tools/call":
                if fault == "successful_tool":
                    data["result"]["isError"] = False
                if fault == "missing_challenge":
                    data["result"].pop("_meta", None)
            if message.get("method") == "tools/list" and fault == "missing_scope":
                data["result"]["tools"][0].pop("securitySchemes", None)
            return httpx.Response(
                response.status_code, json=data, headers=response.headers, request=response.request
            )
        return response

    monkeypatch.setattr(public_cloud, "post", altered)
    with pytest.raises(RuntimeError, match="authentication not enforced|required OAuth scope"):
        probe(public_cloud, "http://localhost")
