"""Security response headers for every HTTP response (pure ASGI, WebSockets untouched).

The Content-Security-Policy allows only same-origin scripts, styles, images and
connections: agent markdown renders only ``/api/projects/<id>/(images|renders)/`` images
(Phase 6A) and nothing loads from third parties. ``style-src 'unsafe-inline'`` is needed for
React ``style`` attributes and KiCanvas' shadow-DOM styles. PDF responses (datasheets,
uploaded PDFs) get no CSP: Chrome's built-in PDF viewer does not render under one (Phase 8).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

_COMMON = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                for key, value in _COMMON:
                    if key not in present:
                        headers.append((key, value))
                if not ctype.startswith(b"application/pdf"):
                    if b"content-security-policy" not in present:
                        headers.append(
                            (b"content-security-policy", CONTENT_SECURITY_POLICY.encode())
                        )
                    if b"x-frame-options" not in present:
                        headers.append((b"x-frame-options", b"DENY"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
