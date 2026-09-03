"""HTTP Basic authentication.

The application was built to sit behind something that already authenticates —
Caddy, nginx, a tailnet — and that is still the better arrangement. This exists
for the case where it is reachable before that is set up, so "no access control
at all" is never the default state of a running instance.

Basic auth sends the password on every request, reversibly encoded. Over plain
HTTP that is the same as sending it in the clear, so this belongs behind TLS or
on a private network. It is a lock on the door, not a wall.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# The container healthcheck runs without credentials, so this one path stays
# open — and answers with nothing but liveness while auth is on.
OPEN_PATHS = frozenset({"/healthz"})

_UNAUTHORIZED = JSONResponse(
    {"detail": "Authentication required"},
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="blocktail", charset="UTF-8"'},
)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username.encode()
        self._password = password.encode()

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return _UNAUTHORIZED

        try:
            user, _, password = base64.b64decode(encoded).partition(b":")
        except (binascii.Error, ValueError):
            return _UNAUTHORIZED

        # Both compared, and both in constant time: comparing only the password,
        # or short-circuiting on the username, leaks which half was wrong.
        ok_user = secrets.compare_digest(user, self._username)
        ok_password = secrets.compare_digest(password, self._password)
        if not (ok_user and ok_password):
            return _UNAUTHORIZED

        return await call_next(request)
