"""Which push endpoints the server may POST to.

A subscription's endpoint comes from the browser (via the signed-in user), and the server
POSTs to it later. Only the browsers' push services are allowed — Chrome/Android/Samsung
(FCM), Firefox (Mozilla autopush), Safari (Apple), Edge (WNS) — and their hosts must
still resolve to public addresses. The allowlist also closes the DNS-rebinding window
between this check and the connection: an attacker would need to control those domains.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from copperchat.research.net import url_refusal

PUSH_SERVICE_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",  # updates.push.services.mozilla.com
    "push.apple.com",  # web.push.apple.com
    "notify.windows.com",  # <region>.notify.windows.com
)


def _allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in PUSH_SERVICE_HOSTS)


async def push_endpoint_refusal(url: str) -> str | None:
    """None if the server may send pushes to ``url``, else the reason."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "malformed URL"
    if parts.scheme != "https":
        return "push endpoints must use https"
    if not _allowed_host(parts.hostname or ""):
        return f"{parts.hostname!r} is not a known browser push service"
    return await url_refusal(url)
