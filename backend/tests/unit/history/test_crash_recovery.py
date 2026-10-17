import pytest

from copperchat.kicad_session import KicadUnavailable
from tests.unit.history.conftest import Opened
from tests.unit.history.test_history_hooks import done, turn


async def test_crash_mid_turn_offers_restore_and_next_turn_works(opened: Opened) -> None:
    env, hooks = opened.env, opened.hooks
    t = turn("run-9")
    await hooks.on_turn_start(t)
    await hooks.on_first_mutation(t)
    env.server.crash()
    with pytest.raises(KicadUnavailable):  # the tool call that hit the dead server
        await opened.session.call(lambda h: h.kicad.get_version())
    await hooks.on_turn_end(t, done("pcb", reason="error", failed=True))

    assert env.engine.errors == []  # the engine posts the only "KiCad stopped responding" row
    snap = (await env.svc.store(opened.project).list())[0]
    assert env.engine.notices[-1][1] == {"action": "restore", "snapshotId": snap.id}
    assert env.saved == []  # no autosave of a broken turn
    current = env.sessions.current()
    assert current is not None
    version = await current.call(lambda h: h.kicad.get_version().full_version)
    assert version == "11.0.0-fake"


async def test_crash_detected_only_at_turn_end_restarts_kicad(opened: Opened) -> None:
    env = opened.env
    t = turn("run-10")
    await opened.hooks.on_turn_start(t)
    env.server.crash()
    await opened.hooks.on_turn_end(t, done(reason="error", failed=True))
    assert env.engine.errors == []
    assert not any(m and m.get("action") == "restore" for _, m in env.engine.notices)
    assert env.server.starts == 2 and not env.server.dead


async def test_failed_restart_is_reported_once(opened: Opened) -> None:
    env = opened.env
    env.server.crash()
    env.server.fail_start_with_exit_code = 1
    await opened.hooks.on_turn_end(turn(), done(reason="error", failed=True))
    assert [m for _, m in env.engine.errors] == [{"code": "kicad_restart_failed"}]
