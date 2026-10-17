import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.projects import project_id_for
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}


def test_visibility_frames_reach_the_registry(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    (settings.projects_root / "demo").mkdir()
    (settings.projects_root / "demo" / "demo.kicad_pro").write_text("")
    pid = project_id_for("demo/demo.kicad_pro")
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/login", json=GOOD)
        registry: Any = c.app.state.push.visibility  # type: ignore[attr-defined]
        with c.websocket_connect("/api/ws") as sock:
            sock.send_text(json.dumps({"type": "visibility", "projectId": pid, "visible": True}))
            sock.send_text(json.dumps({"type": "ping", "projectId": None}))
            assert json.loads(sock.receive_text())["type"] == "pong"  # frames are in order
            assert registry.any_visible() is True
            sock.send_text(json.dumps({"type": "visibility", "projectId": pid, "visible": False}))
            sock.send_text(json.dumps({"type": "ping", "projectId": None}))
            assert json.loads(sock.receive_text())["type"] == "pong"
            assert registry.any_visible() is False
            sock.send_text(json.dumps({"type": "visibility", "projectId": pid, "visible": True}))
            sock.send_text(json.dumps({"type": "visibility", "projectId": pid, "visible": "yes"}))
            assert json.loads(sock.receive_text())["code"] == "invalid_frame"
            assert registry.any_visible() is True
        # the socket closed while visible: it no longer suppresses pushes
        assert registry.any_visible() is False


def test_parked_rate_limit_rearms_reopen_push_after_restart(
    make_settings: Callable[..., Settings],
) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    from copperchat.db import Database
    from copperchat.db.repos.agent_state import RateLimitRepo

    settings = make_settings(dev_fake_agent=True)
    (settings.projects_root / "demo").mkdir()
    (settings.projects_root / "demo" / "demo.kicad_pro").write_text("")
    pid = project_id_for("demo/demo.kicad_pro")
    resets_at = datetime.now(UTC) + timedelta(hours=1)

    async def park() -> None:  # state left behind by a previous process
        db = await Database.open(settings.data_dir / "copperchat.db")
        try:
            await RateLimitRepo(db).set(pid, resets_at)
        finally:
            await db.close()

    asyncio.run(park())
    with TestClient(create_app(settings)) as c:
        notifier: Any = c.app.state.push.notifier  # type: ignore[attr-defined]
        assert set(notifier._timers) == {pid}
        assert not notifier._timers[pid].done()
