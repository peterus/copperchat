import asyncio
from collections.abc import Callable

import httpx

from copperchat.config import Settings
from copperchat.web.app import create_app
from copperchat.web.auth import COOKIE_NAME

GOOD = {"username": "admin", "password": "test-password"}
BAD = {"username": "admin", "password": "wrong"}


async def test_concurrent_failed_logins_cannot_exceed_the_limit(
    make_settings: Callable[..., Settings],
) -> None:
    # Attempts are counted before the (slow) hash check, so a burst of parallel
    # guesses cannot all pass the limiter before the first failure is recorded.
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            responses = await asyncio.gather(
                *(client.post("/api/auth/login", json=BAD) for _ in range(20))
            )
    codes = [r.status_code for r in responses]
    assert codes.count(401) <= 5
    assert codes.count(429) >= 15


async def test_logout_invalidates_the_session_server_side(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.post("/api/auth/login", json=GOOD)).status_code == 204
            stolen = client.cookies.get(COOKIE_NAME)
            assert stolen
            assert (await client.post("/api/auth/logout")).status_code == 204
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as other:
            other.cookies.set(COOKIE_NAME, stolen)
            assert (await other.get("/api/auth/me")).status_code == 401


def test_forwarded_allow_ips_is_configurable(make_settings: Callable[..., Settings]) -> None:
    assert make_settings().forwarded_allow_ips == "127.0.0.1"
    assert make_settings(forwarded_allow_ips="172.16.0.0/12").forwarded_allow_ips == (
        "172.16.0.0/12"
    )
