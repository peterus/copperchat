"""Turn end restarts the api-server once when a footprint document could not be closed
(FP-C2 `restart_pending`, F4a Task 1)."""

from __future__ import annotations

from typing import Any

import pytest
from kipy.errors import ApiError

from copperchat.kicad_session import KicadSession, KicadUnavailable
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from tests.support.fake_kicad import FakeKiCad
from tests.unit.history.conftest import Opened
from tests.unit.history.test_history_hooks import done, turn


@pytest.fixture
def failing_close(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: FakeKiCad, document: Any) -> None:
        raise ApiError("KiCad returned error: Requested document does not match")

    monkeypatch.setattr(FakeKiCad, "close_document", refuse)


async def test_restart_pending_restarts_at_turn_end(opened: Opened, failing_close: None) -> None:
    session = opened.session
    async with session.footprint_document(SEED_LIB_ID):
        pass
    assert session.restart_pending  # the close raised
    before = session.restart_count
    t = turn("run-rp1")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_turn_end(t, done())
    assert session.restart_count == before + 1
    assert not session.restart_pending
    # the next turn end does not restart again
    t2 = turn("run-rp2")
    await opened.hooks.on_turn_start(t2)
    await opened.hooks.on_turn_end(t2, done())
    assert session.restart_count == before + 1


async def test_no_restart_without_the_flag(opened: Opened) -> None:
    before = opened.session.restart_count
    t = turn("run-rp3")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_turn_end(t, done())
    assert opened.session.restart_count == before


async def test_restart_after_autosave(
    opened: Opened, failing_close: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edits of the turn are saved before the server is restarted from disk."""
    env, session = opened.env, opened.session
    order: list[list[str]] = []
    real_restart = KicadSession.restart

    async def restart(self: KicadSession) -> None:
        order.append(list(env.saved))
        await real_restart(self)

    monkeypatch.setattr(KicadSession, "restart", restart)
    t = turn("run-rp4")
    await opened.hooks.on_turn_start(t)
    async with session.footprint_document(SEED_LIB_ID):
        pass
    session.modified = True
    await opened.hooks.on_turn_end(t, done("sch"))
    assert order == [["sch"]]  # restarted once, after the autosave
    assert not session.restart_pending


async def test_failed_restart_clears_the_flag(
    opened: Opened, failing_close: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = opened.session
    async with session.footprint_document(SEED_LIB_ID):
        pass
    calls: list[int] = []

    async def broken(self: KicadSession) -> None:
        calls.append(1)
        raise KicadUnavailable("could not start")

    monkeypatch.setattr(KicadSession, "restart", broken)
    t = turn("run-rp5")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_turn_end(t, done())  # never raises
    assert calls == [1] and not session.restart_pending
