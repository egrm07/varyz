from __future__ import annotations

import pytest
from pydantic import ValidationError

from varyz.config import ConversationsConfig, GraphConfig, ResolutionConfig


def test_resolution_defaults_to_loopback() -> None:
    config = ResolutionConfig()
    assert config.bind_host == "127.0.0.1"
    assert config.auth_mode == "session"


def test_resolution_refuses_wide_bind_with_session_auth() -> None:
    with pytest.raises(ValidationError, match="requires token auth"):
        ResolutionConfig(bind_host="0.0.0.0")


def test_resolution_allows_explicit_strong_auth_for_wide_bind() -> None:
    config = ResolutionConfig(bind_host="0.0.0.0", auth_mode="token")
    assert config.bind_host == "0.0.0.0"


def test_source_policy_names_are_canonicalized() -> None:
    graph = GraphConfig(excluded_sources=[" WhatsApp ", "TELEGRAM", "whatsapp"])
    conversations = ConversationsConfig(sources=[" AI_CONVERSATIONS ", "ai_conversations"])

    assert graph.excluded_sources == ["telegram", "whatsapp"]
    assert conversations.sources == ["ai_conversations"]


def test_conversations_read_agent_sessions_and_never_a_chat_app() -> None:
    assert ConversationsConfig().sources == ["ai_conversations"]
    # An older config that still names the chat apps reads none of them.
    older = ConversationsConfig(sources=["whatsapp", "Telegram", "ai_conversations"])
    assert older.sources == ["ai_conversations"]
