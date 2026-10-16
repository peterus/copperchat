import stat
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.errors import ConfigError
from copperchat.web.app import create_app
from copperchat.web.auth import COOKIE_NAME, LoginRateLimiter

GOOD = {"username": "admin", "password": "test-password"}


def test_login_sets_hardened_cookie(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings(cookie_secure=True))) as client:
        r = client.post("/api/auth/login", json=GOOD)
        assert r.status_code == 204
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie or "SameSite=Lax" in cookie
        assert "Secure" in cookie


def test_cookie_secure_can_be_disabled(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings(cookie_secure=False))) as client:
        r = client.post("/api/auth/login", json=GOOD)
        assert "Secure" not in r.headers["set-cookie"]


def test_me_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/auth/me").status_code == 401
        client.post("/api/auth/login", json=GOOD)
        r = client.get("/api/auth/me")
        assert r.status_code == 200 and r.json() == {"username": "admin"}
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_and_unknown_user_look_identical(
    make_settings: Callable[..., Settings],
) -> None:
    with TestClient(create_app(make_settings())) as client:
        a = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
        b = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        assert a.status_code == b.status_code == 401
        assert a.json() == b.json()


def test_rate_limit_after_five_failures(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        for _ in range(5):
            assert (
                client.post(
                    "/api/auth/login", json={"username": "admin", "password": "bad"}
                ).status_code
                == 401
            )
        r = client.post("/api/auth/login", json=GOOD)
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1


def test_rate_limiter_window_expires() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(max_failures=2, window_s=60, clock=lambda: now[0])
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is not None
    assert limiter.retry_after("5.6.7.8") is None
    now[0] = 61.0
    assert limiter.retry_after("1.2.3.4") is None


def test_admin_seeded_once_and_password_not_reseeded(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    with TestClient(create_app(settings)):
        pass
    changed = settings.model_copy(update={"admin_password": None})
    with TestClient(create_app(changed)) as client:
        assert client.post("/api/auth/login", json=GOOD).status_code == 204


def test_first_start_without_admin_password_fails(make_settings: Callable[..., Settings]) -> None:
    with (
        pytest.raises(ConfigError, match="ADMIN_PASSWORD"),
        TestClient(create_app(make_settings(admin_password=None))),
    ):
        pass


def test_session_secret_generated_0600_and_reused(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        cookie = client.cookies.get(COOKIE_NAME)
    secret_file = settings.data_dir / "session_secret"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    with TestClient(create_app(settings)) as client2:
        client2.cookies.set(COOKIE_NAME, cookie or "")
        assert client2.get("/api/auth/me").status_code == 200


def test_tampered_cookie_rejected(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.cookies.set(COOKIE_NAME, "eyJ1aWQiOjF9.forged.sig")
        assert client.get("/api/auth/me").status_code == 401


def test_rate_limiter_forgets_expired_clients() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(max_failures=2, window_s=60, clock=lambda: now[0])
    limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("5.6.7.8") is None
    now[0] = 61.0
    assert limiter.retry_after("1.2.3.4") is None
    assert limiter.tracked_clients() == 0
