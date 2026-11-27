"""`pcb_route` planning and the per-connection loop (SP4 P5b Tasks 1-2), pure: builders for
the board, a fake `BoardStageContext` (commits, DRC, board reads) and a fake `run_cpu`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import math
import time
import types
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import DiffPair, classify
from copperchat.board.model import BoardModel
from copperchat.board.router import RoutePath, RouteTooLarge
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import route
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult, DrcTimeout

from .builders import net_class, track
from .builders_circuit import circuit, conn, ic, pad, res

# -- planning ----------------------------------------------------------------------------


def _two_res(*extra: Any, **kw: Any) -> BoardModel:
    # R1.1 (9.5, 10) and R2.1 (19.5, 10) on N1; their pads 2 on N2 / N3
    r1, r2 = res("R1", "N1", "N2", (10.0, 10.0)), res("R2", "N1", "N3", (20.0, 10.0))
    return circuit(r1, r2, *extra, **kw)


def _plan(m: BoardModel, nets: Sequence[str], **kw: Any) -> list[route.ConnPlan]:
    opts: dict[str, Any] = {"layers": None, "allow_vias": True} | kw
    return route.plan_connections(m, classify(m), nets, **opts)


def test_plans_unrouted_connection() -> None:
    (p,) = _plan(_two_res(), ["N1"])
    assert (p.net, p.a.ref, p.b.ref, p.pair) == ("N1", "R1", "R2", None)
    assert p.req.width == 0.2 and p.req.clearance == 0.2
    assert p.req.via == (0.6, 0.3)
    assert p.req.layers == ("F.Cu", "B.Cu") and p.req.allow_vias


def test_skips_routed() -> None:
    m = _two_res(tracks=(track("N1", "F.Cu", (9.5, 10.0), (19.5, 10.0)),))
    assert _plan(m, ["N1"]) == []


def test_class_widths() -> None:
    power = dataclasses.replace(net_class("Power", 0.4, 0.25), via_diameter=0.8, via_drill=0.4)
    m = _two_res(
        net_classes=(net_class(), power),
        net_class_of=(("N1", "Default,Power"),),  # KiCad's composite effective class (P2)
    )
    (p,) = _plan(m, ["N1"])
    assert (p.req.width, p.req.clearance, p.req.via) == (0.4, 0.25, (0.8, 0.4))


def test_class_never_below_board_minimum() -> None:
    m = _two_res(rules=(("min_clearance", 0.3), ("min_track_width", 0.25)))
    (p,) = _plan(m, ["N1"])
    assert (p.req.width, p.req.clearance) == (0.25, 0.3)


FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_power_outer_first() -> None:
    m = _two_res(copper_layers=FOUR)
    assert route.layer_order(m, "supply") == ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
    assert route.layer_order(m, "switch_node") == ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
    assert route.layer_order(m, "signal") == FOUR


def test_default_layers_skip_planes() -> None:
    """In1.Cu stays the solid GND plane of the default 4-layer stack (owner D1)."""
    m = circuit(
        res("R1", "+3V3", "N2", (10.0, 10.0)),
        res("R2", "+3V3", "N3", (20.0, 10.0)),
        copper_layers=FOUR,
    )
    (p,) = _plan(m, ["+3V3"])
    assert p.req.layers == ("F.Cu", "B.Cu", "In2.Cu")
    (q,) = _plan(m, ["+3V3"], layers=("In1.Cu", "F.Cu"))
    assert q.req.layers == ("F.Cu", "In1.Cu")
    six = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
    assert route.default_layers(dataclasses.replace(m, copper_layers=six)) == (
        "F.Cu",
        "In2.Cu",
        "In3.Cu",
        "B.Cu",
    )
    assert route.default_layers(dataclasses.replace(m, copper_layers=("F.Cu", "B.Cu"))) == (
        "F.Cu",
        "B.Cu",
    )


def _pair_board() -> BoardModel:
    j1 = conn("J1", {"1": "DP", "2": "DN", "3": "GND"}, (10.0, 10.0), pitch=0.5)
    u1 = ic("U1", {"1": "DP", "2": "DN", "3": "GND"}, (20.0, 10.0), pitch=0.5)
    return circuit(j1, u1)


def _pair_cls(m: BoardModel) -> Any:
    return dataclasses.replace(classify(m), pairs=(DiffPair("DP", "DN", "J1"),))


def test_pair_planned_once() -> None:
    m = _pair_board()
    cls = _pair_cls(m)
    plans = route.plan_connections(m, cls, ["DN", "DP"], layers=None, allow_vias=True)
    assert [(p.net, p.pair) for p in plans] == [("DP", "DN"), ("DN", "DP")]
    p, n = plans
    assert (p.a.ref, n.a.ref) == ("J1", "J1")  # ends matched: a next to a
    alone = route.plan_connections(m, cls, ["DP"], layers=None, allow_vias=True)
    assert [(p.net, p.pair) for p in alone] == [("DP", None)]


def test_shortest_first() -> None:
    # N1: R1.1 (9.5, 10), R2.1 (11.5, 10), R3.1 (29.5, 10): the 2 mm connection first
    m = circuit(
        res("R1", "N1", "A", (10.0, 10.0)),
        res("R3", "N1", "B", (30.0, 10.0)),
        res("R2", "N1", "C", (12.0, 10.0)),
    )
    plans = _plan(m, ["N1"])
    lengths = [abs(p.a.x - p.b.x) + abs(p.a.y - p.b.y) for p in plans]
    assert lengths == sorted(lengths) and lengths[0] == pytest.approx(2.0)


def test_conn_key_natural_order() -> None:
    a = pad("U10", "2", "N", (0, 0))
    b = pad("U9", "11", "N", (1, 0))
    assert route.conn_key("N", a, b) == route.conn_key("N", b, a) == "N:U9.11-U10.2"


def test_footprint_keepout_is_an_obstacle() -> None:
    area = ((30.0, 5.0), (40.0, 5.0), (40.0, 15.0), (30.0, 15.0))
    m = _two_res(res("A1", "X", "Y", (60, 60), rule_areas=(area,)))
    (p,) = _plan(m, ["N1"])
    zones = route.with_footprint_keepouts(m, p.req).zones
    (z,) = zones
    assert z.rule_area and z.keepout == {"tracks", "vias"} and z.outline == area
    inside = dataclasses.replace(p.req, b=dataclasses.replace(p.req.b, x=35.0, y=10.0))
    assert route.with_footprint_keepouts(m, inside).zones == ()


def test_router_avoids_footprint_keepout() -> None:
    """The real router goes around a footprint keepout lying across the straight path."""
    from copperchat.board.router import solve

    area = ((13.0, 7.0), (16.0, 7.0), (16.0, 13.0), (13.0, 13.0))
    m = _two_res(res("A1", "X", "Y", (60, 60), rule_areas=(area,)))
    (p,) = _plan(m, ["N1"])
    path = solve(route.with_footprint_keepouts(m, p.req), p.req)
    assert path is not None
    for _, a, b, _w in path.segments:
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            assert not (13.0 - 0.1 < x < 16.0 + 0.1 and 7.0 - 0.1 < y < 13.0 + 0.1), (x, y)


# -- the loop with fakes -------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@dataclasses.dataclass
class FakeSession:
    commits: list[str] = dataclasses.field(default_factory=list)

    @contextlib.asynccontextmanager
    async def commit(self, label: str, *, doc: str = "sch") -> AsyncIterator[None]:
        assert doc == "pcb"
        self.commits.append(label)
        yield


class FakeSctx:
    """`BoardStageContext` stand-in: `call` runs the function with no handle (the KiCad
    writes are patched), `drc` answers from `drc_fn`, `model` returns `model`."""

    def __init__(self, model: BoardModel, drc_fn: Callable[[float], Any] | None = None) -> None:
        self.ctx = types.SimpleNamespace(session=FakeSession())
        self.report: dict[str, Any] = {}
        self.created: set[str] = set()
        self._model = model
        self.drc_fn = drc_fn
        self.drc_timeouts: list[float] = []

    async def call(self, fn: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        return fn(None)

    async def drc(self, *, timeout_s: float = 60.0) -> DrcResult:
        self.drc_timeouts.append(timeout_s)
        if self.drc_fn is None:
            return DrcResult((), 0, 0.01)
        out = self.drc_fn(timeout_s)
        if asyncio.iscoroutine(out):
            out = await out
        assert isinstance(out, DrcResult)
        return out

    async def model(self) -> BoardModel:
        return self._model


class Board:
    """The fake board's items: `_create` / `_remove` patched onto it."""

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self.n = 0

    def create(self, h: Any, items: Sequence[tuple[Any, RoutePath]], lock: bool) -> list[str]:
        out = []
        for req, path in items:
            for _ in range(len(path.segments) + len(path.vias)):
                self.n += 1
                k = f"k{self.n}"
                self.items[k] = (req.net, lock)
                out.append(k)
        return out

    def remove(self, h: Any, ids: Sequence[str]) -> None:
        for k in ids:
            del self.items[k]


def _straight(req: Any) -> RoutePath:
    a, b = (req.a.x, req.a.y), (req.b.x, req.b.y)
    return RoutePath(((req.layers[0], a, b, req.width),), (), abs(b[0] - a[0]) + abs(b[1] - a[1]))


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> Board:
    b = Board()
    monkeypatch.setattr(route, "_create", b.create)
    monkeypatch.setattr(route, "_remove", b.remove)
    return b


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(route, "time", types.SimpleNamespace(monotonic=c.monotonic))
    return c


def _three(pitch: float = 10.0) -> BoardModel:
    """Three nets of one connection each (N1, N2, N3), `pitch` mm apart."""
    parts = []
    for i, net in enumerate(("N1", "N2", "N3")):
        y = 10.0 + pitch * i
        parts += [
            res(f"R{2 * i + 1}", net, f"A{i}", (10.0, y)),
            res(f"R{2 * i + 2}", net, f"B{i}", (20.0, y)),
        ]
    return circuit(*parts)


def _plans3(pitch: float = 10.0) -> list[route.ConnPlan]:
    return _plan(_three(pitch), ["N1", "N2", "N3"])


class FakeCpu:
    """`run_cpu` stand-in: records each call; `answer(fn, args, kw)` decides."""

    def __init__(self, answer: Callable[..., Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer = answer

    async def __call__(
        self, fn: Any, *args: Any, timeout_s: float, wait_s: Any = None, **kw: Any
    ) -> Any:
        self.calls.append({"fn": fn, "args": args, "timeout_s": timeout_s, "wait_s": wait_s} | kw)
        if self.answer is not None:
            out = self.answer(fn, args, kw, timeout_s)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        return _straight(args[1])


async def test_routes_all_without_deadline(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """test_route_deadline_none_routes_all"""
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    sctx = FakeSctx(_three())
    out = await route.route_connections(sctx, _plans3(), lock=True)  # type: ignore[arg-type]
    assert len(out.routed) == 3 and out.failed == ()
    assert all(c["timeout_s"] == 30 and c["wait_s"] is None for c in cpu.calls)
    assert sctx.drc_timeouts == [60.0] * 3
    assert set(board.items) == sctx.created and all(lock for _, lock in board.items.values())
    assert [c["margin"] for c in cpu.calls] == [0.0] * 3


async def test_route_connections_deadline(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drc(t: float) -> DrcResult:
        clock.now += 20  # the first connection's check takes the clock past the deadline
        return DrcResult((), 0, 0.01)

    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    sctx = FakeSctx(_three(), drc)
    out = await route.route_connections(  # type: ignore[arg-type]
        sctx, _plans3(), lock=True, deadline=clock.now + 10
    )
    assert out.routed == ("N1:R1.1-R2.1",)
    assert [r for _, r in out.failed] == ["timeout", "timeout"]
    assert len(cpu.calls) == 1  # never solved


async def test_too_large_reported(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(fn: Any, args: Any, kw: Any, t: float) -> Any:
        raise RouteTooLarge(5_000_000)

    cpu = FakeCpu(answer)
    monkeypatch.setattr(route, "run_cpu", cpu)
    plans = _plans3()[:1]
    out = await route.route_connections(FakeSctx(_three()), plans, lock=True)  # type: ignore[arg-type]
    assert out.failed == (("N1:R1.1-R2.1", "too_large"),)
    assert len(cpu.calls) == 1


async def test_compute_timeout_reported(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(fn: Any, args: Any, kw: Any, t: float) -> Any:
        raise StructuredToolError("x", "y", "z", code="board_compute_timeout")

    monkeypatch.setattr(route, "run_cpu", FakeCpu(answer))
    out = await route.route_connections(FakeSctx(_three()), _plans3()[:1], lock=True)  # type: ignore[arg-type]
    assert out.failed == (("N1:R1.1-R2.1", "timeout"),)


async def test_no_path_after_every_margin(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    cpu = FakeCpu(lambda fn, args, kw, t: None)
    monkeypatch.setattr(route, "run_cpu", cpu)
    out = await route.route_connections(FakeSctx(_three()), _plans3()[:1], lock=True)  # type: ignore[arg-type]
    assert out.failed == (("N1:R1.1-R2.1", "no_path"),)
    assert [c["margin"] for c in cpu.calls] == [0.0, 0.05, 0.10]


async def test_solve_gets_pruned_model(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    far = res("R99", "N1", "Z", (90.0, 70.0))
    m = dataclasses.replace(_three(), parts=(*_three().parts, far))
    plans = [p for p in _plan(m, ["N2"])]
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    (call,) = cpu.calls
    assert call["fn"] is route.solve
    refs = {p.ref for p in call["args"][0].parts}
    assert "R99" not in refs and {"R3", "R4"} <= refs


async def test_deadline_checked_per_margin(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(fn: Any, args: Any, kw: Any, t: float) -> None:
        clock.now += 20  # margin 0.0 finds nothing and uses up the time
        return None

    cpu = FakeCpu(answer)
    monkeypatch.setattr(route, "run_cpu", cpu)
    out = await route.route_connections(  # type: ignore[arg-type]
        FakeSctx(_three()), _plans3()[:1], lock=True, deadline=clock.now + 10
    )
    assert out.failed == (("N1:R1.1-R2.1", "timeout"),)
    assert [c["margin"] for c in cpu.calls] == [0.0]


async def test_steps_bounded_by_remaining(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = clock.now + 10.0
    seen: list[tuple[float, float, Any]] = []

    def answer(fn: Any, args: Any, kw: Any, t: float) -> RoutePath:
        clock.now += 1.5
        return _straight(args[1])

    async def call(fn: Any, *args: Any, timeout_s: float, wait_s: Any = None, **kw: Any) -> Any:
        seen.append((deadline - clock.now, timeout_s, wait_s))
        return answer(fn, args, kw, timeout_s)

    monkeypatch.setattr(route, "run_cpu", call)
    drcs: list[tuple[float, float]] = []

    def drc(t: float) -> DrcResult:
        drcs.append((deadline - clock.now, t))
        clock.now += 1.0
        return DrcResult((), 0, 0.01)

    out = await route.route_connections(  # type: ignore[arg-type]
        FakeSctx(_three(), drc), _plans3(), lock=True, deadline=deadline
    )
    assert len(out.routed) == 3
    for remaining, timeout_s, wait_s in seen:
        assert timeout_s <= min(30, remaining) + 1e-9 and wait_s == pytest.approx(remaining)
    for remaining, t in drcs:
        assert t <= min(60, remaining) + 1e-9


async def test_overrun_bounded(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real clock: a 1 s deadline with every step sleeping its whole budget ends within
    1.5 s (worst case = deadline + one DRC, here bounded by the remaining time)."""

    async def sleepy(fn: Any, *args: Any, timeout_s: float, wait_s: Any = None, **kw: Any) -> Any:
        await asyncio.sleep(timeout_s)
        return None

    async def slow_drc(t: float) -> DrcResult:
        await asyncio.sleep(t)
        return DrcResult((), 0, t)

    monkeypatch.setattr(route, "run_cpu", sleepy)
    start = time.monotonic()
    out = await route.route_connections(  # type: ignore[arg-type]
        FakeSctx(_three(), slow_drc), _plans3(), lock=True, deadline=start + 1.0
    )
    assert time.monotonic() - start < 1.5
    assert out.routed == () and len(out.failed) == 3


async def test_drc_timeout_fails_only_that_connection(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    n = {"drc": 0}

    def drc(t: float) -> DrcResult:
        n["drc"] += 1
        if n["drc"] == 2:
            raise DrcTimeout(t, "still running")
        return DrcResult((), 0, 0.01)

    sctx = FakeSctx(_three(), drc)
    out = await route.route_connections(sctx, _plans3(), lock=True)  # type: ignore[arg-type]
    assert out.routed == ("N1:R1.1-R2.1", "N3:R5.1-R6.1")
    assert out.failed == (("N2:R3.1-R4.1", "timeout"),)
    assert [c["margin"] for c in cpu.calls] == [0.0, 0.0, 0.0]  # no further margin for N2
    assert {net for net, _ in board.items.values()} == {"N1", "N3"}  # N2's items removed
    assert set(board.items) == sctx.created


async def test_drc_reject_then_retry(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    n = {"drc": 0}

    def drc(t: float) -> DrcResult:
        n["drc"] += 1
        if n["drc"] == 1:  # the first attempt's copper violates clearance
            mine = sorted(board.items)
            return DrcResult(
                (
                    DrcMarker("clearance", "error", "Clearance violation", (mine[0], "pad"), None),
                    DrcMarker(
                        "unconnected_items", "error", "Missing connection", tuple(mine), None
                    ),
                    DrcMarker("clearance", "error", "elsewhere", ("other",), None),
                ),
                1,
                0.01,
            )
        return DrcResult(
            (DrcMarker("unconnected_items", "error", "x", tuple(board.items), None),), 1, 0.01
        )

    sctx = FakeSctx(_three(), drc)
    out = await route.route_connections(sctx, _plans3()[:1], lock=False)  # type: ignore[arg-type]
    assert out.routed == ("N1:R1.1-R2.1",)
    assert [c["margin"] for c in cpu.calls] == [0.0, 0.05]
    assert len(board.items) == 1 and set(board.items) == sctx.created
    assert all(lock is False for _, lock in board.items.values())


async def test_drc_rejected_reason(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route, "run_cpu", FakeCpu())

    def drc(t: float) -> DrcResult:
        return DrcResult(
            (DrcMarker("clearance", "error", "Clearance (0.1 mm)", tuple(board.items), None),),
            0,
            0.01,
        )

    sctx = FakeSctx(_three(), drc)
    out = await route.route_connections(sctx, _plans3()[:1], lock=True)  # type: ignore[arg-type]
    assert out.failed == (("N1:R1.1-R2.1", "drc_rejected: Clearance (0.1 mm)"),)
    assert board.items == {} and sctx.created == set()


async def test_accepted_copper_seen_by_later_solves(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    await route.route_connections(FakeSctx(_three()), _plans3(), lock=True)  # type: ignore[arg-type]
    seen = [sorted(t.net for t in c["args"][0].tracks) for c in cpu.calls]
    # N2 sees N1's new track 10 mm away; N3 sees N2's (N1's is beyond the pruned window)
    assert seen == [[], ["N1"], ["N2"]]


async def test_pair_routed_together(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _pair_board()
    plans = route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)

    def answer(fn: Any, args: Any, kw: Any, t: float) -> Any:
        assert fn is route.solve_pair
        _, p, n, gap = args
        assert gap == pytest.approx(0.2)
        return (_straight(p), _straight(n))

    monkeypatch.setattr(route, "run_cpu", FakeCpu(answer))
    out = await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    assert set(out.routed) == {"DP:J1.1-U1.1", "DN:J1.2-U1.2"}
    coupling = out.results[0].coupling
    assert coupling is not None and set(coupling) == {"length_mismatch", "gap_min", "gap_max"}


async def test_pair_falls_back_to_single_nets(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §10.2: a pair that cannot be routed coupled is routed net by net."""
    m = _pair_board()
    plans = route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)

    def answer(fn: Any, args: Any, kw: Any, t: float) -> Any:
        return None if fn is route.solve_pair else _straight(args[1])

    cpu = FakeCpu(answer)
    monkeypatch.setattr(route, "run_cpu", cpu)
    out = await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    assert set(out.routed) == {"DP:J1.1-U1.1", "DN:J1.2-U1.2"}
    assert [c["fn"] for c in cpu.calls] == [route.solve_pair] * 3 + [route.solve] * 2
    assert all(r.coupling is None for r in out.results)


def _blocked_by_n1(fn: Any, args: Any, kw: Any, t: float) -> RoutePath | None:
    """N2 finds no path while N1's copper is on the board."""
    model, req = args[0], args[1]
    if req.net == "N2" and any(tr.net == "N1" for tr in model.tracks):
        return None
    return _straight(req)


async def test_rip_up_retry(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2 is blocked by N1 (routed first): N1 is taken out, N2 routed, N1 routed again."""
    cpu = FakeCpu(_blocked_by_n1)
    monkeypatch.setattr(route, "run_cpu", cpu)
    sctx = FakeSctx(_three(1.5))
    out = await route.route_connections(sctx, _plans3(1.5), lock=True)  # type: ignore[arg-type]
    assert out.failed == () and len(out.routed) == 3
    assert out.routed == ("N1:R1.1-R2.1", "N2:R3.1-R4.1", "N3:R5.1-R6.1")  # plan order
    assert set(board.items) == sctx.created
    assert sorted(net for net, _ in board.items.values()) == ["N1", "N2", "N3"]
    nets = [c["args"][1].net for c in cpu.calls]
    assert nets == ["N1", "N2", "N2", "N2", "N2", "N1", "N3"]


async def test_rip_up_restores_blocker(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2 needs N1 gone, but N1 cannot come back while N2 is there: N2's copper goes,
    N1's old path is put back exactly, N2 is reported failed."""

    def answer(fn: Any, args: Any, kw: Any, t: float) -> RoutePath | None:
        model, req = args[0], args[1]
        if req.net == "N1" and any(tr.net == "N2" for tr in model.tracks):
            return None
        return _blocked_by_n1(fn, args, kw, t)

    monkeypatch.setattr(route, "run_cpu", FakeCpu(answer))
    sctx = FakeSctx(_three(1.5))
    out = await route.route_connections(sctx, _plans3(1.5), lock=True)  # type: ignore[arg-type]
    assert dict(out.failed) == {"N2:R3.1-R4.1": "no_path"}
    assert set(out.routed) == {"N1:R1.1-R2.1", "N3:R5.1-R6.1"}
    assert sorted(net for net, _ in board.items.values()) == ["N1", "N3"]
    assert set(board.items) == sctx.created
    n1 = next(r for r in out.results if r.net == "N1")
    assert set(n1.kiids) <= set(board.items)


# -- pcb_route's total deadline ------------------------------------------------------------


async def test_pcb_route_total_deadline(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """ROUTE_DEADLINE_S = 0: every net failed `timeout`, nothing created, the stages and
    the read-back succeed (a partial report, not an error)."""
    m = _three()

    async def model(ctx: Any) -> BoardModel:
        return m

    async def nothing(*a: Any) -> None:
        return None

    async def doc(ctx: Any) -> BoardDoc:
        return BoardDoc()

    monkeypatch.setattr(route, "require_board_tools", lambda ctx: None)
    monkeypatch.setattr(route, "require_board", lambda ctx: None)
    monkeypatch.setattr(route, "board_model", model)
    monkeypatch.setattr(route, "require_placement_approved", nothing)
    monkeypatch.setattr(route, "read_board_doc", doc)
    monkeypatch.setattr(route, "ROUTE_DEADLINE_S", 0.0)
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    cs = await route.prepare_route(object(), {"nets": ["N1", "N2", "N3"]})  # type: ignore[arg-type]
    assert isinstance(cs, route.RouteChangeSet)
    assert cs.logical.changes == 1 and len(cs.board_stages) == 1
    sctx = FakeSctx(m)
    for stage in cs.board_stages:
        assert not stage.commit
        await stage.run(sctx)  # type: ignore[arg-type]
    assert cs.verify is not None and cs.verify(m, cs.report) == []
    assert [e["status"] for e in cs.report["nets"]] == ["failed"] * 3
    assert all(f["reason"] == "timeout" for e in cs.report["nets"] for f in e["failed"])
    assert cpu.calls == [] and board.items == {}


async def test_pcb_route_budget_and_already_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    m = dataclasses.replace(_three(), tracks=(track("N1", "F.Cu", (9.5, 10.0), (19.5, 10.0)),))

    async def model(ctx: Any) -> BoardModel:
        return m

    async def nothing(*a: Any) -> None:
        return None

    async def doc(ctx: Any) -> BoardDoc:
        return BoardDoc()

    monkeypatch.setattr(route, "require_board_tools", lambda ctx: None)
    monkeypatch.setattr(route, "require_board", lambda ctx: None)
    monkeypatch.setattr(route, "board_model", model)
    monkeypatch.setattr(route, "require_placement_approved", nothing)
    monkeypatch.setattr(route, "read_board_doc", doc)
    only = await route.prepare_route(object(), {"nets": ["N1"]})  # type: ignore[arg-type]
    assert only.board_stages == [] and only.logical.changes == 0  # type: ignore[attr-defined]
    assert only.report["nets"] == [{"net": "N1", "status": "already_routed", "connections": 0}]  # type: ignore[attr-defined]
    with pytest.raises(StructuredToolError) as err:
        await route.prepare_route(object(), {"nets": ["N9"]})  # type: ignore[arg-type]
    assert err.value.code == "not_found" and err.value.details["closest"]["N9"]
    with pytest.raises(StructuredToolError) as err:
        await route.prepare_route(object(), {"nets": ["N1"], "layers": ["In1.Cu"]})  # type: ignore[arg-type]
    assert err.value.code == "not_found"


def test_hole_spacing_keeps_new_vias_off_existing_ones() -> None:
    """A via keepout octagon around every existing via (any net) so a new via keeps the
    hole-to-hole rule; the real router then places no via within it."""
    from copperchat.board.router import build_grid

    from .builders import via

    m = _two_res(
        vias=(via("N1", (15.0, 10.0), kiid="v-own"), via("OTHER", (15.0, 12.0), kiid="v-x")),
        rules=(("min_clearance", 0.2), ("hole_to_hole_min", 0.45)),
    )
    (p,) = _plan(m, ["N1"])
    zones = route.with_hole_spacing(m, p.req).zones
    assert len(zones) == 2 and all(z.keepout == {"vias"} and z.rule_area for z in zones)
    need = 0.15 + 0.15 + 0.45  # drill/2 + drill/2 + hole-to-hole
    grid = build_grid(route.router_model(m, p.req), p.req)
    plain = build_grid(m, p.req)
    for v in m.vias:
        for dx, dy in ((1, 0), (0, 1), (-0.7071, 0.7071)):
            near = grid.cell(v.at[0] + dx * (need - 0.06), v.at[1] + dy * (need - 0.06))
            far = grid.cell(v.at[0] + dx * (need / math.cos(math.pi / 8) + 0.06),
                            v.at[1] + dy * (need / math.cos(math.pi / 8) + 0.06))  # fmt: skip
            assert not grid.via_ok(*near), (v.kiid, dx, dy)
            assert grid.via_ok(*far), (v.kiid, dx, dy)
    own = plain.cell(15.0 + need - 0.06, 10.0)
    assert plain.via_ok(*own)  # without it the router could drop a via next to an own one
    no_vias = dataclasses.replace(p.req, allow_vias=False)
    assert route.with_hole_spacing(m, no_vias) is m


def test_router_rejects_close_via_pair() -> None:
    """P5a `valid_path` (minimal P5b fix): two vias of one path closer than via diameter +
    clearance are refused — a via, a 0.05 mm hop on the other layer and a via faked a
    135° turn on sp4_synth."""
    from copperchat.board.router import build_grid
    from copperchat.board.router.astar import valid_path

    from .builders import part

    m = circuit(
        part("P1", 10.0, 10.0, pads=(pad("P1", "1", "N1", (10.0, 10.0)),)),
        part("P2", 20.0, 10.0, pads=(pad("P2", "1", "N1", (20.0, 10.0)),)),
    )
    (p,) = _plan(m, ["N1"])
    grid = build_grid(m, p.req)
    a, b = (p.a.x, p.a.y), (p.b.x, p.b.y)

    def path(v1: tuple[float, float], v2: tuple[float, float]) -> RoutePath:
        w = p.req.width
        segs = (("F.Cu", a, v1, w), ("B.Cu", v1, v2, w), ("F.Cu", v2, b, w))
        return RoutePath(segs, (v1, v2), 10.0)

    assert valid_path(path((12.0, 10.0), (16.0, 10.0)), grid, p.req)
    assert not valid_path(path((12.0, 10.0), (12.05, 10.0)), grid, p.req)
    assert not valid_path(path((12.0, 10.0), (12.79, 10.0)), grid, p.req)  # < 0.6 + 0.2


async def test_rip_up_restore_survives_deadline(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocked connection's retry uses up the time: the taken-out connection is put
    back exactly (no DRC gate that a spent deadline would turn into a removal)."""
    deadline = clock.now + 100.0

    def answer(fn: Any, args: Any, kw: Any, t: float) -> RoutePath | None:
        model, req = args[0], args[1]
        if req.net == "N1" and any(tr.net == "N2" for tr in model.tracks):
            return None
        if req.net == "N2" and not any(tr.net == "N1" for tr in model.tracks):
            clock.now = deadline + 1  # the retry ends after the deadline
        return _blocked_by_n1(fn, args, kw, t)

    drcs: list[float] = []

    def drc(t: float) -> DrcResult:
        drcs.append(t)
        return DrcResult((), 0, 0.01)

    monkeypatch.setattr(route, "run_cpu", FakeCpu(answer))
    sctx = FakeSctx(_three(1.5), drc)
    out = await route.route_connections(  # type: ignore[arg-type]
        sctx, _plans3(1.5)[:2], lock=True, deadline=deadline
    )
    n1 = next(r for r in out.results if r.net == "N1")
    assert n1.status == "routed" and set(n1.kiids) <= set(board.items)
    assert dict(out.failed) == {"N2:R3.1-R4.1": "timeout"}
    assert sorted(net for net, _ in board.items.values()) == ["N1"]
    assert set(board.items) == sctx.created


async def test_drc_of_rejected_copper_not_reported(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RouteOutcome.drc` is the last DRC whose copper stayed, never one of a rejection."""
    monkeypatch.setattr(route, "run_cpu", FakeCpu())
    n = {"drc": 0}
    keep = DrcResult((), 0, 0.01)

    def drc(t: float) -> DrcResult:
        n["drc"] += 1
        if n["drc"] == 1:
            return keep
        mine = tuple(sorted(board.items))[-1:]
        return DrcResult((DrcMarker("clearance", "error", "x", mine, None),), 0, 0.01)

    out = await route.route_connections(  # type: ignore[arg-type]
        FakeSctx(_three(), drc), _plans3()[:2], lock=True
    )
    assert len(out.routed) == 1 and out.drc is keep


def test_pair_keepouts_one_pass() -> None:
    """A pair gets each footprint keepout once, exempt when it holds a pad of either net."""
    m = _pair_board()
    p, n = route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)
    around_n = ((10.3, 9.8), (10.7, 9.8), (10.7, 10.2), (10.3, 10.2))  # holds J1.2 (DN)
    far = ((30.0, 30.0), (31.0, 30.0), (31.0, 31.0), (30.0, 31.0))
    m2 = dataclasses.replace(
        m, parts=(*m.parts, res("A1", "X", "Y", (60, 60), rule_areas=(around_n, far)))
    )
    zones = route.router_model(m2, p.req, n.req).zones
    keepouts = [z for z in zones if z.keepout == {"tracks", "vias"}]
    assert [z.outline for z in keepouts] == [far]


def test_hole_spacing_around_drilled_pads() -> None:
    """Drilled pads (any net, also the routed net's own) get the hole-spacing octagon."""
    tht = dataclasses.replace(
        pad("J9", "1", "N9", (40.0, 10.0), w=1.7, h=1.7),
        drill=1.0,
        smd=False,
        layers=frozenset({"*.Cu"}),
    )
    from .builders import part

    m = _two_res(part("J9", 40.0, 10.0, pads=(tht,)), rules=(("hole_to_hole_min", 0.45),))
    (p,) = _plan(m, ["N1"])
    (z,) = route.with_hole_spacing(m, p.req).zones
    apothem = 0.5 + 0.15 + 0.45 - 0.3
    assert min(math.dist((40.0, 10.0), q) for q in z.outline) == pytest.approx(
        apothem / math.cos(math.pi / 8)
    )


async def test_no_push_after_deadline(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A solve that ends past the deadline: nothing pushed, no DRC started."""
    deadline = clock.now + 5.0

    def answer(fn: Any, args: Any, kw: Any, t: float) -> RoutePath:
        clock.now = deadline + 1
        return _straight(args[1])

    monkeypatch.setattr(route, "run_cpu", FakeCpu(answer))
    sctx = FakeSctx(_three())
    out = await route.route_connections(  # type: ignore[arg-type]
        sctx, _plans3()[:1], lock=True, deadline=deadline
    )
    assert out.failed == (("N1:R1.1-R2.1", "timeout"),)
    assert board.items == {} and sctx.drc_timeouts == [] and sctx.ctx.session.commits == []


async def test_second_round_retries_pair_nets_alone(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair whose nets also failed alone is retried in the second round net by net
    (never as the coupled pair again)."""
    m = _pair_board()
    plans = route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)
    cpu = FakeCpu(lambda fn, args, kw, t: None)
    monkeypatch.setattr(route, "run_cpu", cpu)
    out = await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    assert dict(out.failed) == {"DP:J1.1-U1.1": "no_path", "DN:J1.2-U1.2": "no_path"}
    assert [c["fn"] for c in cpu.calls] == [route.solve_pair] * 3 + [route.solve] * 6


async def test_no_drc_when_push_used_up_the_time(
    board: Board, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = clock.now + 5.0
    real_create = board.create

    def slow_create(h: Any, items: Any, lock: bool) -> list[str]:
        clock.now = deadline + 1  # the commit ends after the deadline
        return real_create(h, items, lock)

    monkeypatch.setattr(route, "_create", slow_create)
    monkeypatch.setattr(route, "run_cpu", FakeCpu())
    sctx = FakeSctx(_three())
    out = await route.route_connections(  # type: ignore[arg-type]
        sctx, _plans3()[:1], lock=True, deadline=deadline
    )
    assert out.failed == (("N1:R1.1-R2.1", "timeout"),)
    assert board.items == {} and sctx.drc_timeouts == [] and sctx.created == set()


async def test_duplicate_pad_numbers_kept_apart(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections with the same pad names (a part's pad "2" twice, e.g. a SOT-223
    tab): both are routed and reported, the second key gets "#2"."""
    from .builders import part

    u5 = part(
        "U5",
        10.0,
        15.0,
        pads=(pad("U5", "2", "N1", (10.0, 10.0)), pad("U5", "2", "N1", (10.0, 20.0))),
    )
    r1 = part("R1", 12.0, 15.0, pads=(pad("R1", "1", "N1", (12.0, 15.0)),))
    m = circuit(u5, r1)
    plans = _plan(m, ["N1"])
    assert len(plans) == 2 and len({route.conn_key(p.net, p.a, p.b) for p in plans}) == 1
    monkeypatch.setattr(route, "run_cpu", FakeCpu())
    out = await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    assert out.routed == ("N1:R1.1-U5.2", "N1:R1.1-U5.2#2")
    assert len(out.results) == 2 and len({k for r in out.results for k in r.kiids}) == 2
    assert set(board.items) == {k for r in out.results for k in r.kiids}


# -- R3: straight first (owner rule b) -----------------------------------------------------


def _adjacent() -> BoardModel:
    """R1.2 (10.5, 10) and R2.1 (11.5, 10) on N1, aligned 0.4 mm apart (a corridor);
    R3/R4 on N2 far apart (no corridor)."""
    return circuit(
        res("R1", "A", "N1", (10.0, 10.0)),
        res("R2", "N1", "B", (12.0, 10.0)),
        res("R3", "N2", "C", (10.0, 20.0)),
        res("R4", "D", "N2", (20.0, 20.0)),
    )


def test_straight_path_of_aligned_pair() -> None:
    m = _adjacent()
    (p,) = _plan(m, ["N1"])
    path = route.straight_path(m, p)
    assert path is not None and path.vias == ()
    assert path.segments == (("F.Cu", (10.5, 10.0), (11.5, 10.0), 0.2),)
    (q,) = _plan(m, ["N2"])
    assert route.straight_path(m, q) is None


async def test_straight_first_aligned_pair(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass 0: the aligned pair gets its one segment (no search); the other net is searched."""
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    m = _adjacent()
    sctx = FakeSctx(m)
    out = await route.route_connections(sctx, _plan(m, ["N1", "N2"]), lock=True)  # type: ignore[arg-type]
    assert set(out.routed) == {"N1:R1.2-R2.1", "N2:R3.1-R4.2"}
    assert [c["args"][1].net for c in cpu.calls] == ["N2"]
    n1 = next(r for r in out.results if r.net == "N1")
    assert len(n1.kiids) == 1 and n1.length_mm == pytest.approx(1.0)


async def test_straight_first_falls_back(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """The straight segment is free in geometry but DRC-rejected (clearance to a zone): it is
    removed and the search routes the connection, no error."""
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    m = _adjacent()
    first = {"done": False}

    def drc(t: float) -> DrcResult:
        if first["done"]:
            return DrcResult((), 0, 0.01)
        first["done"] = True
        (k,) = list(board.items)
        return DrcResult((DrcMarker("clearance", "error", "zone", (k, "zone-1"), None),), 0, 0.01)

    sctx = FakeSctx(m, drc)
    out = await route.route_connections(sctx, _plan(m, ["N1"]), lock=True)  # type: ignore[arg-type]
    assert out.routed == ("N1:R1.2-R2.1",) and out.failed == ()
    assert len(cpu.calls) == 1  # the search
    assert set(board.items) == sctx.created and len(board.items) == 1


async def test_straight_first_retries_after_sibling(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two pass-0 segments that clash only with each other are both removed; each is tried
    again alone at the end of the pass (the second then clashes with the first one kept)."""
    cpu = FakeCpu(lambda fn, args, kw, t: None)
    monkeypatch.setattr(route, "run_cpu", cpu)
    m = circuit(
        res("R1", "A", "N1", (10.0, 10.0)),
        res("R2", "N1", "B", (12.0, 10.0)),
        res("R5", "E", "N3", (10.0, 14.0)),
        res("R6", "N3", "F", (12.0, 14.0)),
    )
    calls = {"n": 0}

    def drc(t: float) -> DrcResult:
        calls["n"] += 1
        ks = list(board.items)
        if calls["n"] == 1:  # both pushed together: they "clash"
            return DrcResult((DrcMarker("clearance", "error", "x", tuple(ks), None),), 0, 0.01)
        if calls["n"] == 3:  # the second retry clashes with the first (kept) one
            return DrcResult((DrcMarker("clearance", "error", "x", tuple(ks), None),), 0, 0.01)
        return DrcResult((), 0, 0.01)

    sctx = FakeSctx(m, drc)
    out = await route.route_connections(sctx, _plan(m, ["N1", "N3"]), lock=True)  # type: ignore[arg-type]
    assert out.routed == ("N1:R1.2-R2.1",)
    assert [k for k, _ in out.failed] == ["N3:R5.2-R6.1"]  # its search found nothing
    assert {c["args"][1].net for c in cpu.calls} == {"N3"}


def test_corridor_reservations_free_only() -> None:
    m = _adjacent()
    res_all = route.corridor_reservations(m, classify(m))
    assert [r.key for r in res_all] == ["N1:R1.2-R2.1"]
    blocked = dataclasses.replace(m, tracks=(track("X", "F.Cu", (11.0, 9.0), (11.0, 11.0)),))
    assert route.corridor_reservations(blocked, classify(blocked)) == ()


async def test_corridor_reserved_across_calls(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing N2 alone (a chunk) while N1's corridor is still free: N2's search sees that
    corridor as N1 copper; N1's own plans never see their reservation."""
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    m = _adjacent()
    reserve = route.corridor_reservations(m, classify(m))
    sctx = FakeSctx(m)
    await route.route_connections(sctx, _plan(m, ["N2"]), lock=True, reserve=reserve)  # type: ignore[arg-type]
    (call,) = cpu.calls
    fake = [t for t in call["args"][0].tracks if t.kiid.startswith("reserved:")]
    assert [(t.net, t.start, t.end) for t in fake] == [("N1", (10.5, 10.0), (11.5, 10.0))]
    cpu.calls.clear()
    monkeypatch.setattr(route, "straight_path", lambda model, plan: None)
    await route.route_connections(FakeSctx(m), _plan(m, ["N1"]), lock=True, reserve=reserve)  # type: ignore[arg-type]
    (call,) = cpu.calls
    assert not [t for t in call["args"][0].tracks if t.kiid.startswith("reserved:")]


def test_straight_segments_never_ripped() -> None:
    """A pass-0 straight segment is no rip-up candidate for a blocked connection (owner rule
    b: re-routing it through the search would make a dogleg)."""
    m = _adjacent()
    (p,) = _plan(m, ["N1"])
    (q,) = _plan(m, ["N2"])
    path = route.straight_path(m, p)
    assert path is not None
    st = route._Loop(m, True, None)
    st.kept.append(route._Kept([p], [path], ["k1"], straight=True))
    near = dataclasses.replace(q, a=dataclasses.replace(q.a, x=11.0, y=10.5))
    assert route._blockers(st, [near]) == []
    st.kept[0].straight = False
    assert route._blockers(st, [near]) == [st.kept[0]]


def test_same_part_connections_plan_last() -> None:
    """R3: a connection between two pads of one part (USB-C A6 - B6) comes after the net's
    other connections, so it can land on their copper as a T; every non-pair plan may."""
    from .builders import part as mk_part

    j1 = mk_part(
        "J1",
        10.0,
        10.0,
        pads=(pad("J1", "A6", "N1", (10.0, 10.0)), pad("J1", "B6", "N1", (11.0, 10.0))),
    )
    m = circuit(j1, res("R1", "N1", "X", (16.0, 10.0)))
    plans = _plan(m, ["N1"], tree=True)
    assert [(p.a.ref, p.b.ref) for p in plans][-1] == ("J1", "J1")
    assert all(p.req.tree for p in plans)
    assert not any(p.req.tree for p in _plan(m, ["N1"]))


def _usbc() -> BoardModel:
    """USB-C-like row: B7 (DN) A6 (DP) A7 (DN) B6 (DP), 0.5 mm apart; and an ESD-like part
    whose pass-throughs DP 1-6 and DN 3-4 run parallel."""
    from .builders import part as mk_part

    row = [("B7", "DN"), ("A6", "DP"), ("A7", "DN"), ("B6", "DP")]
    j1 = mk_part(
        "J1",
        10.0,
        10.0,
        pads=tuple(
            pad("J1", num, net, (9.25 + 0.5 * k, 10.0), w=0.3, h=1.0)
            for k, (num, net) in enumerate(row)
        ),
    )
    u2 = mk_part(
        "U2",
        20.0,
        10.0,
        pads=(
            pad("U2", "1", "DP", (19.0, 9.0)),
            pad("U2", "6", "DP", (19.0, 11.0)),
            pad("U2", "3", "DN", (21.0, 9.0)),
            pad("U2", "4", "DN", (21.0, 11.0)),
        ),
    )
    return circuit(j1, u2)


def test_pair_match_skips_crossing_same_part() -> None:
    m = _usbc()
    plans = route.plan_connections(
        m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True, tree=True
    )
    pairs = {(p.a.ref, p.b.ref, p.a.number, p.b.number) for p in plans if p.pair is not None}
    assert ("J1", "J1", "A6", "B6") not in pairs and ("J1", "J1", "B6", "A6") not in pairs
    esd = {(p.net, p.pair) for p in plans if p.a.ref == p.b.ref == "U2"}
    assert esd == {("DP", "DN"), ("DN", "DP")}  # parallel pass-throughs still couple


async def test_straight_pair_records_coupling(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both connections of a matched pair routed by their straight segment: coupled."""
    monkeypatch.setattr(route, "run_cpu", FakeCpu())
    m = _usbc()
    plans = [
        p
        for p in route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)
        if p.a.ref == p.b.ref == "U2"
    ]
    out = await route.route_connections(FakeSctx(m), plans, lock=True)  # type: ignore[arg-type]
    assert len(out.routed) == 2
    assert all(r.coupling is not None for r in out.results)


def test_m8_connections_first() -> None:
    """The connections M8 measures (a decoupling cap's supply pad to its IC pin) are
    planned before every other one, so earlier nets cannot wall them off."""
    from copperchat.board.classify import Decoupling

    m = _two_res(res("C1", "N9", "GND", (40.0, 10.0)), res("R9", "N9", "Z", (43.0, 10.0)))
    cls = dataclasses.replace(classify(m), decoupling=(Decoupling("C1", "R9", "1", "N9", "GND"),))
    plans = route.plan_connections(m, cls, ["N1", "N9"], layers=None, allow_vias=True)
    assert (plans[0].a.ref, plans[0].b.ref) in (("C1", "R9"), ("R9", "C1"))
    assert [p.net for p in plans] == ["N9", "N1"]


async def test_pass0_pair_half_straight_alone(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half of a matched pair keeps its straight segment even when the other half's is
    DRC-rejected (owner rule b outranks coupling); the other half is routed alone."""
    cpu = FakeCpu()
    monkeypatch.setattr(route, "run_cpu", cpu)
    m = _usbc()
    plans = [
        p
        for p in route.plan_connections(m, _pair_cls(m), ["DP", "DN"], layers=None, allow_vias=True)
        if p.a.ref == p.b.ref == "U2"
    ]
    first = {"done": False}

    def drc(t: float) -> DrcResult:
        if first["done"]:
            return DrcResult((), 0, 0.01)
        first["done"] = True
        k = next(iter(board.items))  # the first half's segment
        return DrcResult((DrcMarker("clearance", "error", "x", (k, "zone"), None),), 0, 0.01)

    out = await route.route_connections(FakeSctx(m, drc), plans, lock=True)  # type: ignore[arg-type]
    assert len(out.routed) == 2
    assert [c["fn"] for c in cpu.calls] == [route.solve]  # the rejected half, alone


def test_entry_defect_reported() -> None:
    """A routed path with a pad-entry loop is flagged (`pad_entry_defects`)."""
    from copperchat.board.router import RoutePath

    m = _two_res()
    (p,) = _plan(m, ["N1"])
    a = (p.a.x, p.a.y)
    pts = [a, (a[0] + 0.3, a[1]), (a[0] + 0.6, a[1] - 0.3), (a[0] + 0.6, a[1] - 0.6)]
    pts += [(a[0] + 0.3, a[1] - 0.9), (a[0] - 0.6, a[1] - 0.9)]
    loop = RoutePath(tuple(("F.Cu", u, v, 0.2) for u, v in itertools.pairwise(pts)), (), 0.0)
    assert route.entry_defect(loop, p.req)
    straight = RoutePath((("F.Cu", a, (p.b.x, p.b.y), 0.2),), (), 0.0)
    assert not route.entry_defect(straight, p.req)
    r = route.ConnResult("k", "N1", "R1.1", "R2.1", "routed", None, 1.0, 0, (), pad_entry=True)
    assert route._net_entry("N1", [r], True)["pad_entry_defects"] == ["R1.1-R2.1"]


async def test_pass0_retries_capped_and_drc_filtered(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six straight candidates that all clash only with each other: at most
    STRAIGHT_RETRY_MAX retries (each its own DRC) follow the batch DRC, and the stored
    DRC never names a segment that was dropped again."""
    monkeypatch.setattr(route, "run_cpu", FakeCpu(lambda fn, args, kw, t: None))
    parts = []
    nets = []
    for k in range(6):
        y = 10.0 + 4.0 * k
        parts += [res(f"R{2 * k + 1}", f"A{k}", f"N{k}", (10.0, y))]
        parts += [res(f"R{2 * k + 2}", f"N{k}", f"B{k}", (12.0, y))]
        nets.append(f"N{k}")
    m = circuit(*parts)
    calls = {"n": 0}

    def drc(t: float) -> DrcResult:
        calls["n"] += 1
        ks = tuple(board.items)
        if calls["n"] == 1:  # the batch: everything clashes with everything
            return DrcResult((DrcMarker("clearance", "error", "x", ks, None),), 0, 0.01)
        return DrcResult((DrcMarker("clearance", "error", "x", (ks[-1], "other"), None),), 0, 0.01)

    sctx = FakeSctx(m, drc)
    out = await route.route_connections(sctx, _plan(m, nets), lock=True)  # type: ignore[arg-type]
    assert calls["n"] == 1 + route.STRAIGHT_RETRY_MAX
    assert out.routed == ()
    assert out.drc is None  # nothing kept: no verified DRC to report


async def test_pass0_stored_drc_without_dropped_copper(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass-0 segment kept, two clashing ones dropped (and failing their retry): the
    stored DRC keeps a marker of other copper but none naming a dropped segment."""
    monkeypatch.setattr(route, "run_cpu", FakeCpu(lambda fn, args, kw, t: None))
    parts, nets = [], []
    for k in range(3):
        y = 10.0 + 4.0 * k
        parts += [res(f"R{2 * k + 1}", f"A{k}", f"N{k}", (10.0, y))]
        parts += [res(f"R{2 * k + 2}", f"N{k}", f"B{k}", (12.0, y))]
        nets.append(f"N{k}")
    m = circuit(*parts)
    other = DrcMarker("silk_overlap", "warning", "elsewhere", ("zone-9",), None)
    calls = {"n": 0}

    def drc(t: float) -> DrcResult:
        calls["n"] += 1
        ks = tuple(board.items)
        if calls["n"] == 1:  # segments 2 and 3 clash with each other
            clash = DrcMarker("clearance", "error", "x", (ks[1], ks[2]), None)
            return DrcResult((clash, other), 0, 0.01)
        return DrcResult((DrcMarker("clearance", "error", "x", (ks[-1], "z"), None),), 0, 0.01)

    out = await route.route_connections(FakeSctx(m, drc), _plan(m, nets), lock=True)  # type: ignore[arg-type]
    assert len(out.routed) == 1
    assert out.drc is not None and out.drc.markers == (other,)


def test_reservation_of_pair_net_uses_pair_width() -> None:
    m = _usbc()
    cls = _pair_cls(m)
    nc = dataclasses.replace(net_class(track_width=0.2), diff_pair_width=0.35, diff_pair_gap=0.15)
    m = dataclasses.replace(m, net_classes=(nc,))
    got = {r.net: r.width for r in route.corridor_reservations(m, cls)}
    assert got and all(w == pytest.approx(0.35) for w in got.values()), got


def test_reservation_of_pair_net_falls_back_to_class_width() -> None:
    """A pair net's connection whose pads are too narrow for the pair width (an unmatched
    one, placed at the class width) is still reserved, at the class width."""
    m = _usbc()
    nc = dataclasses.replace(net_class(track_width=0.2), diff_pair_width=0.7, diff_pair_gap=0.15)
    m = dataclasses.replace(m, net_classes=(nc,))
    got = {r.net: r.width for r in route.corridor_reservations(m, _pair_cls(m))}
    assert got and all(w == pytest.approx(0.2) for w in got.values()), got


def test_reservation_width_per_connection() -> None:
    """Round 3 regression: a fully necked first connection does not shrink the width of
    the net's later ones (each is reserved at its own placed width)."""
    from .builders import part as mk_part

    fine = mk_part("U1", 10.0, 10.0, pads=(pad("U1", "1", "P", (10.0, 10.0), w=0.25, h=0.25),))
    near = mk_part("U2", 10.6, 10.0, pads=(pad("U2", "1", "P", (10.6, 10.0), w=0.25, h=0.25),))
    wide = mk_part("C1", 20.0, 10.0, pads=(pad("C1", "1", "P", (20.0, 10.0), w=1.0, h=1.0),))
    wide2 = mk_part("C2", 21.5, 10.0, pads=(pad("C2", "1", "P", (21.5, 10.0), w=1.0, h=1.0),))
    m = circuit(fine, near, wide, wide2, net_classes=(net_class(track_width=0.5),))
    got = sorted(r.width for r in route.corridor_reservations(m, classify(m)))
    assert got[-1] == pytest.approx(0.5), got


def test_reservation_pair_width_below_track_width() -> None:
    """A pair width below the track width is reserved as placed (pair_width, no max)."""
    m = _usbc()
    nc = dataclasses.replace(net_class(track_width=0.3), diff_pair_width=0.15, diff_pair_gap=0.15)
    m = dataclasses.replace(m, net_classes=(nc,))
    got = {r.net: r.width for r in route.corridor_reservations(m, _pair_cls(m))}
    assert got and all(w == pytest.approx(0.15) for w in got.values()), got


def test_reservation_falls_back_when_pair_corridor_blocked() -> None:
    """The pair-width corridor exists but is blocked by another net's track that leaves
    room for the narrower class width: the class-width option is reserved."""
    m = _usbc()
    nc = dataclasses.replace(net_class(track_width=0.1), diff_pair_width=0.5, diff_pair_gap=0.15)
    # DP U2.1 (19, 9) - U2.6 (19, 11): a vertical corridor at x = 19
    blocker = track("X", "F.Cu", (19.4, 9.5), (19.4, 10.5), 0.1)
    m = dataclasses.replace(m, net_classes=(nc,), tracks=(blocker,))
    got = {r.key: r.width for r in route.corridor_reservations(m, _pair_cls(m))}
    dp = [w for k, w in got.items() if k.startswith("DP:")]
    assert dp == [pytest.approx(0.127)], got  # the class width, floored at min_track_width
