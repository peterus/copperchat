from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app
from copperchat.web.security import CONTENT_SECURITY_POLICY


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>CopperChat</title>")
    (d / "sw.js").write_text("self.addEventListener('push', () => {});")
    (d / "manifest.webmanifest").write_text("{}")
    (d / "assets" / "app-abc123.js").write_text("console.log(1)")
    return d


def test_spa_fallback_and_cache_headers(make_settings: Callable[..., Settings], dist: Path) -> None:
    with TestClient(create_app(make_settings(frontend_dist=dist))) as c:
        for path in ("/", "/login", "/p/abc123def456", "/settings", "/projects"):
            r = c.get(path)
            assert r.status_code == 200 and "CopperChat" in r.text, path
            assert r.headers["cache-control"] == "no-cache", path
        sw = c.get("/sw.js")
        assert "push" in sw.text and sw.headers["cache-control"] == "no-cache"
        assert c.get("/manifest.webmanifest").headers["cache-control"] == "no-cache"
        asset = c.get("/assets/app-abc123.js")
        assert "immutable" in asset.headers["cache-control"]
        stale = c.get("/assets/app-old999.js")
        assert stale.status_code == 404 and "text/html" not in stale.headers["content-type"]
        nope = c.get("/api/nope")
        assert nope.status_code == 404 and nope.json() == {"detail": "Not Found"}
        assert c.get("/api/health").status_code == 200


def test_security_headers(make_settings: Callable[..., Settings], dist: Path) -> None:
    with TestClient(create_app(make_settings(frontend_dist=dist))) as c:
        for path in ("/", "/api/health", "/api/nope"):
            h = c.get(path).headers
            assert h["content-security-policy"] == CONTENT_SECURITY_POLICY, path
            assert h["x-content-type-options"] == "nosniff"
            assert h["x-frame-options"] == "DENY"
            assert h["referrer-policy"] == "same-origin"
    assert "script-src 'self';" in CONTENT_SECURITY_POLICY  # no inline scripts
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY


async def test_pdf_responses_get_no_csp() -> None:
    from copperchat.web.security import SecurityHeadersMiddleware

    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: Callable[..., object]) -> None:
        await send(  # type: ignore[misc]
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/pdf")],
            }
        )

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {}

    await SecurityHeadersMiddleware(app)({"type": "http"}, receive, send)  # type: ignore[arg-type]
    names = {k for k, _ in sent[0]["headers"]}  # type: ignore[attr-defined]
    assert b"content-security-policy" not in names and b"x-frame-options" not in names
    assert b"x-content-type-options" in names
