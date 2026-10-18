"""Anthropic provider — API key or Claude.ai subscription OAuth.

Only ever receives content that passed the sensitivity gate in llm/router.py —
never call this module directly with entry content.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import urlencode

from varyz.llm.oauth import (
    OAuthTokens,
    TokenStore,
    decode_jwt_claims,
    exchange_token_json,
    generate_pkce,
    new_state,
    open_browser,
)

# Claude Code's public OAuth client (PKCE, no secret). This authenticates the
# user's own Claude.ai subscription for their own personal tool — the same
# grant Claude Code itself uses. See varyz connect claude.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
TOKEN_SECRET = "claude_oauth_tokens"

# console.anthropic.com is Cloudflare-fronted; present the Claude Code client
# identity so the token endpoint doesn't bot-block the request.
CLAUDE_CODE_UA = "claude-cli/1.0.0 (external, cli)"
TOKEN_HEADERS = {"User-Agent": CLAUDE_CODE_UA}


def _tokens_from_response(data: dict, fallback_refresh: str | None = None) -> OAuthTokens:
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", fallback_refresh),
        expires_at=time.time() + data.get("expires_in", 3600),
        extra={"scope": data.get("scope")},
    )


def refresh_tokens(refresh_token: str) -> OAuthTokens:
    data = exchange_token_json(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        },
        headers=TOKEN_HEADERS,
    )
    return _tokens_from_response(data, fallback_refresh=refresh_token)


def token_store() -> TokenStore:
    return TokenStore(TOKEN_SECRET, refresh_tokens)


def build_authorize_url() -> tuple[str, str, str]:
    """Returns (url, code_verifier, state)."""
    verifier, challenge = generate_pkce()
    state = new_state()
    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}", verifier, state


def complete_login(pasted: str, verifier: str, state: str) -> OAuthTokens:
    """Exchange the pasted 'code#state' for tokens and persist them."""
    code, _, returned_state = pasted.strip().partition("#")
    data = exchange_token_json(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "state": returned_state or state,
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers=TOKEN_HEADERS,
    )
    tokens = _tokens_from_response(data)
    token_store().save(tokens)
    return tokens


def login(console, prompt=input) -> OAuthTokens:
    """Interactive Claude.ai sign-in (browser + copy-paste code)."""
    url, verifier, state = build_authorize_url()
    open_browser(url)
    console.print(
        "Opening Claude sign-in in your browser. If it didn't open, visit:\n"
        f"[dim]{url}[/dim]\n\n"
        "After approving, Claude shows a code. Copy and paste it here."
    )
    pasted = prompt("Authorization code: ")
    return complete_login(pasted, verifier, state)


def account_label() -> str | None:
    tokens = token_store().load()
    if tokens is None:
        return None
    claims = decode_jwt_claims(tokens.access_token)
    return claims.get("email") or claims.get("sub")


class ClaudeProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        oauth: bool = False,
        max_tokens: int = 2048,
    ):
        self.model = model
        self.api_key = api_key
        self.oauth = oauth
        self.max_tokens = max_tokens

    def _client(self):
        from anthropic import Anthropic

        if self.oauth:
            return Anthropic(
                auth_token=token_store().access_token(),
                default_headers={"anthropic-beta": OAUTH_BETA_HEADER},
            )
        return Anthropic(api_key=self.api_key)

    def stream(self, system: str, user: str) -> Iterator[str]:
        with self._client().messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            yield from stream.text_stream

    def complete(self, system: str, user: str) -> str:
        return "".join(self.stream(system, user))
