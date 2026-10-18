"""Provider factory: turns config + stored credentials into a working provider.

Auth resolution per provider: OAuth session first (subscription), then API
key, then a clear error naming the connect command. The sensitivity gate in
router.py applies identically to every provider — all of them are cloud.
"""

from __future__ import annotations

from varyz import secrets
from varyz.config import Config


class NoCredentialsError(RuntimeError):
    def __init__(self, provider: str):
        super().__init__(
            f"No credentials for '{provider}'. Run: varyz connect {provider}"
        )


def provider_overview(cfg: Config) -> list[dict]:
    """Status of every provider for the CLI `connect status` and web settings."""
    from varyz import secrets
    from varyz.llm import claude, openai_oauth

    rows = []

    claude_oauth = claude.token_store().load() is not None
    claude_key = bool(secrets.get_secret("anthropic_api_key"))
    rows.append({
        "name": "claude", "label": "Claude (Anthropic)",
        "oauth": claude_oauth, "api_key": claude_key,
        "auth": "OAuth" if claude_oauth else "API key" if claude_key else None,
        "account": claude.account_label(),
        "connected": claude_oauth or claude_key,
        "active": cfg.llm.provider == "claude",
        "supports_oauth": True,
    })

    oa_oauth = openai_oauth.token_store().load() is not None
    oa_key = bool(secrets.get_secret("openai_api_key"))
    rows.append({
        "name": "openai", "label": "ChatGPT (OpenAI)",
        "oauth": oa_oauth, "api_key": oa_key,
        "auth": "OAuth" if oa_oauth else "API key" if oa_key else None,
        "account": openai_oauth.account_label(),
        "connected": oa_oauth or oa_key,
        "active": cfg.llm.provider == "openai",
        "supports_oauth": True,
    })

    ds_key = bool(secrets.get_secret("deepseek_api_key"))
    rows.append({
        "name": "deepseek", "label": "DeepSeek",
        "oauth": False, "api_key": ds_key,
        "auth": "API key" if ds_key else None,
        "account": None,
        "connected": ds_key,
        "active": cfg.llm.provider == "deepseek",
        "supports_oauth": False,
    })
    return rows


def build_provider(cfg: Config, role: str, max_tokens: int = 2048):
    name = cfg.llm.provider
    if name not in ("claude", "openai", "deepseek"):
        raise ValueError(f"[llm] provider must be claude | openai | deepseek, got '{name}'")
    models = cfg.llm.models_for(name)
    model = models.chat_model if role == "chat" else models.enrich_model

    if name == "claude":
        from varyz.llm.claude import ClaudeProvider, token_store

        if token_store().load() is not None:
            return ClaudeProvider(model, oauth=True, max_tokens=max_tokens)
        api_key = secrets.get_secret("anthropic_api_key")
        if api_key:
            return ClaudeProvider(model, api_key=api_key, max_tokens=max_tokens)
        raise NoCredentialsError("claude")

    if name == "openai":
        from varyz.llm import openai_oauth
        from varyz.llm.openai_compat import OpenAICompatProvider

        if openai_oauth.token_store().load() is not None:
            return openai_oauth.OpenAIOAuthProvider(model, max_tokens=max_tokens)
        api_key = secrets.get_secret("openai_api_key")
        if api_key:
            return OpenAICompatProvider(model, api_key=api_key, max_tokens=max_tokens)
        raise NoCredentialsError("openai")

    from varyz.llm.openai_compat import DEEPSEEK_BASE_URL, OpenAICompatProvider

    api_key = secrets.get_secret("deepseek_api_key")
    if api_key:
        return OpenAICompatProvider(
            model, api_key=api_key, base_url=DEEPSEEK_BASE_URL, max_tokens=max_tokens
        )
    raise NoCredentialsError("deepseek")
