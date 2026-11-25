"""Raw-protobuf board API wrappers (G3, G5) on a real api-server (SP4 P1a, Task 4)."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kipy.board_types import Group, Track, Zone
from kipy.client import KiCadClient
from kipy.errors import ApiError
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common import ApiStatusCode, tasks_pb2
from kipy.proto.common.commands.editor_commands_pb2 import GetOpenDocuments

from copperchat.board.model import board_hash
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board.api import (
    DrcTimeout,
    RefillTimeout,
    board_project,
    create_board_document,
    drc_call_timeout,
    export_specctra_dsn,
    import_specctra_session,
    refill,
    refill_call_timeout,
    run_drc_task,
    save_document_to_string,
    set_group_name,
)
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.projects import Project
from tests.support.kicad_env import KICAD_CLI

pytestmark = pytest.mark.kicad


class SendSpy:
    """Every protobuf command sent by any kipy client: (name, token, reply state)."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, int | None]] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[SendSpy]:
    s = SendSpy()
    real = KiCadClient.send

    def send(self: KiCadClient, command: Any, response_type: Any) -> Any:
        token = getattr(getattr(command, "token", None), "value", "")
        try:
            reply = real(self, command, response_type)
        except Exception:
            s.log.append((type(command).__name__, token, None))
            raise
        if isinstance(reply, tasks_pb2.StartTaskResponse):
            token = reply.token.value
        state = reply.state if isinstance(reply, tasks_pb2.TaskStatusResponse) else None
        if isinstance(command, GetOpenDocuments):
            return reply  # `h.board()` looks the document up on every call
        s.log.append((type(command).__name__, token, state))
        return reply

    monkeypatch.setattr(KiCadClient, "send", send)
    yield s


async def _drc(s: KicadSession, timeout_s: float) -> Any:
    return await s.call(
        lambda h: run_drc_task(h, timeout_s=timeout_s), timeout=drc_call_timeout(timeout_s)
    )


def _starts(spy: SendSpy) -> list[int]:
    return [i for i, (name, _, _) in enumerate(spy.log) if name == "StartDrcTask"]


async def test_drc_task_on_ecc83(ecc83_session: KicadSession, tmp_path: Path) -> None:
    s = ecc83_session
    result = await _drc(s, 60)
    assert result.seconds < 5
    assert result.markers and all(m.kiids for m in result.markers)

    copy = await s.save_copy("pcb", tmp_path / "copy" / "ecc83-pp.kicad_pcb")
    out = tmp_path / "drc.json"
    subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out),
         str(copy)],
        check=False, capture_output=True, timeout=300,
    )  # fmt: skip
    report = json.loads(out.read_text(encoding="utf-8"))
    assert result.unconnected == len(report.get("unconnected_items", []))
    print("ecc83 DRC:", len(result.markers), "markers,", result.unconnected, "unconnected")


async def test_drc_timeout_distinct(ecc83_session: KicadSession, spy: SendSpy) -> None:
    s = ecc83_session
    with pytest.raises(DrcTimeout) as info:
        await _drc(s, 0.001)
    assert info.value.code == "drc_timeout"
    assert s.pending_drc is not None
    gen, token = s.pending_drc
    assert gen == s.generation

    t0 = time.monotonic()
    result = await _drc(s, 60)  # a separate session.call: a fresh handle
    assert time.monotonic() - t0 <= 31
    assert result.seconds < 60
    assert s.pending_drc is None
    starts = _starts(spy)
    assert len(starts) == 2
    assert spy.log[starts[0]][1] == token
    # Before the second task started, the first one was seen leaving TS_RUNNING.
    between = spy.log[starts[0] + 1 : starts[1]]
    assert any(
        name == "GetTaskStatus" and tok == token and state != tasks_pb2.TS_RUNNING
        for name, tok, state in between
    )


async def test_pending_drc_cleared_on_restart(ecc83_session: KicadSession, spy: SendSpy) -> None:
    s = ecc83_session
    with pytest.raises(DrcTimeout):
        await _drc(s, 0.001)
    assert s.pending_drc is not None
    _, old = s.pending_drc
    await s.restart()
    assert s.pending_drc is None
    spy.log.clear()
    await _drc(s, 60)
    assert spy.log[0][0] == "StartDrcTask"  # at once, no poll of the old token
    assert all(tok != old for _, tok, _ in spy.log)


def _gnd_zones(h: KicadHandle, project: Project) -> None:
    """GND pours over the whole outline on F.Cu and B.Cu: a zone-heavy fill."""
    board = h.board()
    m = load_board_model(h, project)
    o = m.outline
    (gnd,) = [n for n in board.get_nets() if n.name == "GND"]
    zones = []
    for layer in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu):
        z = Zone()
        z.layers = [layer]
        z.net = gnd
        line = PolyLine()
        for x, y in ((o.x, o.y), (o.x + o.w, o.y), (o.x + o.w, o.y + o.h), (o.x, o.y + o.h)):
            line.append(PolyLineNode.from_xy(round(x * 1e6), round(y * 1e6)))
        line.closed = True
        poly = PolygonWithHoles()
        poly.outline = line
        z.outline = poly
        zones.append(z)
    board.create_items(zones)


async def test_refill_no_busy_leak(ecc83_session: KicadSession, spy: SendSpy) -> None:
    s = ecc83_session
    async with s.commit("zones", doc="pcb"):
        await s.call(lambda h: _gnd_zones(h, s.project))
    spy.log.clear()
    try:
        await s.call(lambda h: refill(h, timeout_s=0.01), timeout=refill_call_timeout(0.01))
        outcome = "returned"
    except RefillTimeout:
        outcome = "refill_timeout"
    print("refill(0.01) on a zone-heavy ecc83:", outcome, "busy slot:", s.refill_busy)
    names = [name for name, _, _ in spy.log]
    assert names[0] == "RefillZones"
    assert set(names[1:]) <= {"Ping"}
    try:
        zones = await s.call(lambda h: len(h.board().get_zones()))
    except ApiError as exc:  # pragma: no cover - the failure this test guards against
        assert exc.code != ApiStatusCode.AS_BUSY, exc
        raise
    assert zones >= 2


async def test_save_document_to_string(ecc83_session: KicadSession) -> None:
    text = await ecc83_session.call(save_document_to_string)
    assert text.startswith("(kicad_pcb")


async def test_specctra_export_live(ecc83_session: KicadSession, tmp_path: Path) -> None:
    dest = tmp_path / "board.dsn"
    await ecc83_session.call(lambda h: export_specctra_dsn(h, dest), timeout=120)
    assert dest.read_text(encoding="utf-8", errors="replace").lstrip().startswith("(pcb")


async def test_import_specctra_session_missing_file(
    ecc83_session: KicadSession, tmp_path: Path
) -> None:
    s = ecc83_session
    before = board_hash(await s.call(lambda h: load_board_model(h, s.project)))
    with pytest.raises(ApiError) as info:
        await s.call(lambda h: import_specctra_session(h, tmp_path / "missing.ses"))
    print("ImportSpecctraSession on a missing file:", info.value)
    assert str(info.value)
    assert board_hash(await s.call(lambda h: load_board_model(h, s.project))) == before


async def test_board_project_path_fix(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    classes = await s.call(lambda h: [c.name for c in board_project(h).get_net_classes()])
    assert "Default" in classes
    with pytest.raises(ApiError, match="not open at path"):
        await s.call(lambda h: h.board().get_project().get_net_classes())


async def test_group_name(ecc83_session: KicadSession) -> None:
    s = ecc83_session

    def make(h: KicadHandle) -> str:
        board = h.board()
        fp = sorted(board.get_footprints(), key=lambda f: f.reference_field.text.value)[0]
        g = Group()
        g.item_ids = [fp.id]
        set_group_name(g.proto, "blk:power")
        board.create_items([g])
        return str(fp.reference_field.text.value)

    async with s.commit("group", doc="pcb"):
        ref = await s.call(make)
    names = await s.call(lambda h: [g.name for g in h.board().get_groups()])
    assert "blk:power" in names
    m = await s.call(lambda h: load_board_model(h, s.project))
    assert ("blk:power", (ref,)) in m.groups
    assert m.part(ref).group == "blk:power"


async def test_create_board_document(blank_nopcb_session: KicadSession) -> None:
    s = blank_nopcb_session
    path = s.project.dir / f"{s.project.name}.kicad_pcb"
    assert not path.exists()
    await s.call(lambda h: create_board_document(h, path), timeout=60)
    assert path.read_text(encoding="utf-8").startswith("(kicad_pcb")


class _Drop(Exception):
    pass


async def test_probe_drc_sees_open_commit(ecc83_session: KicadSession, record_property) -> None:  # type: ignore[no-untyped-def]
    """Spec §6.2 probe: does the DRC task see items staged in an open (unpushed) commit?
    Recorded only - no code may depend on the answer."""
    s = ecc83_session

    def short(h: KicadHandle) -> str:
        board = h.board()
        pads = [p for p in board.get_pads() if p.net.name]
        a = pads[0]
        b = next(p for p in pads if p.net.name != a.net.name)
        t = Track()
        t.layer = BoardLayer.BL_F_Cu
        t.net = a.net
        t.start = Vector2.from_xy(a.position.x, a.position.y)
        t.end = Vector2.from_xy(b.position.x, b.position.y)
        t.width = 250_000
        (created,) = board.create_items([t])
        return str(created.id.value)

    seen: bool | None = None
    try:
        async with s.commit("probe", doc="pcb"):
            kiid = await s.call(short)
            result = await _drc(s, 60)
            seen = any(kiid in m.kiids for m in result.markers)
            raise _Drop
    except _Drop:
        pass
    assert seen is not None
    record_property("drc_sees_open_commit", seen)
    print(f"drc_sees_open_commit = {seen}")

    # Sanity check of the probe itself: the same short after the push.
    async with s.commit("probe pushed", doc="pcb"):
        kiid = await s.call(short)
    pushed = await _drc(s, 60)
    seen_pushed = any(kiid in m.kiids for m in pushed.markers)
    record_property("drc_sees_pushed_commit", seen_pushed)
    print(f"drc_sees_pushed_commit = {seen_pushed}")
