"""Host-only production cookies; no fallback to sibling-domain cookies."""


def cookie_name(name, secure):
    return "__Host-" + name if secure else name


def read_cookie(request, name, default=""):
    secure = request.app.state.settings.public_url.startswith("https://")
    return request.cookies.get(cookie_name(name, secure), default)


def set_cookie(response, name, value, *, secure, **options):
    response.set_cookie(cookie_name(name, secure), value, secure=secure, path="/", **options)


def delete_cookie(response, name, *, secure):
    response.delete_cookie(
        cookie_name(name, secure), secure=secure, path="/", httponly=True, samesite="lax"
    )
