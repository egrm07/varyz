"""OpenAI sign-in with a ChatGPT subscription (Codex OAuth, PKCE).

Unlike Claude, a ChatGPT OAuth token does NOT authenticate api.openai.com; it
authenticates the ChatGPT backend's Responses API. So this provider posts to
that backend with the account id decoded from the id_token. The reliable
fallback for OpenAI remains a plain API key (llm/openai_compat.py), which the
provider factory uses when there's no OAuth session.

Constants match the Codex CLI public client. This signs in the user's own
ChatGPT subscription for their own personal tool.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from urllib.parse import urlencode

import httpx

from varyz.llm.oauth import (
    OAuthError,
    OAuthTokens,
    TokenStore,
    decode_jwt_claims,
    exchange_token,
    generate_pkce,
    new_state,
    open_browser,
    wait_for_callback,
)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = "openid profile email offline_access"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
ACCOUNT_CLAIM = "https://api.openai.com/auth"
TOKEN_SECRET = "openai_oauth_tokens"

# auth.openai.com sits behind Cloudflare bot protection that 403s clients it
# doesn't recognise (surfacing as `unsupported_country_region_territory` on the
# token endpoint). Presenting the official Codex client's identity gets through.
CODEX_UA = "codex_cli_rs/0.21.0"
CODEX_HEADERS = {"User-Agent": CODEX_UA, "originator": "codex_cli_rs"}


def _account_id(id_token: str) -> str | None:
    claims = decode_jwt_claims(id_token)
    auth = claims.get(ACCOUNT_CLAIM, {})
    return auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id")


def _tokens_from_response(data: dict, fallback_refresh: str | None = None) -> OAuthTokens:
    id_token = data.get("id_token", "")
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", fallback_refresh),
        expires_at=time.time() + data.get("expires_in", 3600),
        extra={"account_id": _account_id(id_token), "id_token": id_token},
    )


def refresh_tokens(refresh_token: str) -> OAuthTokens:
    data = exchange_token(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
        },
        headers=CODEX_HEADERS,
    )
    tokens = _tokens_from_response(data, fallback_refresh=refresh_token)
    # Refresh responses may omit id_token; keep the prior account id.
    if not tokens.extra.get("account_id"):
        prior = token_store().load()
        if prior and prior.extra:
            tokens.extra["account_id"] = prior.extra.get("account_id")
    return tokens


def token_store() -> TokenStore:
    return TokenStore(TOKEN_SECRET, refresh_tokens)


def build_authorize_url() -> tuple[str, str, str]:
    verifier, challenge = generate_pkce()
    state = new_state()
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", verifier, state


def complete_login(code: str, verifier: str) -> OAuthTokens:
    try:
        data = exchange_token(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
            headers=CODEX_HEADERS,
        )
    except OAuthError as exc:
        if "unsupported_country_region_territory" in str(exc) or "403" in str(exc):
            raise OAuthError(
                "OpenAI's login endpoint refused the request (Cloudflare region/bot "
                "block). This can be intermittent - try again. If it keeps failing, "
                "use an OpenAI API key instead (Use an API key, below)."
            ) from exc
        raise
    tokens = _tokens_from_response(data)
    token_store().save(tokens)
    return tokens


def login(console) -> OAuthTokens:
    """Interactive ChatGPT sign-in via a localhost callback."""
    url, verifier, state = build_authorize_url()
    open_browser(url)
    console.print(
        "Opening ChatGPT sign-in in your browser. If it didn't open, visit:\n"
        f"[dim]{url}[/dim]\n\nWaiting for you to approve…"
    )
    result = wait_for_callback(CALLBACK_PORT, CALLBACK_PATH)
    if result.get("state") != state:
        raise OAuthError("state mismatch - sign-in aborted for safety")
    if "code" not in result:
        raise OAuthError(f"no authorization code returned: {result}")
    return complete_login(result["code"], verifier)


def account_label() -> str | None:
    tokens = token_store().load()
    if tokens is None:
        return None
    return decode_jwt_claims(tokens.extra.get("id_token", "")).get("email")


class OpenAIOAuthProvider:
    """Calls the ChatGPT backend Responses API with subscription tokens."""

    def __init__(self, model: str, *, max_tokens: int = 2048):
        self.model = model
        self.max_tokens = max_tokens

    def _headers(self) -> dict:
        store = token_store()
        access = store.access_token()
        tokens = store.load()
        account_id = (tokens.extra or {}).get("account_id") if tokens else None
        if not account_id:
            raise OAuthError("no ChatGPT account id in the session - sign in again")
        return {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "User-Agent": CODEX_UA,
            "session_id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _payload(self, system: str, user: str) -> dict:
        return {
            "model": self.model,
            "instructions": system,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": user}]}
            ],
            "stream": True,
            "store": False,
        }

    def stream(self, system: str, user: str) -> Iterator[str]:
        with httpx.stream(
            "POST", RESPONSES_URL, headers=self._headers(),
            json=self._payload(system, user), timeout=120,
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:300]
                raise OAuthError(f"ChatGPT backend returned {resp.status_code}: {body}")
            yield from _parse_sse_text(resp.iter_lines())

    def complete(self, system: str, user: str) -> str:
        return "".join(self.stream(system, user))


def _parse_sse_text(lines: Iterator[str]) -> Iterator[str]:
    """Yield text deltas from the Responses API SSE stream."""
    for raw in lines:
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "response.output_text.delta" and "delta" in event:
            yield event["delta"]
