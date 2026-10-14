"""Browser-bound social login; provider tokens never leave the server or persist.

Use provider subjects as identity keys. Existing email accounts must explicitly
link while signed in; a verified provider email alone cannot claim an account.
"""

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .browser import delete_cookie, read_cookie, set_cookie
from .store import digest
from .ui import provider_icon

PROVIDERS = {
    "google": (
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email",
    ),
    "github": (
        "GitHub",
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email",
    ),
}
FLOW_TTL = 600


class SocialAuth:
    def __init__(self, app, settings, store, page, csrf, form, account, sign_in):
        self.settings, self.store, self.page = settings, store, page
        self.csrf, self.form, self.account, self.sign_in = csrf, form, account, sign_in
        self.secure = settings.public_url.startswith("https://")
        self.http_client = lambda: httpx.AsyncClient(timeout=15, follow_redirects=False)
        app.add_api_route("/auth/complete", self.complete_page, methods=["GET"])
        app.add_api_route("/auth/complete", self.complete, methods=["POST"])
        app.add_api_route("/auth/{provider}/start", self.start, methods=["GET"])
        app.add_api_route("/auth/{provider}/link", self.link, methods=["POST"])
        app.add_api_route("/auth/{provider}/callback", self.callback, methods=["GET"])

    def available(self):
        return [
            {"id": key, "label": item[0]}
            for key, item in PROVIDERS.items()
            if getattr(self.settings, key + "_client_id")
            and getattr(self.settings, key + "_client_secret")
        ]

    def linked(self, owner):
        with self.store.connection() as db:
            return [
                row["provider"]
                for row in db.execute(
                    "SELECT provider FROM social_identities WHERE account=?", (owner,)
                )
            ]

    def form_targets(self):
        """Origins allowed by the document initiating an OAuth link form."""
        return tuple(
            f"{urlsplit(PROVIDERS[p['id']][1]).scheme}://{urlsplit(PROVIDERS[p['id']][1]).netloc}"
            for p in self.available()
        )

    def buttons(self):
        enabled = {p["id"] for p in self.available()}
        controls = []
        for key, provider in PROVIDERS.items():
            label = provider_icon(key) + f"<span>Sign in with {provider[0]}</span>"
            if key in enabled:
                controls.append(f'<a href="/auth/{key}/start">{label}</a>')
        if not controls:
            return ""
        return (
            '<div class="cloud-social">'
            + "".join(controls)
            + "</div>"
            + '<p class="cloud-auth-divider">or use email</p>'
        )

    def configured(self, provider):
        if provider not in {p["id"] for p in self.available()}:
            raise HTTPException(503, "This sign-in option is not available yet")

    def save_flow(self, params):
        token = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM oauth_flows WHERE expires<?", (time.time(),))
            if db.execute("SELECT COUNT(*) AS n FROM oauth_flows").fetchone()["n"] >= 10000:
                raise HTTPException(503, "Sign-in is temporarily busy")
            db.execute(
                "INSERT INTO oauth_flows VALUES(?,?,?)",
                (digest(token), json.dumps(params), time.time() + FLOW_TTL),
            )
        return token

    def cookie(self, response, name, token):
        set_cookie(
            response,
            name,
            token,
            secure=self.secure,
            httponly=True,
            samesite="lax",
            max_age=FLOW_TTL,
        )
        return response

    def begin(self, provider, owner=None, session=""):
        self.configured(provider)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = self.save_flow(
            {
                "phase": "social-login",
                "provider": provider,
                "verifier": verifier,
                "link_account": owner,
                "session_hash": digest(session) if owner else None,
            }
        )
        url = (
            PROVIDERS[provider][1]
            + "?"
            + urlencode(
                {
                    "client_id": getattr(self.settings, provider + "_client_id"),
                    "redirect_uri": self.settings.public_url + f"/auth/{provider}/callback",
                    "response_type": "code",
                    "scope": PROVIDERS[provider][3],
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        return self.cookie(RedirectResponse(url, status_code=303), "social_state", state)

    def start(self, provider: str):
        return self.begin(provider)

    async def link(self, provider: str, request: Request):
        self.configured(provider)
        self.csrf(request, await self.form(request))
        owner = self.account(request, browser=True, recent=True)
        request.state.form_targets = (
            PROVIDERS[provider][1].split("/", 3)[0]
            + "//"
            + PROVIDERS[provider][1].split("/", 3)[2],
        )
        return self.begin(provider, owner["id"], read_cookie(request, "session"))

    def flow(self, token, phase):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM oauth_flows WHERE hash=? AND expires>?", (digest(token), time.time())
            ).fetchone()
        if not row:
            raise HTTPException(400, "Sign-in expired. Please start again.")
        params = json.loads(row["params"])
        if params.get("phase") != phase:
            raise HTTPException(400, "Invalid sign-in flow")
        return params

    async def exchange(self, provider, code, verifier):
        async with self.http_client() as client:
            result = await client.post(
                PROVIDERS[provider][2],
                headers={"Accept": "application/json"},
                data={
                    "client_id": getattr(self.settings, provider + "_client_id"),
                    "client_secret": getattr(self.settings, provider + "_client_secret"),
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": self.settings.public_url + f"/auth/{provider}/callback",
                },
            )
            result.raise_for_status()
            token = result.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("Missing provider token")
            headers = {
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "User-Agent": "Varyz-Cloud",
            }
            if provider == "google":
                response = await client.get(
                    "https://openidconnect.googleapis.com/v1/userinfo", headers=headers
                )
                response.raise_for_status()
                data = response.json()
                if data.get("email_verified") is not True:
                    raise ValueError("Verified email required")
                subject, email = data.get("sub"), data.get("email")
            else:
                response = await client.get("https://api.github.com/user", headers=headers)
                response.raise_for_status()
                identifier = response.json().get("id")
                if type(identifier) is not int or identifier <= 0:
                    raise ValueError("Invalid provider identity")
                subject = str(identifier)
                response = await client.get("https://api.github.com/user/emails", headers=headers)
                response.raise_for_status()
                emails = response.json()
                email = next(
                    (
                        entry.get("email")
                        for entry in emails
                        if isinstance(entry, dict)
                        and entry.get("verified") is True
                        and entry.get("primary") is True
                    ),
                    None,
                )
            if (
                not isinstance(subject, str)
                or not 1 <= len(subject) <= 255
                or not isinstance(email, str)
                or not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,128}\.[^\s@]{1,64}", email)
            ):
                raise ValueError("Provider identity needs a verified email")
            return {"subject": subject, "email": email.lower()}

    async def callback(self, provider: str, request: Request):
        self.configured(provider)
        state = request.query_params.get("state", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state) or not hmac.compare_digest(
            state.encode(), read_cookie(request, "social_state").encode()
        ):
            raise HTTPException(400, "Invalid sign-in state")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_flows WHERE hash=? AND expires>?", (digest(state), time.time())
            ).fetchone()
            if not row:
                raise HTTPException(400, "Sign-in expired. Please start again.")
            flow = json.loads(row["params"])
            if flow.get("phase") != "social-login" or flow.get("provider") != provider:
                raise HTTPException(400, "Invalid sign-in flow")
            db.execute("DELETE FROM oauth_flows WHERE hash=?", (digest(state),))
        if request.query_params.get("error"):
            return self.error("Sign-in was canceled. You can try again.", 400)
        code = request.query_params.get("code", "")
        if not code or len(code) > 4096:
            raise HTTPException(400, "Missing sign-in code")
        try:
            identity = await self.exchange(provider, code, flow["verifier"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return self.error("Could not verify this sign-in. Please try again.", 502)
        if flow["link_account"]:
            owner = self.account(request, browser=True)
            if owner["id"] != flow["link_account"] or not hmac.compare_digest(
                digest(read_cookie(request, "session")), flow["session_hash"]
            ):
                raise HTTPException(403, "Sign in again before linking this account")
            try:
                with self.store.connection() as db:
                    db.execute(
                        "INSERT INTO social_identities VALUES(?,?,?)",
                        (provider, identity["subject"], owner["id"]),
                    )
            except sqlite3.IntegrityError:
                return self.error("This sign-in is already connected to an account.", 409)
            response = RedirectResponse("/", status_code=303)
        else:
            with self.store.connection() as db:
                owner = db.execute(
                    "SELECT a.* FROM accounts a JOIN social_identities s "
                    "ON s.account=a.id WHERE s.provider=? AND s.subject=?",
                    (provider, identity["subject"]),
                ).fetchone()
                email_owner = db.execute(
                    "SELECT id FROM accounts WHERE email=?", (identity["email"],)
                ).fetchone()
            if owner:
                response = self.sign_in(request, owner)
            elif email_owner:
                return self.error(
                    "An account already uses this email. Sign in with email, then connect "
                    + PROVIDERS[provider][0]
                    + " from your account page.",
                    409,
                )
            else:
                pending = self.save_flow(
                    {"phase": "social-signup", "provider": provider, **identity}
                )
                response = self.cookie(
                    RedirectResponse("/auth/complete", status_code=303), "social_signup", pending
                )
        delete_cookie(response, "social_state", secure=self.secure)
        return response

    def error(self, message, status):
        response = self.page(
            '<h1>Sign in</h1><p role="alert">'
            + html.escape(message)
            + '</p><a href="/login">Return to sign in</a>',
            auth=True,
        )
        response.status_code = status
        delete_cookie(response, "social_state", secure=self.secure)
        return response

    def complete_page(self, request: Request):
        self.flow(read_cookie(request, "social_signup"), "social-signup")
        return self.username_page()

    def username_page(self, message="", status=200):
        csrf = secrets.token_urlsafe(32)
        response = self.page(
            "<h1>Choose your username</h1>"
            + ('<p role="alert">' + html.escape(message) + "</p>" if message else "")
            + "<p>Your email is verified. Choose a unique name to finish creating your account.</p>"
            + '<form method="post" action="/auth/complete">'
            + f'<input type="hidden" name="csrf" value="{csrf}">'
            + '<label>Username<input name="username" autocomplete="username" minlength="3" '
            'maxlength="30" pattern="[A-Za-z0-9_]{3,30}" required></label>'
            "<p>3–30 letters, numbers or underscores. "
            "Capitalization does not make a different username.</p>"
            "<button>Create account</button></form>",
            auth=True,
        )
        response.status_code = status
        return self.cookie(response, "csrf", csrf)

    async def complete(self, request: Request):
        from .app import password_hash

        data = await self.form(request)
        self.csrf(request, data)
        token = read_cookie(request, "social_signup")
        username = data.get("username", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            self.flow(token, "social-signup")
            return self.username_page("Username must be 3–30 letters, numbers or underscores", 422)
        try:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                pending = db.execute(
                    "SELECT * FROM oauth_flows WHERE hash=? AND expires>?",
                    (digest(token), time.time()),
                ).fetchone()
                if not pending:
                    raise HTTPException(400, "Sign-in expired. Please start again.")
                identity = json.loads(pending["params"])
                if identity.get("phase") != "social-signup":
                    raise HTTPException(400, "Invalid sign-in flow")
                if (
                    db.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
                    >= self.settings.max_accounts
                ):
                    raise HTTPException(503, "New accounts are temporarily at capacity")
                owner = secrets.token_hex(16)
                db.execute(
                    "INSERT INTO accounts(id,email,password,username,email_verified) "
                    "VALUES(?,?,?,?,1)",
                    (owner, identity["email"], password_hash(secrets.token_urlsafe(48)), username),
                )
                db.execute(
                    "INSERT INTO social_identities VALUES(?,?,?)",
                    (identity["provider"], identity["subject"], owner),
                )
                db.execute("DELETE FROM oauth_flows WHERE hash=?", (digest(token),))
        except sqlite3.IntegrityError:
            return self.username_page(
                "That username or email is already registered. Choose another name or sign in.", 409
            )
        response = self.sign_in(request, {"id": owner})
        delete_cookie(response, "social_signup", secure=self.secure)
        return response
