"""Minimal browser session discovery for the public website's account navigation."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

WEBSITE_ORIGIN = "https://varyzai.com"
SESSION_PATH = "/api/browser-session"


def website_session_request(request: Request) -> bool:
    # This exception is read-only and exact: website scripts cannot call account actions.
    return (
        request.method == "GET"
        and request.url.path == SESSION_PATH
        and request.headers.get("origin") == WEBSITE_ORIGIN
    )


def is_admin(owner, settings) -> bool:
    return bool(
        owner
        and owner.get("email_verified")
        and str(owner.get("email") or "").lower()
        in {email.lower() for email in settings.admin_emails}
    )


def register_navigation(app, settings, account):
    @app.get(SESSION_PATH)
    def browser_session(request: Request):
        try:
            owner = account(request, browser=True)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            owner = None
        response = JSONResponse({
            "authenticated": owner is not None,
            "admin": is_admin(owner, settings),
        })
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Origin, Cookie"
        if website_session_request(request):
            response.headers["Access-Control-Allow-Origin"] = WEBSITE_ORIGIN
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response
