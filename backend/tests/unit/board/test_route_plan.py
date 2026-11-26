"""`pcb_route` planning and the per-connection loop (SP4 P5b Tasks 1-2), pure: builders for
the board, a fake `BoardStageContext` (commits, DRC, board reads) and a fake `run_cpu`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
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
    for _, a, b in path.segments:
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
    return RoutePath(((req.layers[0], a, b),), (), abs(b[0] - a[0]) + abs(b[1] - a[1]))


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
        segs = (("F.Cu", a, v1), ("B.Cu", v1, v2), ("F.Cu", v2, b))
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
