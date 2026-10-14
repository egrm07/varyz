"""Dedicated browser/API and MCP resource for recipient consultation."""

import asyncio
import contextlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from . import sharing_models as models
from . import sharing_notify as notices
from . import sharing_ui as ui
from .app import SESSION_TTL
from .browser import delete_cookie, read_cookie, set_cookie
from .sharing_service import SCOPE, Sharing, SharingError, fail, identifier
from .store import digest

NOT_FOLLOWING = (
    "The person follows nobody yet. When they name a creator, call follow with action "
    "follow and the username."
)


def register_sharing(app, settings, store, archive, account, csrf, read_form, page):
    service = Sharing(store, archive, settings)
    app.state.sharing = service
    following = service.following
    secure = settings.public_url.startswith("https://")

    async def form(request):
        data = await read_form(request)
        request.state.sharing_form = data
        return data

    def browser_page(request):
        return request.url.path.startswith(
            (
                "/account/shares",
                "/account/shared",
                "/account/sharing",
                "/account/questions",
                "/invite/",
                "/account/public",
                "/account/following",
                "/@",
            )
        )

    def back_link(request):
        path = request.url.path
        if path.startswith("/account/public"):
            return "/account/public", "Back to Public Varyz"
        if path.startswith("/account/following"):
            return "/account/shared?tab=with-me#following", "Back to Following"
        if path.startswith("/@"):
            return "/@" + path[2:].split("/")[0], "Back to the profile"
        return "/account/shared", "Back to Shared"

    @app.exception_handler(HTTPException)
    async def browser_error(request, error):
        if request.url.path == "/login" and read_cookie(request, "sharing_draft"):
            return response(
                request,
                ui.shell(
                    "Sign-in didn’t finish",
                    '<p role="alert">' + ui.esc(error.detail) + "</p>"
                    "<p>Your saved sharing action has not been sent.</p>"
                    '<p><a class="shared-action" href="/login?reauth=1">'
                    "Try signing in again</a></p>",
                ),
                status=error.status_code,
            )
        if not browser_page(request):
            return await http_exception_handler(request, error)
        reauth = error.status_code == 403 and error.detail == (
            "Sign in again at /login?reauth=1 before this account action"
        )
        if reauth or error.status_code == 401:
            # Resuming must never overwrite the draft it is trying to restore.
            if request.url.path == "/account/sharing/resume":
                return RedirectResponse("/login?reauth=1", status_code=303)
            token = read_cookie(request, "session")
            with store.connection() as db:
                previous = (
                    db.execute(
                        "SELECT account FROM credentials WHERE hash=? AND kind='session'",
                        (digest(token),),
                    ).fetchone()
                    if token
                    else None
                )
            data = getattr(request.state, "sharing_form", None)
            recovery_owner = previous["account"] if previous else None
            if (
                not recovery_owner
                and data
                and data.get("sharing_owner", "").startswith("encrypted:")
            ):
                try:
                    hint = service.open(data["sharing_owner"])
                    if (
                        isinstance(hint, dict)
                        and hint.get("phase") == "sharing-form-owner"
                        and isinstance(hint.get("actor"), str)
                        and hint.get("expires", 0) > time.time()
                    ):
                        recovery_owner = hint.get("actor")
                except (InvalidToken, ValueError, TypeError, AttributeError):
                    pass
            # Confidential form drafts are always bound to a known account, even
            # when its session just expired. Never attach them to whoever signs in.
            saved = {
                "phase": "shared-browser-draft",
                "actor": recovery_owner,
                "path": request.url.path,
                "method": request.method,
                "query": str(request.url.query),
                "draft": {
                    k: v for k, v in (data or {}).items() if k not in ("csrf", "sharing_owner")
                }
                if recovery_owner
                else {},
            }
            if len(json.dumps(saved).encode()) > 32768:
                return response(
                    request,
                    ui.shell(
                        "Saved action is too large",
                        "<p>Nothing was sent. Shorten the form and try again.</p>",
                    ),
                    status=413,
                )
            peer = request.client.host if request.client else "unknown"
            if not store.allow_request("sharing-recovery:" + digest(token or peer), 60, 20):
                return response(
                    request,
                    ui.shell(
                        "Please try again shortly",
                        "<p>Too many sign-in recovery attempts. Wait a minute, then try again.</p>",
                    ),
                    status=429,
                )
            receipt = identifier()
            with service.transaction() as db:
                db.execute("DELETE FROM oauth_flows WHERE expires<?", (time.time(),))
                old = read_cookie(request, "sharing_draft")
                if old:
                    db.execute("DELETE FROM oauth_flows WHERE hash=?", (digest(old),))
                db.execute(
                    "INSERT INTO oauth_flows(hash,params,expires) VALUES(?,?,?)",
                    (digest(receipt), service.seal(saved), time.time() + 600),
                )
            result = RedirectResponse("/login?reauth=1", status_code=303)
            set_cookie(
                result,
                "sharing_draft",
                receipt,
                secure=secure,
                httponly=True,
                samesite="lax",
                max_age=600,
            )
            return result
        target, label = back_link(request)
        return response(
            request,
            ui.shell(
                "Couldn’t complete this action",
                '<p role="alert">' + ui.esc(error.detail) + "</p>"
                '<p><a href="' + ui.esc(target) + '">' + label + "</a></p>",
            ),
            status=error.status_code,
        )

    @app.exception_handler(SharingError)
    async def sharing_error(request, error):
        if browser_page(request):
            target, label = back_link(request)
            return response(
                request,
                ui.shell(
                    "Couldn’t complete this action",
                    '<p role="alert">' + ui.esc(error.message) + "</p>"
                    '<p><a href="' + ui.esc(target) + '">' + label + "</a></p>",
                ),
                status=error.status,
            )
        return JSONResponse(
            error.envelope(),
            status_code=error.status,
            headers={"Retry-After": "60"} if error.status == 429 else None,
        )

    def actor(request, *, owner=False, recent=False):
        return account(request, browser=True, paid=owner, recent=recent)["id"]

    def session(request):
        return digest(read_cookie(request, "session"))

    def response(request, content, *, status=200, **head):
        cookie_ttl = 1800
        if browser_page(request):
            try:
                owner = actor(request)
            except HTTPException:
                owner = None
            if owner:
                cookie_ttl = SESSION_TTL
            if owner and "<form" in content:
                hint = service.seal(
                    {"phase": "sharing-form-owner", "actor": owner, "expires": time.time() + 86400}
                )
                content = re.sub(
                    r"(<form\b[^>]*>)", lambda m: m[1] + ui.hidden("sharing_owner", hint), content
                )
        token = read_cookie(request, "csrf") or secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        request.state.dashboard_nonce = nonce
        content += '<script nonce="' + nonce + '">' + ui.behavior() + "</script>"
        value = page(content.replace("{{CSRF}}", ui.esc(token)), **head)
        value.status_code = status
        set_cookie(
            value, "csrf", token, secure=secure, httponly=True, samesite="lax", max_age=cookie_ttl
        )
        return value

    def validated(model, data):
        try:
            return model.model_validate(data).model_dump()
        except ValidationError:
            fail(422, "invalid_fields", "Check the required fields, lengths and versions")

    def integer(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            fail(422, "invalid_fields", "Check the required fields and versions")

    async def body(request, model):
        actor(request)
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            fail(422, "invalid_fields", "Expected a JSON object")
        csrf(request, {"csrf": request.headers.get("x-csrf-token", "")})
        return validated(model, value)

    def key(request):
        return request.headers.get("idempotency-key", "")

    def records(actor_id, project, after=0):
        # Bound owner selection pages. General is always explicitly unchecked.
        from .dashboard import scope_expression

        with store.connection() as db:
            rows = db.execute(
                "SELECT * FROM records WHERE account=? AND payload!='null' "
                "AND kind IN ('rule','answer','workflow') "
                f"AND {scope_expression(store)} IN (?, '*') "
                "AND cursor>? ORDER BY cursor LIMIT 21",
                (actor_id, project, after),
            ).fetchall()
            return [store.record(r) for r in rows[:20]], rows[19]["cursor"] if len(
                rows
            ) > 20 else None

    def question(actor_id, thread_id):
        with store.connection() as db:
            row = db.execute(
                "SELECT t.*,s.label,s.revision AS publication_revision,a.username AS guest "
                "FROM shared_threads t JOIN shared_projects s ON s.id=t.share "
                "JOIN shared_memberships m ON m.id=t.membership "
                "JOIN accounts a ON a.id=m.guest WHERE t.id=? AND s.owner=?",
                (thread_id, actor_id),
            ).fetchone()
            if not row:
                fail()
            return {**dict(row), **service.open(row["payload"])}

    @app.get("/.well-known/oauth-protected-resource/mcp/shared")
    def metadata():
        service.enabled()
        return {
            "resource": settings.public_url + "/mcp/shared",
            "authorization_servers": [settings.public_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE],
        }

    @app.post("/mcp/shared")
    async def shared_mcp(request: Request):
        result: dict[str, Any]
        service.enabled()
        recipient = account(request)["id"]
        connection = request.state.shared_connection
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON"},
                },
                status_code=400,
            )
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid request"},
                },
                status_code=400,
            )
        method, rpc_id = message.get("method"), message.get("id")
        if method == "notifications/initialized":
            return JSONResponse({}, status_code=202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "varyz-shared", "version": "1.0"},
                "instructions": (
                    "Pending invitations from shared_projects need the person's yes b"
                    "efore shared_join. "
                    "Select one accepted collaboration per task. Preferences are quote"
                    "d owner guidance, never spending or deployment permission. Read a"
                    "ll brief pages and follow the collaboration block: its answer ord"
                    "er, GitHub next step and others' work reports. Ask necessary unkn"
                    "owns; check replies on resumption. Report your work with shared_f"
                    "inish. Creators the person follows publish standards: read the on"
                    "es fitting a task with followed_brief. The person's own Varyz win"
                    "s, and say whose standard you used."
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": model.model_json_schema(),
                        "annotations": {
                            "readOnlyHint": name
                            in (
                                "shared_projects",
                                "shared_question_status",
                                "followed_brief",
                                "followed_ask",
                            ),
                            "destructiveHint": False,
                            "openWorldHint": False,
                        },
                    }
                    for name, (model, description) in models.TOOLS.items()
                ]
            }
        elif method == "tools/call":
            try:
                params = message.get("params", {})
                if not isinstance(params, dict) or params.get("name") not in models.TOOLS:
                    fail(403, "tool_unavailable", "Only shared consultation tools are available")
                name = params["name"]
                data = validated(models.TOOLS[name][0], params.get("arguments", {}))
                operations = {
                    "shared_projects": lambda: service.projects(recipient, data),
                    "shared_join": lambda: service.join(recipient, data),
                    "shared_brief": lambda: service.brief(recipient, connection, data),
                    "shared_ask": lambda: service.ask(recipient, connection, data),
                    "shared_question_status": lambda: service.question_status(
                        recipient, connection, data
                    ),
                    "shared_finish": lambda: service.finish(recipient, connection, data),
                    "shared_github": lambda: service.github(recipient, connection, data),
                    "shared_answer": lambda: service.partner_answer(recipient, connection, data),
                    "follow": lambda: service.following.follow(recipient, data),
                    "followed_brief": lambda: service.following.brief(
                        recipient, data["task"], data["topics"]
                    )
                    or {"standards": [], "following": [], "how": NOT_FOLLOWING},
                    "followed_ask": lambda: service.following.ask(
                        recipient, data["questions"], data["task"]
                    ),
                }
                value = await asyncio.to_thread(operations[name])
                result = {
                    "content": [{"type": "text", "text": json.dumps(value)}],
                    "structuredContent": value,
                }
            except SharingError as error:
                value = error.envelope()
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(value)}],
                    "structuredContent": value,
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": "Method unavailable"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    @app.post("/api/sharing/v1/shares/preview")
    async def preview_api(request: Request):
        data = await body(request, models.Preview)
        return service.preview(actor(request, owner=True), session(request), data)

    @app.post("/api/sharing/v1/shares")
    async def create_api(request: Request):
        data = await body(request, models.Publish)
        return service.create(actor(request, owner=True), session(request), data, key(request))

    @app.post("/api/sharing/v1/shares/{share_id}/preview")
    async def update_preview_api(share_id: str, request: Request):
        data = await body(request, models.Preview)
        return service.preview(actor(request, owner=True), session(request), data, share_id)

    @app.post("/api/sharing/v1/shares/{share_id}/publish")
    async def update_api(share_id: str, request: Request):
        data = await body(request, models.Update)
        return service.update(
            actor(request, owner=True), session(request), share_id, data, key(request)
        )

    @app.post("/api/sharing/v1/shares/{share_id}/invites")
    async def invite_api(share_id: str, request: Request):
        data = await body(request, models.Invite)
        return service.invite(actor(request, owner=True), share_id, data, key(request))

    @app.post("/api/sharing/v1/shares/{share_id}/state")
    async def state_api(share_id: str, request: Request):
        data = await body(request, models.State)
        return service.lifecycle(
            actor(request, recent=data["status"] == "deleted"), share_id, data, key(request)
        )

    @app.post("/api/sharing/v1/memberships/{membership_id}/revoke")
    async def revoke_api(membership_id: str, request: Request):
        data = await body(request, models.MemberState)
        return service.end_member(actor(request), membership_id, data, key(request))

    @app.post("/api/sharing/v1/memberships/{membership_id}/leave")
    async def leave_api(membership_id: str, request: Request):
        data = await body(request, models.MemberState)
        return service.end_member(actor(request), membership_id, data, key(request), leave=True)

    @app.post("/api/sharing/v1/questions/{thread_id}/reply")
    async def reply_api(thread_id: str, request: Request):
        data = await body(request, models.Reply)
        return service.reply(actor(request, owner=True), thread_id, data, key(request))

    @app.post("/api/sharing/v1/questions/{thread_id}/decline")
    async def decline_api(thread_id: str, request: Request):
        data = await body(request, models.Decline)
        return service.reply(
            actor(request, owner=True), thread_id, data, key(request), declined=True
        )

    @app.post("/api/sharing/v1/owner/collaboration")
    async def collaboration_api(request: Request):
        # Bearer access for the owner's local Varyz; the hosted owner MCP calls the service.
        owner = account(request, paid=True)["id"]
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            fail(422, "invalid_fields", "Expected a JSON object")
        data = validated(models.Collaboration, value)
        return await asyncio.to_thread(service.collaborate, owner, data)

    async def bearer_json(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            fail(422, "invalid_fields", "Expected a JSON object")
        if not isinstance(value, dict):
            fail(422, "invalid_fields", "Expected a JSON object")
        return value

    # Notices for local Varyz: desktop notifications and the email switch. Free accounts too.
    @app.get("/api/sharing/v1/notices")
    def notices_api(request: Request):
        service.enabled()
        return notices.desktop(service, account(request)["id"])

    @app.post("/api/sharing/v1/notices/seen")
    async def notices_seen_api(request: Request):
        service.enabled()
        person = account(request)["id"]
        ids = (await bearer_json(request)).get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) and len(i) <= 64 for i in ids):
            fail(422, "invalid_fields", "Pass ids as a list")
        return await asyncio.to_thread(notices.shown, service, person, ids)

    @app.post("/api/sharing/v1/notices/settings")
    async def notices_settings_api(request: Request):
        service.enabled()
        person = account(request)["id"]
        email = (await bearer_json(request)).get("email")
        if not isinstance(email, bool):
            fail(422, "invalid_fields", "Pass email as true or false")
        return await asyncio.to_thread(notices.set_email, service, person, email)

    @app.post("/api/sharing/v1/notices/test")
    async def notices_test_api(request: Request):
        service.enabled()
        return await asyncio.to_thread(notices.test_email, service, account(request)["id"])

    def unsubscribe_target(request):
        person = request.query_params.get("a", "")
        token = request.query_params.get("t", "")
        if not person or not hmac.compare_digest(
            token.encode(), service.mac("notices-off:" + person).encode()
        ):
            fail()
        return person, token

    @app.get("/notices/email")
    def notices_email_page(request: Request):
        person, token = unsubscribe_target(request)
        on = notices.desktop(service, person)["email"]
        action = "/notices/email?a=" + ui.esc(person) + "&amp;t=" + ui.esc(token)
        button = "Stop these emails" if on else "Turn these emails back on"
        content = (
            "<p>"
            + (
                "Varyz emails you when someone invites you to a shared project, asks you a "
                "question or answers yours."
                if on
                else "Shared project emails are off. Desktop notifications and your agent "
                "still tell you."
            )
            + '</p><form method="post" action="'
            + action
            + '"><input type="hidden" name="email" value="'
            + ("off" if on else "on")
            + '"><button>'
            + button
            + "</button></form>"
        )
        return page(ui.shell("Shared project emails", content))

    @app.post("/notices/email")
    async def notices_email_change(request: Request):
        person, _ = unsubscribe_target(request)
        data = await form(request)
        # A mail client's one-click unsubscribe posts List-Unsubscribe=One-Click.
        on = data.get("email") == "on"
        await asyncio.to_thread(notices.set_email, service, person, on)
        return page(
            ui.shell(
                "Shared project emails",
                "<p>"
                + (
                    "Emails are back on."
                    if on
                    else "Done. Varyz will not email you about shared projects. Desktop "
                    "notifications and your agent still tell you."
                )
                + "</p>",
            )
        )

    # Public Varyz: profiles at /@username, following by username, and the creator's
    # Choose, Review, Publish flow. Following is free; publishing needs the Cloud plan.

    def viewer(request):
        try:
            return actor(request)
        except HTTPException:
            return None

    def profile_name(username):
        name = username.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", name):
            fail(404, "no_profile", "No public Varyz has this name")
        return name

    def follow_and_return(request, person, name):
        try:
            following.follow(person, {"action": "follow", "username": name})
        except SharingError as error:
            return response(
                request,
                ui.shell(
                    "Couldn’t follow @" + name,
                    '<p role="alert">' + ui.esc(error.message) + "</p>"
                    '<p><a href="/@' + ui.esc(name) + '">Back to the profile</a></p>',
                ),
                status=error.status,
            )
        return RedirectResponse("/@" + name + "?followed=1", status_code=303)

    @app.get("/@{username}")
    def public_profile(username: str, request: Request):
        person = viewer(request)
        try:
            value = following.page(person, profile_name(username))
        except SharingError as error:
            if error.code != "no_profile":
                raise
            return response(
                request,
                ui.shell(
                    "Not found",
                    "<p>No public Varyz has this name. Check the spelling of the username.</p>"
                    '<p><a href="https://varyzai.com">What is Varyz?</a></p>',
                    nav=bool(person),
                ),
                status=404,
            )
        name = value["username"]
        if name != username:
            return RedirectResponse("/@" + name, status_code=301)
        titles = [topic["title"].lower() for topic in value["topics"]]
        description = (
            "The " + ", ".join(titles) + " standards @" + name + " actually applies. "
            "Follow to let your AI use them."
            if titles
            else "@" + name + "’s public Varyz: the standards they actually apply."
        )
        notice = (
            "You now follow @" + name + "."
            if request.query_params.get("followed") and value["following"]
            else None
        )
        return response(
            request,
            ui.profile(value, "{{CSRF}}", bool(person), notice),
            title="@" + name + " · Varyz",
            description=description,
            url=value["url"],
        )

    @app.post("/@{username}/follow")
    async def follow_page(username: str, request: Request):
        data = await form(request)
        csrf(request, data)
        name = profile_name(username)
        person = viewer(request)
        if person is not None:
            return await asyncio.to_thread(follow_and_return, request, person, name)
        # Signed out: sign in (or sign up) once, then the follow completes on return.
        service.enabled()
        peer = request.client.host if request.client else "unknown"
        if not store.allow_request("public-follow:" + digest(peer), 60, 20):
            fail(429, "rate_limited", "Too many attempts. Wait a minute, then try again.")
        continuation = identifier()
        with service.transaction() as db:
            db.execute(
                "INSERT INTO oauth_flows VALUES(?,?,?)",
                (
                    digest(continuation),
                    service.seal({"phase": "public-follow", "username": name}),
                    time.time() + 1800,
                ),
            )
        result = RedirectResponse("/login", status_code=303)
        set_cookie(
            result,
            "invite_return",
            continuation,
            secure=secure,
            httponly=True,
            samesite="lax",
            max_age=1800,
        )
        return result

    @app.post("/@{username}/unfollow")
    async def unfollow_page(username: str, request: Request):
        data = await form(request)
        csrf(request, data)
        name = profile_name(username)
        await asyncio.to_thread(
            following.follow, actor(request), {"action": "unfollow", "username": name}
        )
        if data.get("back") == "shared":
            return RedirectResponse("/account/shared?tab=with-me#following", status_code=303)
        return RedirectResponse("/@" + name, status_code=303)

    @app.post("/account/following")
    async def follow_by_name(request: Request):
        data = await form(request)
        csrf(request, data)
        person = actor(request)
        name = (data.get("username") or "").strip()
        validated(models.Follow, {"action": "follow", "username": name})
        return await asyncio.to_thread(follow_and_return, request, person, name.lstrip("@"))

    @app.post("/account/following/pick")
    async def follow_pick(request: Request):
        data = await form(request)
        csrf(request, data)
        person = actor(request)
        value = validated(
            models.Follow,
            {"action": "pick", "username": data.get("username"), "topic": data.get("topic")},
        )
        await asyncio.to_thread(following.follow, person, value)
        return RedirectResponse("/account/shared?tab=with-me#following", status_code=303)

    public_notices = {
        "published": "Published. Followers get these standards on their next matching task.",
        "removed": "Removed. Followers stop getting that topic now.",
        "off": "Public Varyz is off. Your page shows nothing and followers get nothing.",
    }

    @app.get("/account/public")
    def public_page(request: Request):
        service.enabled()
        owner = actor(request)
        value = following.status(owner)
        if not value["published"]:
            try:
                with store.connection() as db:
                    following.creator(db, owner)
            except SharingError as error:
                if error.code not in ("plan_required", "username_required"):
                    raise
                return response(request, ui.public_locked(error.message))
        notice = public_notices.get(request.query_params.get("done", ""))
        return response(
            request,
            ui.public_home(value, following.suggested_topics(owner), "{{CSRF}}", notice),
        )

    @app.get("/account/public/topic")
    def public_topic(request: Request):
        owner = actor(request)
        value = following.candidates(owner, request.query_params.get("name", ""))
        return response(request, ui.public_choose(value, "{{CSRF}}"))

    @app.post("/account/public/preview")
    async def public_preview(request: Request):
        raw = await request.form()
        if any(not isinstance(value, str) for _, value in raw.multi_items()):
            fail(422, "invalid_fields", "This form does not accept files")
        data = dict(raw)
        chosen = [str(value) for value in raw.getlist("selected")]
        data["selected"] = json.dumps(chosen)
        request.state.sharing_form = data
        csrf(request, data)
        owner = actor(request)
        topic = str(data.get("topic", ""))
        try:
            visible_count = min(300, max(
                ui.PUBLIC_CHOOSE_BATCH, int(data.get("visible_count", ui.PUBLIC_CHOOSE_BATCH))
            ))
        except (ValueError, TypeError):
            visible_count = ui.PUBLIC_CHOOSE_BATCH
        if data.get("intent") == "load-more":
            value = following.candidates(owner, topic)
            return response(
                request, ui.public_choose(value, "{{CSRF}}", selected=set(chosen),
                                          visible_count=visible_count + ui.PUBLIC_CHOOSE_BATCH)
            )
        try:
            selection = []
            for value in chosen:
                kind, record_id, version = value.split(":")
                selection.append(
                    validated(
                        models.SourceRef, {"kind": kind, "id": record_id, "version": int(version)}
                    )
                )
            preview = await asyncio.to_thread(
                following.preview, owner, session(request), topic, selection
            )
        except (SharingError, ValueError) as error:
            value = following.candidates(owner, topic)
            message = (
                error.message if isinstance(error, SharingError) else "Check the selected standards"
            )
            return response(
                request, ui.public_choose(value, "{{CSRF}}", message, set(chosen), visible_count),
                status=422
            )
        return response(request, ui.public_review(preview, "{{CSRF}}", preview["url"]))

    @app.post("/account/public/publish")
    async def public_publish(request: Request):
        data = await form(request)
        csrf(request, data)
        owner = actor(request)
        if data.get("reviewed") != "yes":
            fail(422, "confirmation_required", "Confirm you reviewed the exact text")
        await asyncio.to_thread(
            following.publish,
            owner,
            session(request),
            data.get("preview_id", ""),
            data.get("idempotency_key", ""),
        )
        return RedirectResponse("/account/public?done=published", status_code=303)

    @app.post("/account/public/remove")
    async def public_remove(request: Request):
        data = await form(request)
        csrf(request, data)
        await asyncio.to_thread(following.remove_topic, actor(request), data.get("topic", ""))
        return RedirectResponse("/account/public?done=removed", status_code=303)

    @app.post("/account/public/off")
    async def public_off(request: Request):
        data = await form(request)
        csrf(request, data)
        await asyncio.to_thread(following.unpublish, actor(request))
        return RedirectResponse("/account/public?done=off", status_code=303)

    # Following for local Varyz and other clients. Free accounts too.
    @app.post("/api/sharing/v1/following")
    async def following_api(request: Request):
        person = account(request)["id"]
        data = validated(models.Follow, await bearer_json(request))
        return await asyncio.to_thread(following.follow, person, data)

    @app.post("/api/sharing/v1/following/brief")
    async def following_brief_api(request: Request):
        person = account(request)["id"]
        data = validated(models.FollowedBrief, await bearer_json(request))
        value = await asyncio.to_thread(following.brief, person, data["task"], data["topics"])
        return value or {"standards": [], "following": [], "how": NOT_FOLLOWING}

    @app.post("/api/sharing/v1/following/ask")
    async def following_ask_api(request: Request):
        person = account(request)["id"]
        data = validated(models.FollowedAsk, await bearer_json(request))
        return await asyncio.to_thread(following.ask, person, data["questions"], data["task"])

    @app.get("/account/shares")
    def shares_page(request: Request):
        return shared_page(request)

    def shared_panel(name, active):
        return (
            '<section id="shared-panel-' + name + '" class="shared-section" '
            'role="tabpanel" aria-labelledby="shared-tab-'
            + name
            + '"'
            + (" hidden" if name != active else "")
            + ">"
        )

    def public_status(owner):
        if not settings.shared_varyz_enabled:
            return None
        try:
            with store.connection() as db:
                service.admitted(db, owner)
            return following.status(owner)
        except SharingError:
            return None

    def outgoing_projects(owner, active):
        with store.connection() as db:
            rows = db.execute(
                "SELECT id,label,status FROM shared_projects WHERE owner=? AND status!='deleted' "
                "ORDER BY id LIMIT 21",
                (owner,),
            ).fetchall()
        content = shared_panel("by-me", active)
        content += '<div class="shared-section-heading"><div><h2 id="by-me-title">By me</h2>'
        content += (
            '<p class="cloud-count">Projects you share · manage preferences and access</p></div>'
        )
        content += (
            '<a class="cloud-share-primary" href="/account/shares/new">Share a project</a>'
            if settings.shared_varyz_enabled
            else (
                "<p>New sharing is currently unavailable. Existing access can still be revoked.</p>"
            )
        )
        content += "</div>" + ui.public_card(public_status(owner))
        content += '<div class="shared-list-grid">' + "".join(
            '<article class="shared-row"><a href="/account/shares/'
            + ui.esc(r["id"])
            + '">'
            + ui.esc(r["label"])
            + '</a><p class="cloud-muted">'
            + ui.esc(r["status"])
            + '</p><a class="shared-card-action" href="/account/shares/'
            + ui.esc(r["id"])
            + '/people">Manage people</a></article>'
            for r in rows[:20]
        )
        content += "</div>"
        if not rows:
            content += (
                '<div class="shared-empty"><strong>No projects shared yet</strong>'
                "<p>Share selected preferences so someone’s AI knows how you want "
                "a project done.</p></div>"
            )
        if len(rows) > 20:
            content += '<p class="cloud-muted">Showing the first 20 shared projects.</p>'
        return content + "</section>"

    @app.get("/account/shares/new")
    def new_page(request: Request):
        service.enabled()
        owner = actor(request, owner=True)
        project = request.query_params.get("project")
        share_id = request.query_params.get("share_id")
        if share_id:
            share = service.owner_detail(owner, share_id)
            return response(
                request,
                ui.invitation(
                    share["project"],
                    "{{CSRF}}",
                    {
                        "share_id": share_id,
                        "explanation": share["project_context"]["explanation"],
                    },
                ),
            )
        if not project:
            from urllib.parse import urlencode

            from .dashboard import project_summaries

            groups = project_summaries(store, owner)
            content = "".join(
                '<a class="shared-project-choice" href="/account/shares/new?'
                + ui.esc(urlencode({"project": g["scope"]}))
                + '"><strong>'
                + ui.esc(g["scope"])
                + "</strong><span>"
                + str(g["count"])
                + " project preferences · invite someone →</span></a>"
                for g in groups
                if g["scope"] != "*"
            )
            return response(
                request,
                ui.shell(
                    "Choose a project",
                    "<p>Choose the project you want to share.</p>"
                    '<div class="shared-project-picker">' + content + "</div>"
                    if content
                    else "<p>No confirmed project preferences in Cloud yet.</p>",
                ),
            )
        return response(request, ui.invitation(project, "{{CSRF}}"))

    async def selection_form(request, share_id=None):
        raw = await request.form()
        if any(not isinstance(value, str) for _, value in raw.multi_items()):
            fail(422, "invalid_fields", "This sharing form does not accept files")
        data = dict(raw)
        data["selected"] = json.dumps(raw.getlist("selected"))
        request.state.sharing_form = data
        csrf(request, data)
        owner = actor(request, owner=True)
        share = service.owner_detail(owner, share_id) if share_id else None
        selection = []
        try:
            if data.get("step") == "more":
                values, next_after = records(
                    owner, data.get("project", ""), int(data.get("next_after", "0"))
                )
                data["selected"] = json.dumps(raw.getlist("selected"))
                return response(
                    request,
                    ui.selection(
                        values,
                        data.get("project", ""),
                        "{{CSRF}}",
                        share,
                        data,
                        next_after=next_after,
                    ),
                )
            for value in raw.getlist("selected"):
                kind, record_id, version = str(value).split(":")
                selection.append({"kind": kind, "id": record_id, "version": int(version)})
            payload = validated(
                models.Preview,
                {
                    "project": data.get("project"),
                    "label": data.get("label"),
                    "selection": selection,
                    "expected_publication_revision": integer(
                        data.get("expected_publication_revision")
                    )
                    if share
                    else None,
                },
            )
            preview = service.preview(owner, session(request), payload, share_id)
            return response(
                request,
                ui.review(
                    preview,
                    "{{CSRF}}",
                    data.get("invited_email", ""),
                    share_id,
                    data.get("level", "consult"),
                    "yes" if data.get("fill_gaps") == "yes" else "",
                ),
            )
        except (SharingError, ValueError) as error:
            values, _ = records(owner, data.get("project", ""))
            data["selected"] = json.dumps(raw.getlist("selected"))
            return response(
                request,
                ui.selection(
                    values,
                    data.get("project", ""),
                    "{{CSRF}}",
                    share,
                    data,
                    error.message
                    if isinstance(error, SharingError)
                    else "Check the selected preferences",
                ),
                status=422,
            )

    def invitation_confirmation(owner, result, recipient="They"):
        value = RedirectResponse("/account/shares/" + result["share_id"], status_code=303)
        set_cookie(
            value,
            "sharing_sent",
            service.seal(
                {
                    "actor": owner,
                    "share_id": result["share_id"],
                    "recipient": recipient,
                    "expires": time.time() + 120,
                }
            ),
            secure=secure,
            httponly=True,
            samesite="lax",
            max_age=120,
        )
        return value

    @app.post("/account/shares/new")
    async def new_preview(request: Request):
        data = await form(request)
        csrf(request, data)
        owner = actor(request, owner=True)
        try:
            payload = validated(
                models.ProjectInvitation,
                {
                    "project": data.get("project"),
                    "recipient": data.get("recipient"),
                    "level": data.get("level") or "consult",
                    "share_id": data.get("share_id") or None,
                    "explanation": data.get("explanation", ""),
                },
            )
            result = service.invite_project(owner, payload, data.get("idempotency_key", ""))
            return invitation_confirmation(owner, result, payload["recipient"])
        except SharingError as error:
            return response(
                request,
                ui.invitation(data.get("project", ""), "{{CSRF}}", data, error.message),
                status=error.status,
            )

    @app.get("/account/shares/{share_id}/edit")
    def edit_page(share_id: str, request: Request):
        share = service.owner_detail(actor(request), share_id)
        values, next_after = records(share["owner"], share["project"])
        with store.connection() as db:
            selected = [
                r["kind"] + ":" + r["source_id"] + ":" + str(r["version"])
                for r in db.execute(
                    "SELECT s.kind,s.source_id,r.version FROM shared_selections s JOIN records r "
                    "ON r.account=? AND r.kind=s.kind AND r.id=s.source_id "
                    "WHERE s.share=? AND s.origin='owner_confirmed_record' "
                    "AND s.suppressed IS NULL",
                    (share["owner"], share_id),
                ).fetchall()
            ]
        return response(
            request,
            ui.selection(
                values,
                share["project"],
                "{{CSRF}}",
                share,
                {"selected": json.dumps(selected)},
                next_after=next_after,
            ),
        )

    @app.post("/account/shares/{share_id}/preview")
    async def edit_preview(share_id: str, request: Request):
        return await selection_form(request, share_id)

    async def publish_form(request, share_id=None):
        data = await form(request)
        csrf(request, data)
        owner = actor(request, owner=True)
        if data.get("reviewed") != "yes":
            fail(422, "review_required", "Review the exact shared text first")
        try:
            if share_id:
                payload = validated(
                    models.Update,
                    {
                        "preview_id": data.get("preview_id"),
                        "expected_publication_revision": integer(
                            data.get("expected_publication_revision", "-1")
                        ),
                    },
                )
                service.update(
                    owner, session(request), share_id, payload, data.get("idempotency_key")
                )
                return RedirectResponse("/account/shares/" + share_id, status_code=303)
            payload = validated(
                models.Publish,
                {
                    **{
                        k: data.get(k)
                        for k in ("preview_id", "invited_email", "source_fingerprint")
                    },
                    "level": data.get("level") or "consult",
                    "fill_gaps": data.get("fill_gaps") == "yes",
                },
            )
            result = service.create(owner, session(request), payload, data.get("idempotency_key"))
            return invitation_confirmation(owner, result, data.get("invited_email", "They"))
        except SharingError as error:
            return response(
                request,
                ui.shell(
                    "Not published",
                    '<p role="alert">'
                    + ui.esc(error.message)
                    + (
                        "</p><p>Your reviewed version was not published. Go back to review"
                        " the selection again.</p>"
                    ),
                ),
                status=error.status,
            )

    @app.post("/account/shares/new/publish")
    async def new_publish(request: Request):
        return await publish_form(request)

    @app.post("/account/shares/{share_id}/publish")
    async def edit_publish(share_id: str, request: Request):
        return await publish_form(request, share_id)

    def people_controls(share):
        share_id = share["id"]
        members = [m for m in share["members"] if m["state"] == "active"]
        invitations = [
            i for i in share["invites"] if i["state"] == "pending" and i["expires"] > time.time()
        ]
        content = '<section class="shared-people"><h2>People with access</h2>'
        if not members and not invitations:
            content += '<p class="cloud-muted">No one has joined yet.</p>'
        for member in members:
            level = member.get("level") or "consult"
            gaps = member.get("fill_gaps") is None or bool(member.get("fill_gaps"))
            content += (
                '<article class="shared-person"><p><strong>'
                + ui.esc(member["username"])
                + "</strong>"
                + " · "
                + ui.esc(member["state"])
                + " · "
                + level.capitalize()
                + "</p>"
            )
            if member["state"] == "active":
                content += "<details><summary>Change permissions</summary>"
                content += ui.form(
                    "/account/sharing/action",
                    "{{CSRF}}",
                    ui.hidden("operation", "set_level")
                    + ui.hidden("share_id", share_id)
                    + ui.hidden("membership_id", member["id"])
                    + ui.level_fields(level, gaps),
                    "Save",
                )
                content += "</details><details><summary>Remove access</summary>"
                content += ui.form(
                    "/account/sharing/action",
                    "{{CSRF}}",
                    ui.hidden("operation", "revoke")
                    + ui.hidden("membership_id", member["id"])
                    + ui.hidden("expected_revision", member["revision"])
                    + (
                        '<label><input type="checkbox" name="confirm" required> Remove thi'
                        "s person’s access</label>"
                    ),
                    "Remove access",
                )
                content += "</details>"
            content += "</article>"
        if invitations:
            content += '<h3 class="shared-list-label">Invitations</h3>'
        for invite in invitations:
            state = (
                "expired"
                if invite["expires"] < time.time() and invite["state"] == "pending"
                else invite["state"]
            )
            content += (
                '<article class="shared-person shared-invite-row"><div><strong>'
                + ui.esc(invite["email"])
                + '</strong><span class="shared-person-state">'
                + ("Waiting for them to join" if state == "pending" else ui.esc(state.capitalize()))
                + "</span></div>"
            )
            if state == "pending":
                content += ui.form(
                    "/account/sharing/action",
                    "{{CSRF}}",
                    ui.hidden("operation", "revoke_invite") + ui.hidden("invite_id", invite["id"]),
                    "Revoke invitation",
                )
            content += "</article>"
        return content + "</section>"

    @app.get("/account/shares/{share_id}/people")
    def people_page(share_id: str, request: Request):
        share = service.owner_detail(actor(request), share_id)
        content = project_guidance(share) + people_controls(share)
        return response(
            request, ui.shell("Manage people · " + share["label"], content, project=True)
        )

    def project_guidance(share):
        from urllib.parse import urlencode

        joined = sum(m["state"] == "active" for m in share["members"])
        pending = sum(
            i["state"] == "pending" and i["expires"] > time.time() for i in share["invites"]
        )
        if share["status"] == "paused":
            message = "Sharing is paused. Resume access in Sharing settings when you’re ready."
        elif pending and not joined:
            message = "Invitation sent. Once they join, their AI can use the preferences you share."
        elif joined:
            message = "Manage who can use this project’s shared preferences."
        else:
            message = "Invite someone to use this project’s preferences with their AI."
        return (
            '<a class="shared-back" href="/account/shared?tab=by-me">&larr; Shared projects</a>'
            '<div class="shared-project-intro"><div><span class="shared-status">'
            + ("Sharing paused" if share["status"] == "paused" else "Sharing on")
            + "</span><p>"
            + ui.esc(message)
            + "</p></div>"
            + '<a class="shared-action" href="/account/shares/new?'
            + ui.esc(urlencode({"project": share["project"], "share_id": share["id"]}))
            + '">Invite someone else</a></div>'
        )

    @app.get("/account/shares/{share_id}")
    def share_page(share_id: str, request: Request):
        share = service.owner_detail(actor(request), share_id)
        content = project_guidance(share)
        content += ui.project_overview(share["project_context"])
        content += ui.context_editor(share, "{{CSRF}}")
        content += people_controls(share)
        count = len(share["items"])
        content += (
            '<details class="shared-disclosure shared-preferences"><summary>'
            '<span>Shared preferences</span><span class="shared-detail-count">'
            + str(count)
            + "</span></summary>"
            '<p class="cloud-muted">These are the instructions available to collaborators’ AIs. '
            "They are not a description of the project.</p>"
            + ui.items(share["items"])
            + '<a href="/account/shares/'
            + share_id
            + '/edit">Review updates</a>'
        )
        if share["pending_updates"]:
            content += (
                '<p class="shared-update-note">'
                + str(share["pending_updates"])
                + " preferences changed privately. Review updates to share the changes.</p>"
            )
        for item in share["items"]:
            if item["origin"] == "owner_shared_answer":
                content += "<details><summary>Remove a shared answer</summary>"
                content += ui.form(
                    "/account/sharing/action",
                    "{{CSRF}}",
                    ui.hidden("operation", "retract_answer")
                    + ui.hidden("share_id", share_id)
                    + ui.hidden("item_id", item["item_id"])
                    + ui.hidden("expected_publication_revision", share["revision"])
                    + '<label><input type="checkbox" name="confirm" required> '
                    "Remove shared answer: " + ui.esc(item["body"]["question"]) + "</label>",
                    "Remove this shared answer",
                )
                content += "</details>"
        content += "</details>"
        digest = share["collaboration"]
        needs_review = bool(digest["access_requests"] or digest["review"])
        content += (
            '<details class="shared-disclosure"'
            + (" open" if needs_review else "")
            + "><summary>Repository and activity"
            + (" · Needs your attention" if needs_review else "")
            + "</summary>"
            + ui.collaboration(share, "{{CSRF}}")
            + "</details>"
        )
        content += (
            '<details class="shared-disclosure shared-settings" id="sharing-settings">'
            "<summary>Sharing settings</summary>"
        )
        target = "paused" if share["status"] == "active" else "active"
        content += ui.form(
            "/account/sharing/action",
            "{{CSRF}}",
            ui.hidden("operation", "state")
            + ui.hidden("share_id", share_id)
            + ui.hidden("status", target)
            + ui.hidden("expected_publication_revision", share["revision"]),
            "Pause access" if target == "paused" else "Resume access",
        )
        content += (
            (
                "<details><summary>Erase this collaboration</summary><p>Erases its"
                " questions, replies and publications. Separately remembered owner"
                " preferences remain. Text already sent to another AI cannot be re"
                "called.</p>"
            )
            + ui.form(
                "/account/sharing/action",
                "{{CSRF}}",
                ui.hidden("operation", "state")
                + ui.hidden("share_id", share_id)
                + ui.hidden("status", "deleted")
                + ui.hidden("expected_publication_revision", share["revision"])
                + (
                    '<label><input type="checkbox" name="confirm" required> Erase this'
                    " collaboration</label>"
                ),
                "Erase collaboration",
            )
            + "</details>"
        )
        content += "</details>"
        notice = read_cookie(request, "sharing_sent")
        if notice:
            try:
                sent = service.open(notice)
                if (
                    sent.get("actor") == actor(request)
                    and sent.get("share_id") == share_id
                    and sent.get("expires", 0) > time.time()
                ):
                    content += ui.invitation_sent({**sent, "label": share["label"]})
            except (InvalidToken, ValueError, TypeError, AttributeError):
                pass
        value = response(request, ui.shell(share["label"], content, project=True))
        if notice:
            delete_cookie(value, "sharing_sent", secure=secure)
        return value

    @app.get("/invite/{token}")
    def invitation_page(token: str, request: Request):
        service.enabled()
        try:
            guest = actor(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            # Opaque continuation cookie; raw invite never appears in OAuth state/query.
            continuation = identifier()
            with service.transaction() as db:
                db.execute(
                    "INSERT INTO oauth_flows VALUES(?,?,?)",
                    (
                        digest(continuation),
                        service.seal({"phase": "shared-invite", "token": token}),
                        time.time() + 600,
                    ),
                )
            result = RedirectResponse("/login", status_code=303)
            set_cookie(
                result,
                "invite_return",
                continuation,
                secure=secure,
                httponly=True,
                samesite="lax",
                max_age=600,
            )
            return result
        with service.transaction() as db:
            _, share = service.inspect_invite(db, guest, token)
        receipt = identifier()
        with service.transaction() as db:
            db.execute(
                "INSERT INTO oauth_flows VALUES(?,?,?)",
                (
                    digest(receipt),
                    service.seal({"phase": "shared-accept", "actor": guest, "token": token}),
                    time.time() + 600,
                ),
            )
        # Keep the token out of the form document. no-referrer otherwise creates
        # Origin:null on submission, which the existing CSRF policy correctly denies.
        return RedirectResponse("/account/shared/invitations/" + receipt, status_code=303)

    def invite_receipt(request, receipt):
        guest = actor(request)
        with store.connection() as db:
            row = db.execute(
                "SELECT params FROM oauth_flows WHERE hash=? AND expires>?",
                (digest(receipt), time.time()),
            ).fetchone()
        if not row:
            fail()
        value = service.open(row["params"])
        if value.get("phase") != "shared-accept" or value.get("actor") != guest:
            fail()
        return guest, value["token"]

    @app.get("/account/shared/invitations/{receipt}")
    def invite_confirmation(receipt: str, request: Request):
        guest, token = invite_receipt(request, receipt)
        with service.transaction() as db:
            _, share = service.inspect_invite(db, guest, token)
            owner = db.execute(
                "SELECT username FROM accounts WHERE id=?", (share["owner"],)
            ).fetchone()
        return response(
            request,
            ui.shell(
                "Accept invitation",
                "<p>"
                + ui.esc(owner["username"] or "Owner")
                + " invited you to "
                + ui.esc(share["label"])
                + ".</p><p>Your AI can consult selected preferences and ask the owner questions. "
                "It cannot edit their Varyz.</p>"
                + ui.form(
                    "/account/shared/invitations/" + receipt + "/accept",
                    "{{CSRF}}",
                    "",
                    "Accept invitation",
                ),
            ),
        )

    @app.post("/account/shared/invitations/{receipt}/accept")
    async def confirm_accept(receipt: str, request: Request):
        data = await form(request)
        csrf(request, data)
        guest, token = invite_receipt(request, receipt)
        result = service.accept(guest, token, data.get("idempotency_key", ""))
        return RedirectResponse("/account/shared/" + result["membership_id"], status_code=303)

    @app.post("/invite/{token}/accept")
    async def accept_page(token: str, request: Request):
        data = await form(request)
        csrf(request, data)
        result = service.accept(actor(request), token, data.get("idempotency_key", ""))
        return RedirectResponse("/account/shared/" + result["membership_id"], status_code=303)

    @app.get("/account/shared")
    def shared_page(request: Request):
        guest = actor(request)
        continuation = read_cookie(request, "invite_return")
        if continuation:
            resumed: dict[str, Any] = {}
            with service.transaction() as db:
                row = db.execute(
                    "SELECT params FROM oauth_flows WHERE hash=? AND expires>?",
                    (digest(continuation), time.time()),
                ).fetchone()
                if row:
                    resumed = service.open(row["params"])
                    if resumed.get("phase") in ("shared-invite", "public-follow"):
                        db.execute("DELETE FROM oauth_flows WHERE hash=?", (digest(continuation),))
            if resumed.get("phase") == "shared-invite":
                result = RedirectResponse("/invite/" + resumed["token"], status_code=303)
                delete_cookie(result, "invite_return", secure=secure)
                return result
            if resumed.get("phase") == "public-follow":
                result = follow_and_return(request, guest, resumed["username"])
                delete_cookie(result, "invite_return", secure=secure)
                return result
        incoming_unavailable = False
        try:
            data = service.projects(guest, {"cursor": request.query_params.get("cursor")})
        except SharingError as error:
            if error.code != "sharing_disabled":
                raise
            incoming_unavailable = True
            data = {"memberships": [], "invitations": [], "next_cursor": None}
        active = request.query_params.get("tab")
        if active not in {"by-me", "with-me"}:
            active = "with-me" if request.query_params.get("cursor") else "by-me"
        content = (
            "<p>Project preferences, shared in either direction. "
            "You decide what others can use.</p>"
            '<div class="shared-section-switch" role="tablist" aria-label="Shared projects">'
        )
        for name, label in (("by-me", "By me"), ("with-me", "With me")):
            selected = name == active
            content += (
                '<a role="tab" id="shared-tab-'
                + name
                + '" aria-controls="shared-panel-'
                + name
                + '" aria-selected="'
                + str(selected).lower()
                + '" tabindex="'
                + ("0" if selected else "-1")
                + '" href="?tab='
                + name
                + "#"
                + name
                + '">'
                + label
                + "</a>"
            )
        content += "</div>" + outgoing_projects(guest, active)
        content += (
            shared_panel("with-me", active)
            + '<div class="shared-section-heading"><div><h2 id="with-me-title">With me</h2>'
            '<p class="cloud-count">Invitations and projects you joined · free with your '
            'Cloud account</p></div></div><div class="shared-list-grid">'
        )
        for invite in data.get("invitations") or []:
            content += (
                '<article class="shared-row"><p>'
                + ui.esc(invite["owner_label"] or "Owner")
                + " invited you to "
                + ui.esc(invite["collaboration_label"])
                + " at "
                + ui.esc((invite["level"] or "consult").capitalize())
                + ".</p>"
                + ui.form(
                    "/account/shared/join/" + invite["invite_id"],
                    "{{CSRF}}",
                    "",
                    "Join",
                )
                + "</article>"
            )
        for member in data["memberships"]:
            content += (
                '<article class="shared-row"><a href="/account/shared/'
                + member["membership_id"]
                + '">'
                + ui.esc(member["collaboration_label"])
                + "</a><p>Shared by "
                + ui.esc(member["owner_label"] or "Owner")
                + " · "
                + ui.esc(member["share_state"])
                + '</p><a class="shared-card-action" href="/account/shared/'
                + ui.esc(member["membership_id"])
                + '/leave">Leave project</a></article>'
            )
        content += "</div>"
        if not data["memberships"] and not data.get("invitations"):
            content += (
                '<div class="shared-empty"><strong>'
                "Shared projects are currently unavailable</strong>"
                "<p>Open By me to manage access to your existing projects.</p>"
                "</div>"
                if incoming_unavailable
                else '<div class="shared-empty"><strong>Nothing shared with you yet.</strong>'
                "<p>When someone invites you, their project appears here. Join to use "
                "its preferences with your AI.</p></div>"
            )
        if data["next_cursor"]:
            content += (
                '<a href="?tab=with-me&amp;cursor='
                + ui.esc(data["next_cursor"])
                + '#with-me">Next page</a>'
            )
        with contextlib.suppress(SharingError):
            content += ui.following_section(following.following(guest), "{{CSRF}}")
        return response(request, ui.shell("Shared", content + "</section>"))

    @app.post("/account/shared/join/{invite_id}")
    async def join_page(invite_id: str, request: Request):
        data = await form(request)
        csrf(request, data)
        # Same key as the agent's shared_join, so joining twice either way is one join.
        result = service.accept(actor(request), None, "shared-join:" + invite_id, invite_id)
        return RedirectResponse("/account/shared/" + result["membership_id"], status_code=303)

    @app.get("/api/sharing/v1/memberships/{membership_id}/connection")
    def connection_status_api(membership_id: str, request: Request):
        guest = actor(request)
        with service.transaction() as db:
            _, share = service.member(db, guest, membership_id)
            service.suppress(db, share)
            return service.connection_status(db, guest, membership_id, share["revision"])

    @app.get("/account/shared/{membership_id}/leave")
    def leave_confirmation(membership_id: str, request: Request):
        guest = actor(request)
        # Ending access must work even when sharing or the owner's access is paused.
        with store.connection() as db:
            member = db.execute(
                "SELECT m.id,m.revision,s.label FROM shared_memberships m "
                "JOIN shared_projects s ON s.id=m.share "
                "WHERE m.id=? AND m.guest=? AND m.state='active' AND s.status!='deleted'",
                (membership_id, guest),
            ).fetchone()
        if not member:
            fail()
        content = (
            "<p>You and your AI will lose access to this project’s shared preferences. "
            "You’ll need a new invitation to join again.</p>"
            + ui.form(
                "/account/sharing/action",
                "{{CSRF}}",
                ui.hidden("operation", "leave")
                + ui.hidden("membership_id", membership_id)
                + ui.hidden("expected_revision", member["revision"])
                + ui.hidden("confirm", "1")
                + '<a class="shared-cancel" href="/account/shared?tab=with-me">Cancel</a>',
                "Leave project",
            )
        )
        return response(request, ui.shell("Leave " + member["label"] + "?", content))

    @app.get("/account/shared/{membership_id}")
    def guest_page(membership_id: str, request: Request):
        guest = actor(request)
        with service.transaction() as db:
            member, share = service.member(db, guest, membership_id)
            service.suppress(db, share)
            values = service.publication(db, share)
            rows = db.execute(
                "SELECT * FROM shared_threads WHERE membership=? ORDER BY updated DESC LIMIT 20",
                (membership_id,),
            ).fetchall()
            connection = service.connection_status(db, guest, membership_id, share["revision"])
            owner = db.execute(
                "SELECT username FROM accounts WHERE id=?", (share["owner"],)
            ).fetchone()
            content = (
                "<p>Shared by "
                + ui.esc(owner["username"] or "Owner")
                + " · published version "
                + str(share["revision"])
                + "</p>"
            )
            content += ui.onboarding(
                {**share, "membership_id": membership_id},
                owner["username"] or "Owner",
                connection,
                settings.shared_varyz_plugin_url,
                settings.public_url + "/mcp/shared",
            )
            from .sharing_context import overview

            content += ui.project_overview(overview(service, db, share))
            content += "<h2>Shared preferences</h2>" + ui.items(values)
            if rows:
                content += "<h2>Your questions</h2>"
            for row in rows:
                request_data = service.open(row["payload"])
                content += (
                    '<article class="shared-row"><h3>'
                    + ui.esc(request_data["question"])
                    + "</h3><p>"
                    + ui.esc(row["status"])
                    + "</p>"
                )
                reply = db.execute(
                    "SELECT payload FROM shared_replies WHERE thread=?", (row["id"],)
                ).fetchone()
                if reply:
                    content += "<p>" + ui.esc(service.open(reply["payload"])["answer"]) + "</p>"
                if row["status"] == "open":
                    content += ui.form(
                        "/account/sharing/action",
                        "{{CSRF}}",
                        ui.hidden("operation", "withdraw")
                        + ui.hidden("membership_id", membership_id)
                        + ui.hidden("thread_id", row["id"])
                        + ui.hidden("expected_thread_version", row["version"]),
                        "Withdraw question",
                    )
                content += "</article>"
        content += (
            '<p><a class="shared-card-action" href="/account/shared/'
            + ui.esc(membership_id)
            + '/leave">Leave project</a></p>'
        )
        return response(request, ui.shell(share["label"], content))

    @app.post("/account/shares/{share_id}/context")
    async def save_project_context(share_id: str, request: Request):
        from .sharing_context import approve

        data = await form(request)
        csrf(request, data)
        owner = actor(request, owner=True)
        explanation = validated(
            models.ProjectContext,
            {
                "explanation": data.get("explanation", ""),
            },
        )["explanation"]
        await asyncio.to_thread(
            approve,
            service,
            owner,
            share_id,
            explanation,
            data.get("fingerprint", ""),
            data.get("include_story") == "yes",
        )
        return RedirectResponse("/account/shares/" + share_id, status_code=303)

    @app.get("/account/questions")
    def questions_page(request: Request):
        owner = actor(request)
        after = request.query_params.get("after", "")[:128]
        with store.connection() as db:
            rows = db.execute(
                "SELECT t.id,s.label,a.username FROM shared_threads t JOIN shared_projects s "
                "ON s.id=t.share JOIN shared_memberships m ON m.id=t.membership "
                "JOIN accounts a ON a.id=m.guest WHERE s.owner=? AND t.status='open' "
                "AND t.id>? ORDER BY t.id LIMIT 21",
                (owner, after),
            ).fetchall()
            personal = db.execute(
                "SELECT id,question,project FROM questions WHERE account=? AND answered=0 LIMIT 20",
                (owner,),
            ).fetchall()
        content = "<h2>Collaborators</h2>" + "".join(
            '<p><a href="/account/questions/'
            + r["id"]
            + '">'
            + ui.esc(r["label"])
            + " · "
            + ui.esc(r["username"])
            + "</a></p>"
            for r in rows[:20]
        )
        if not rows:
            content += "<p>No unanswered collaborator questions.</p>"
        if len(rows) > 20:
            content += '<a href="?after=' + ui.esc(rows[19]["id"]) + '">Next page</a>'
        content += (
            "<h2>Your personal questions</h2>"
            + "".join(
                "<p>" + ui.esc(r["project"]) + " · " + ui.esc(r["question"]) + "</p>"
                for r in personal
            )
            + '<a href="/?view=dashboard">Manage personal questions</a>'
        )
        return response(request, ui.shell("Questions", content))

    @app.get("/account/questions/{thread_id}")
    def question_page(thread_id: str, request: Request):
        thread = question(actor(request), thread_id)
        if thread["status"] != "open":
            return response(
                request,
                ui.shell(
                    "Question " + thread["status"],
                    "<p>This question has been resolved or access has ended.</p>",
                ),
            )
        return response(request, ui.reply_form(thread, "{{CSRF}}"))

    async def reply_form(request, thread_id, declined=False):
        data = await form(request)
        csrf(request, data)
        owner = actor(request)
        try:
            if declined:
                payload = validated(
                    models.Decline,
                    {
                        "expected_thread_version": int(data.get("expected_thread_version", "-1")),
                        "explanation": data.get("explanation", ""),
                    },
                )
            else:
                publish = data.get("publish_to_share") == "1"
                payload = validated(
                    models.Reply,
                    {
                        "expected_thread_version": int(data.get("expected_thread_version", "-1")),
                        "answer": data.get("answer", ""),
                        "remember_project": data.get("remember_project") == "1",
                        "publish_to_share": publish,
                        "expected_publication_revision": int(data.get("publication_revision", "-1"))
                        if publish
                        else None,
                    },
                )
            service.reply(
                owner, thread_id, payload, data.get("idempotency_key", ""), declined=declined
            )
            return RedirectResponse("/account/questions", status_code=303)
        except (SharingError, ValueError) as error:
            return response(
                request,
                ui.reply_form(
                    question(owner, thread_id),
                    "{{CSRF}}",
                    data,
                    error.message if isinstance(error, SharingError) else "Check the reply fields",
                ),
                status=error.status if isinstance(error, SharingError) else 422,
            )

    @app.post("/account/questions/{thread_id}/reply")
    async def reply_page(thread_id: str, request: Request):
        return await reply_form(request, thread_id)

    @app.get("/account/sharing/resume")
    def resume_reply(request: Request):
        owner = actor(request)
        receipt = read_cookie(request, "sharing_draft")
        with service.transaction() as db:
            row = db.execute(
                "SELECT params FROM oauth_flows WHERE hash=? AND expires>?",
                (digest(receipt), time.time()),
            ).fetchone()
            if not row:
                result = response(
                    request,
                    ui.shell(
                        "Saved action expired",
                        "<p>Nothing was sent. For your privacy, saved actions last ten minutes.</p>"
                        '<p><a class="shared-action" href="/account/shared">Back to Shared</a></p>',
                    ),
                    status=410,
                )
                delete_cookie(result, "sharing_draft", secure=secure)
                return result
            saved = service.open(row["params"])
            if saved.get("phase") not in ("shared-reply-draft", "shared-browser-draft"):
                fail()
            if saved.get("actor") and saved["actor"] != owner:
                return response(
                    request,
                    ui.shell(
                        "Use the same account",
                        "<p>This saved action belongs to the account you were using before. "
                        "Nothing was sent. Sign in to that account to continue.</p>"
                        '<p><a class="shared-action" href="/login?reauth=1">Switch account</a></p>',
                    ),
                    status=403,
                )
            db.execute("DELETE FROM oauth_flows WHERE hash=?", (digest(receipt),))
        if saved["phase"] == "shared-browser-draft":
            draft = saved.get("draft", {})
            path = saved["path"]
            if path == "/account/shares/new" and draft:
                result = response(
                    request,
                    ui.invitation(
                        draft.get("project", ""),
                        "{{CSRF}}",
                        draft,
                        "Sign-in completed. Review and send your invitation.",
                    ),
                )
            elif path == "/account/sharing/action" and draft:
                result = response(request, ui.pending_action(draft, "{{CSRF}}"))
            elif (
                path.startswith("/account/questions/")
                and path.endswith(("/reply", "/decline"))
                and draft
            ):
                result = response(
                    request,
                    ui.reply_form(
                        question(owner, path.split("/")[-2]),
                        "{{CSRF}}",
                        draft,
                        "Sign-in completed. Review your reply before sending.",
                    ),
                )
            elif path.startswith("/account/shares/") and path.endswith("/context") and draft:
                share = service.owner_detail(owner, path.split("/")[-2])
                result = response(
                    request,
                    ui.shell(
                        "Review shared context",
                        "<p>Sign-in completed. Review before saving; nothing was sent.</p>"
                        + ui.context_editor(share, "{{CSRF}}", draft),
                    ),
                )
            elif path.startswith("/account/shares/") and path.endswith("/preview") and draft:
                share = service.owner_detail(owner, path.split("/")[-2])
                values, next_after = records(owner, share["project"])
                result = response(
                    request,
                    ui.selection(
                        values,
                        share["project"],
                        "{{CSRF}}",
                        share,
                        draft,
                        "Sign-in completed. Review the preferences before continuing.",
                        next_after=next_after,
                    ),
                )
            elif path.startswith(("/account/public", "/account/following", "/@")):
                # Public Varyz forms are one click each: reopen the page, never replay.
                if path.startswith("/@"):
                    destination = "/@" + path[2:].split("/")[0]
                elif path.startswith("/account/following"):
                    destination = "/account/shared?tab=with-me#following"
                elif path == "/account/public/topic" and saved.get("method") == "GET":
                    destination = path + ("?" + saved["query"] if saved.get("query") else "")
                else:
                    destination = "/account/public"
                result = RedirectResponse(destination, status_code=303)
            elif path.endswith("/publish") and draft:
                # Publication previews are bound to the old session. Rebuild the
                # preview under the new session; never transplant its authority.
                with store.connection() as db:
                    preview_row = db.execute(
                        "SELECT payload FROM shared_previews WHERE id=? AND owner=? AND expires>?",
                        (draft.get("preview_id", ""), owner, time.time()),
                    ).fetchone()
                if not preview_row:
                    result = response(
                        request,
                        ui.shell(
                            "Review sharing again",
                            "<p>Your sharing preview expired. Nothing was sent.</p>"
                            '<p><a href="/account/shared">Back to Shared</a></p>',
                        ),
                        status=410,
                    )
                else:
                    previous = service.open(preview_row["payload"])
                    share_id = (
                        None if path == "/account/shares/new/publish" else path.split("/")[-2]
                    )
                    payload = validated(
                        models.Preview,
                        {
                            "project": previous["project"],
                            "label": previous["label"],
                            "selection": [
                                {k: s[k] for k in ("kind", "id", "version")}
                                for s in previous["selection"]
                            ],
                            "expected_publication_revision": previous[
                                "expected_publication_revision"
                            ],
                        },
                    )
                    preview = service.preview(owner, session(request), payload, share_id)
                    result = response(
                        request,
                        ui.review(
                            preview,
                            "{{CSRF}}",
                            draft.get("invited_email", ""),
                            share_id,
                            draft.get("level", "consult"),
                            draft.get("fill_gaps", ""),
                        ),
                    )
            elif not draft and saved.get("method") == "POST":
                result = response(
                    request,
                    ui.shell(
                        "Review sharing again",
                        "<p>Nothing was sent. This form could not be restored safely. "
                        "Open it again to continue.</p>"
                        '<p><a class="shared-action" href="/account/shared">Back to Shared</a></p>',
                    ),
                    status=410,
                )
            else:
                # GET continuations return only to server-chosen sharing pages.
                allowed = path.startswith(
                    ("/account/shares", "/account/shared", "/account/questions", "/invite/")
                )
                destination = path if allowed and not draft else "/account/shared"
                if path.endswith("/accept") and path.startswith(
                    ("/invite/", "/account/shared/invitations/")
                ):
                    destination = path.removesuffix("/accept")
                elif path.startswith("/account/shared/join/"):
                    destination = "/account/shared?tab=with-me"
                if saved.get("query") and destination == path:
                    destination += "?" + saved["query"]
                result = RedirectResponse(destination, status_code=303)
            delete_cookie(result, "sharing_draft", secure=secure)
            return result
        result = response(
            request,
            ui.reply_form(
                question(owner, saved["thread"]),
                "{{CSRF}}",
                saved["draft"],
                "Sign-in completed. Review and send your reply.",
            ),
        )
        delete_cookie(result, "sharing_draft", secure=secure)
        return result

    @app.post("/account/questions/{thread_id}/decline")
    async def decline_page(thread_id: str, request: Request):
        return await reply_form(request, thread_id, True)

    @app.post("/account/sharing/action")
    async def action_page(request: Request):
        data = await form(request)
        csrf(request, data)
        operation = data.get("operation")
        owner = actor(request, recent=operation == "state" and data.get("status") == "deleted")
        request_key = data.get("idempotency_key", "")
        required_ids = {
            "invite": ("share_id",),
            "revoke_invite": ("invite_id",),
            "retract_answer": ("share_id",),
            "leave": ("membership_id",),
            "revoke": ("membership_id",),
            "state": ("share_id",),
            "withdraw": ("membership_id", "thread_id"),
        }
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", data.get(field, ""))
            for field in required_ids.get(operation, ())
        ):
            fail(422, "invalid_fields", "Check the required sharing fields")
        if operation == "invite":
            result = service.invite(
                owner,
                data["share_id"],
                validated(
                    models.Invite,
                    {
                        "invited_email": data.get("invited_email"),
                        "level": data.get("level") or "consult",
                        "fill_gaps": data.get("fill_gaps") == "yes",
                    },
                ),
                request_key,
            )
            return invitation_confirmation(
                owner, {**result, "share_id": data["share_id"]}, data.get("invited_email", "They")
            )
        if operation in ("link_repo", "set_level", "review", "access_granted"):
            fields = {
                "action": operation,
                "share_id": data.get("share_id"),
                "membership_id": data.get("membership_id") or None,
                "review_id": data.get("review_id") or None,
                "decision": data.get("decision") or None,
                "answer": (data.get("answer") or "").strip() or None,
            }
            if operation == "link_repo":
                fields.update(repo=data.get("repo", "").strip(), branch=data.get("branch", ""))
            if operation == "set_level":
                fields.update(level=data.get("level"), fill_gaps=data.get("fill_gaps") == "yes")
            await asyncio.to_thread(
                service.collaborate, owner, validated(models.Collaboration, fields)
            )
            return RedirectResponse("/account/shares/" + str(data.get("share_id")), status_code=303)
        if operation == "revoke_invite":
            service.revoke_invite(owner, data["invite_id"], request_key)
        elif operation == "retract_answer":
            if not data.get("confirm"):
                fail(422, "confirmation_required", "Confirm removing this shared answer")
            service.retract_answer(
                owner,
                data["share_id"],
                validated(
                    models.RetractAnswer,
                    {
                        "item_id": data.get("item_id"),
                        "expected_publication_revision": integer(
                            data.get("expected_publication_revision", "-1")
                        ),
                    },
                ),
                request_key,
            )
        elif operation in ("leave", "revoke"):
            if not data.get("confirm"):
                fail(422, "confirmation_required", "Confirm ending access")
            service.end_member(
                owner,
                data["membership_id"],
                validated(
                    models.MemberState,
                    {"expected_revision": integer(data.get("expected_revision", "-1"))},
                ),
                request_key,
                leave=operation == "leave",
            )
        elif operation == "state":
            if data.get("status") == "deleted" and not data.get("confirm"):
                fail(422, "confirmation_required", "Confirm erasure")
            service.lifecycle(
                owner,
                data["share_id"],
                validated(
                    models.State,
                    {
                        "status": data.get("status"),
                        "expected_publication_revision": integer(
                            data.get("expected_publication_revision", "-1")
                        ),
                    },
                ),
                request_key,
            )
        elif operation == "withdraw":
            service.withdraw(
                owner,
                data["membership_id"],
                data["thread_id"],
                validated(
                    models.Withdraw,
                    {"expected_thread_version": integer(data.get("expected_thread_version", "-1"))},
                ),
                request_key,
            )
        else:
            fail(422, "unknown_action", "Unknown sharing action")
        return RedirectResponse(
            "/account/shared?tab=with-me"
            if operation in ("leave", "withdraw")
            else "/account/shares",
            status_code=303,
        )
