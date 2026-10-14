"""Secret-safe Google desktop OAuth and a small authenticated REST client.

The product distributor supplies one Desktop OAuth client registration.
End users only see Google's consent screen.  Account refresh tokens stay in
the OS credential manager and PKCE protects the browser authorization code.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets as random_secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from varyz import secrets

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CLIENT_SECRET_NAME = "google_oauth_client_json"

BASE_SCOPES = {
    "openid",
    "email",
    "profile",
}
SERVICE_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "google_calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "google_drive": "https://www.googleapis.com/auth/drive.readonly",
}


class GoogleSetupError(RuntimeError):
    pass


class GoogleAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OAuthAttempt:
    state: str
    verifier: str
    redirect_uri: str
    services: tuple[str, ...]


def token_secret_name(account: str) -> str:
    return f"google_oauth:{account.lower()}"


def store_client_config(raw: str | dict[str, Any]) -> None:
    data = json.loads(raw) if isinstance(raw, str) else raw
    _parse_client_data(data)
    secrets.set_secret(CLIENT_SECRET_NAME, json.dumps(data))


def client_config(base_dir: Path | None = None) -> dict[str, str]:
    """Resolve the app's Desktop OAuth registration without leaking it to TOML."""
    candidates: list[dict[str, Any]] = []
    stored = secrets.get_secret(CLIENT_SECRET_NAME)
    if stored:
        with contextlib.suppress(json.JSONDecodeError):
            candidates.append(json.loads(stored))
    configured_path = os.environ.get("VARYZ_GOOGLE_CLIENT_CONFIG")
    paths = [
        Path(configured_path) if configured_path else None,
        (base_dir / "google_oauth_client.json") if base_dir else None,
        Path(__file__).with_name("google_oauth_client.json"),
    ]
    for path in paths:
        if path and path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                candidates.append(json.loads(path.read_text(encoding="utf-8")))
    env_client_id = os.environ.get("VARYZ_GOOGLE_CLIENT_ID")
    if env_client_id:
        candidates.append(
            {
                "client_id": env_client_id,
                "client_secret": os.environ.get("VARYZ_GOOGLE_CLIENT_SECRET", ""),
            }
        )
    for candidate in candidates:
        try:
            return _parse_client_data(candidate)
        except GoogleSetupError:
            continue
    raise GoogleSetupError(
        "Google sign-in is ready but this build has no Desktop OAuth client. "
        "The app distributor must provide google_oauth_client.json once; end "
        "users will then connect with one approval. To point at a client file "
        "you already have, set VARYZ_GOOGLE_CLIENT_CONFIG to its path, or store "
        "it once with store_client_config() so it lives in the OS credential "
        "store rather than on disk."
    )


def _parse_client_data(data: dict[str, Any]) -> dict[str, str]:
    source = data.get("installed") or data
    client_id = str(source.get("client_id", "")).strip()
    if not client_id:
        raise GoogleSetupError("Google Desktop OAuth JSON has no client_id")
    return {
        "client_id": client_id,
        "client_secret": str(source.get("client_secret", "")),
        "auth_uri": str(source.get("auth_uri", AUTHORIZE_URL)),
        "token_uri": str(source.get("token_uri", TOKEN_URL)),
    }


def begin_oauth(
    services: list[str],
    redirect_uri: str,
    *,
    base_dir: Path | None = None,
) -> tuple[str, OAuthAttempt]:
    config = client_config(base_dir)
    selected = tuple(dict.fromkeys(service for service in services if service in SERVICE_SCOPES))
    if not selected:
        raise GoogleSetupError("Select at least one Google data source")
    verifier = random_secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = random_secrets.token_urlsafe(32)
    scopes = sorted(BASE_SCOPES | {SERVICE_SCOPES[name] for name in selected})
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return (
        f"{config['auth_uri']}?{urlencode(params)}",
        OAuthAttempt(state, verifier, redirect_uri, selected),
    )


def finish_oauth(
    code: str,
    attempt: OAuthAttempt,
    *,
    base_dir: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    config = client_config(base_dir)
    data = {
        "client_id": config["client_id"],
        "code": code,
        "code_verifier": attempt.verifier,
        "redirect_uri": attempt.redirect_uri,
        "grant_type": "authorization_code",
    }
    if config["client_secret"]:
        data["client_secret"] = config["client_secret"]
    with httpx.Client(timeout=30, transport=transport, trust_env=False) as client:
        response = client.post(config["token_uri"], data=data)
        if response.is_error:
            raise GoogleSetupError(_error_message(response, "Google token exchange failed"))
        token = response.json()
        user = client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        if user.is_error:
            raise GoogleSetupError(_error_message(user, "Google account lookup failed"))
        account = str(user.json().get("email", "")).strip().lower()
    if not account:
        raise GoogleSetupError("Google did not return an account email")
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    token["services"] = list(attempt.services)
    token["account"] = account
    secrets.set_secret(token_secret_name(account), json.dumps(token))
    return account


def connected(account: str) -> bool:
    return bool(secrets.get_secret(token_secret_name(account)))


def disconnect(account: str) -> None:
    secrets.delete_secret(token_secret_name(account))


class GoogleAPI:
    def __init__(
        self,
        account: str,
        *,
        base_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.account = account.lower()
        self.base_dir = base_dir
        self.transport = transport

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = self._token()
        with httpx.Client(
            timeout=60,
            follow_redirects=True,
            transport=self.transport,
            trust_env=False,
        ) as client:
            response = client.request(
                method,
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                response = client.request(
                    method,
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token(force=True)}"},
                )
        if response.is_error:
            raise GoogleAPIError(
                response.status_code,
                _error_message(response, f"Google API returned {response.status_code}"),
            )
        return response

    def json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", url, params=params).json()

    def bytes(self, url: str, *, params: dict[str, Any] | None = None) -> bytes:
        return self.request("GET", url, params=params).content

    def _token(self, *, force: bool = False) -> str:
        raw = secrets.get_secret(token_secret_name(self.account))
        if not raw:
            raise GoogleSetupError(
                f"Google account {self.account} is not connected in Varyz Imports"
            )
        token = json.loads(raw)
        if (
            not force
            and token.get("access_token")
            and float(token.get("expires_at", 0)) > time.time() + 60
        ):
            return str(token["access_token"])
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise GoogleSetupError("Google refresh token is missing; reconnect the account")
        config = client_config(self.base_dir)
        data = {
            "client_id": config["client_id"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if config["client_secret"]:
            data["client_secret"] = config["client_secret"]
        with httpx.Client(timeout=30, transport=self.transport, trust_env=False) as client:
            response = client.post(config["token_uri"], data=data)
        if response.is_error:
            raise GoogleSetupError(_error_message(response, "Google token refresh failed"))
        refreshed = response.json()
        token.update(refreshed)
        token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600))
        secrets.set_secret(token_secret_name(self.account), json.dumps(token))
        return str(token["access_token"])


def _error_message(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
        detail = data.get("error_description") or data.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status")
        if detail:
            return f"{fallback}: {detail}"
    except (ValueError, AttributeError):
        pass
    return fallback
