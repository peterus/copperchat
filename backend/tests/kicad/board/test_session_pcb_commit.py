"""Board commits through `KicadSession.commit(doc="pcb")` (SP4 P1a, Task 3)."""

from __future__ import annotations

import pytest
from kipy.geometry import Vector2

from copperchat.kicad_session import KicadSession
from copperchat.kicad_session.session import KicadHandle

pytestmark = pytest.mark.kicad

_MM = 1_000_000


def _first_footprint(h: KicadHandle) -> tuple[str, int, int]:
    fp = min(h.board().get_footprints(), key=lambda f: f.reference_field.text.value)
    return fp.reference_field.text.value, fp.position.x, fp.position.y


def _position_of(h: KicadHandle, ref: str) -> tuple[int, int]:
    (fp,) = [f for f in h.board().get_footprints() if f.reference_field.text.value == ref]
    return fp.position.x, fp.position.y


async def test_pcb_commit_save_persists_move(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    ref, x, y = await s.call(_first_footprint)

    def _move(h: KicadHandle) -> None:
        board = h.board()
        (fp,) = [f for f in board.get_footprints() if f.reference_field.text.value == ref]
        fp.position = Vector2.from_xy(x + _MM, y)
        board.update_items([fp])

    async with s.commit("t", doc="pcb"):
        await s.call(_move)
    assert s.modified_docs == {"pcb"}
    assert s.modified is False and s.jobs_stale is False
    assert await s.call(lambda h: _position_of(h, ref)) == (x + _MM, y)

    await s.save("pcb")
    assert s.modified_docs == set()

    await s.restart()  # a fresh api-server loads the saved files
    assert await s.call(lambda h: _position_of(h, ref)) == (x + _MM, y)
