from collections.abc import Callable

from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}
BODY = {"endpoint": "https://push.example/x", "keys": {"p256dh": "p", "auth": "a"}}


async def _public(url: str) -> str | None:
    return None if "push.example" in url else "private host"


def test_push_routes(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as c:
        c.app.state.push.check_endpoint = _public  # type: ignore[attr-defined]
        assert c.get("/api/push/vapid-public-key").status_code == 401
        assert c.post("/api/push/subscribe", json=BODY).status_code == 401
        c.post("/api/auth/login", json=GOOD)
        key = c.get("/api/push/vapid-public-key").json()["publicKey"]
        assert isinstance(key, str) and len(key) == 87
        assert c.post("/api/push/subscribe", json=BODY).status_code == 204
        assert c.post("/api/push/subscribe", json=BODY).status_code == 204  # idempotent
        store = c.app.state.push.store  # type: ignore[attr-defined]
        assert [s.endpoint for s in c.portal.call(store.all)] == [BODY["endpoint"]]  # type: ignore[union-attr]
        r = c.request("DELETE", "/api/push/subscribe", json={"endpoint": BODY["endpoint"]})
        assert r.status_code == 204
        assert c.portal.call(store.all) == []  # type: ignore[union-attr]
        insecure = {"endpoint": "http://push.example/x", "keys": BODY["keys"]}
        assert c.post("/api/push/subscribe", json=insecure).status_code == 422
        private = {"endpoint": "https://192.168.1.10/x", "keys": BODY["keys"]}
        assert c.post("/api/push/subscribe", json=private).status_code == 422


def test_real_endpoint_check_refuses_private_hosts(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as c:
        c.post("/api/auth/login", json=GOOD)
        for host in ("127.0.0.1", "10.0.0.5", "[::1]", "push.example"):
            body = {"endpoint": f"https://{host}/push", "keys": BODY["keys"]}
            assert c.post("/api/push/subscribe", json=body).status_code == 422, host


def test_vapid_key_persists_across_restarts(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    keys = []
    for _ in range(2):
        with TestClient(create_app(settings)) as c:
            c.post("/api/auth/login", json=GOOD)
            keys.append(c.get("/api/push/vapid-public-key").json()["publicKey"])
    assert keys[0] == keys[1]
    assert (settings.data_dir / "vapid.json").is_file()
