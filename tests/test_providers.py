"""Provider factory: config.provider + stored credentials -> the right provider,
with OAuth preferred over API key and clear errors when nothing is connected."""

from __future__ import annotations

import time

import pytest

from varyz.config import Config, LLMConfig
from varyz.llm.oauth import OAuthTokens
from varyz.llm.providers import NoCredentialsError, build_provider


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("varyz.secrets.get_secret", lambda n: store.get(n))
    monkeypatch.setattr("varyz.secrets.set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr("varyz.secrets.delete_secret", lambda n: store.pop(n, None))
    return store


def cfg_for(provider: str) -> Config:
    return Config(llm=LLMConfig(provider=provider))


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="claude | openai | deepseek"):
        build_provider(cfg_for("bard"), role="chat")


def test_claude_missing_credentials_names_connect():
    with pytest.raises(NoCredentialsError, match="varyz connect claude"):
        build_provider(cfg_for("claude"), role="chat")


def test_claude_prefers_oauth_over_api_key(fake_keyring):
    from varyz.llm import claude

    fake_keyring["anthropic_api_key"] = "sk-ant-xxx"
    fake_keyring[claude.TOKEN_SECRET] = OAuthTokens(
        access_token="oauth-tok", refresh_token="r", expires_at=time.time() + 999
    ).to_json()
    provider = build_provider(cfg_for("claude"), role="chat")
    assert provider.oauth is True
    assert provider.model == "claude-opus-5"


def test_claude_api_key_fallback(fake_keyring):
    fake_keyring["anthropic_api_key"] = "sk-ant-xxx"
    provider = build_provider(cfg_for("claude"), role="enrich")
    assert provider.oauth is False
    assert provider.api_key == "sk-ant-xxx"
    assert provider.model == "claude-haiku-4-5"


def test_deepseek_uses_key_and_base_url(fake_keyring):
    fake_keyring["deepseek_api_key"] = "ds-xxx"
    provider = build_provider(cfg_for("deepseek"), role="chat")
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.model == "deepseek-chat"


def test_deepseek_missing_key_errors():
    with pytest.raises(NoCredentialsError, match="varyz connect deepseek"):
        build_provider(cfg_for("deepseek"), role="chat")


def test_openai_oauth_preferred(fake_keyring):
    from varyz.llm import openai_oauth

    fake_keyring[openai_oauth.TOKEN_SECRET] = OAuthTokens(
        access_token="oa", refresh_token="r", expires_at=time.time() + 999,
        extra={"account_id": "acct_1"},
    ).to_json()
    provider = build_provider(cfg_for("openai"), role="chat")
    assert isinstance(provider, openai_oauth.OpenAIOAuthProvider)


def test_openai_api_key_fallback(fake_keyring):
    fake_keyring["openai_api_key"] = "sk-openai"
    provider = build_provider(cfg_for("openai"), role="chat")
    assert provider.base_url is None  # standard api.openai.com
    assert provider.model == "gpt-5.5"


def test_legacy_flat_models_still_honored():
    cfg = Config(llm=LLMConfig(provider="claude", chat_model="claude-legacy-x"))
    assert cfg.llm.models_for().chat_model == "claude-legacy-x"
    assert cfg.llm.models_for().enrich_model == "claude-haiku-4-5"
