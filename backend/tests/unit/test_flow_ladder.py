"""R7 ladder rungs of the e2e board flow (`tests/kicad_e2e/flow._route`), with a scripted
fake flow: the own router routes the rest after the last refused fill, and a fill-open
connection the own router cannot route gets the fill copper around it cleared."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.kicad_e2e import flow
from tests.kicad_e2e.flow import FlowError


def _refused(nets: list[str]) -> dict[str, Any]:
    return {
        "error": {
            "code": "fill_not_acceptable",
            "details": {
                "reason": "rip_ratio",
                "connections": [{"connection": f"{n}:A.1-B.1"} for n in nets],
            },
        }
    }


class FakeFlow:
    def __init__(
        self, fills: list[tuple[dict[str, Any], bool]], route_fail: dict[str, int]
    ) -> None:
        self.fills = fills  # (data, err) per pcb_route_fill call
        self.route_fail = route_fail  # net -> how many pcb_route calls still fail it
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers = flow.Answers(outline={}, rules={}, blocks=None, zones=None, stitch={})
        self.turn_no, self.label = 4, "routing"
        self.results: dict[str, Any] = {}
        self.signal_nets = ["C1", "F1", "F2", "F3"]

    async def try_tool(
        self, name: str, /, *, cost: int = 0, **args: Any
    ) -> tuple[dict[str, Any], bool]:
        self.calls.append((name, args))
        assert name == "pcb_route_fill"
        return self.fills.pop(0)

    async def tool(self, name: str, /, *, cost: int = 0, **args: Any) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "pcb_unroute":
            return {"nets": ["F3", "F1"]}
        assert name == "pcb_route"
        out = []
        for n in args["nets"]:
            if self.route_fail.get(n, 0) > 0:
                self.route_fail[n] -= 1
                out.append({"net": n, "status": "failed", "failed": [{"reason": "no_path"}]})
            else:
                out.append({"net": n, "status": "routed"})
        return {"nets": out}


def _routes(f: FakeFlow) -> list[list[str]]:
    return [a["nets"] for n, a in f.calls if n == "pcb_route"]


def test_own_router_routes_the_rest_after_the_last_refused_fill() -> None:
    f = FakeFlow([(_refused(["F1"]), True)] * flow.FILL_ATTEMPTS, {"C1": 1})
    got = asyncio.run(flow._route(f, ["C1"]))  # type: ignore[arg-type]
    assert got == {"completion": 1.0, "own_router_rest": 4}
    # the critical net that failed first is routed again with the rest (review round 1)
    assert _routes(f)[-1] == ["C1", "F1", "F2", "F3"]


def test_rest_rung_failure_is_a_flow_error() -> None:
    f = FakeFlow([(_refused(["F1"]), True)] * flow.FILL_ATTEMPTS, {"F2": 99})
    with pytest.raises(FlowError, match="still open after the ladder"):
        asyncio.run(flow._route(f, ["C1"]))  # type: ignore[arg-type]


def test_open_after_fill_clears_the_region_and_reroutes() -> None:
    fill = {"completion": 0.9, "open": [{"net": "F2", "x": 50.0, "y": 40.0}]}
    f = FakeFlow([(fill, False)], {"F2": 1})
    got = asyncio.run(flow._route(f, ["C1"]))  # type: ignore[arg-type]
    assert got is fill
    unroute = [a for n, a in f.calls if n == "pcb_unroute"]
    assert unroute == [{"region": {"x": 46.0, "y": 36.0, "w": 8.0, "h": 8.0}}]
    assert _routes(f)[-1] == ["F2", "F1", "F3"]  # the open net first, then the cleared ones


def test_clear_reroutes_even_when_only_the_open_net_was_cleared() -> None:
    fill = {"completion": 0.9, "open": [{"net": "F2", "x": 1.0, "y": 1.0}]}
    f = FakeFlow([(fill, False)], {"F2": 1})

    real = f.tool

    async def unroute_only_f2(name: str, /, *, cost: int = 0, **args: Any) -> dict[str, Any]:
        if name == "pcb_unroute":
            f.calls.append((name, args))
            return {"nets": ["F2"]}
        return await real(name, cost=cost, **args)

    f.tool = unroute_only_f2  # type: ignore[method-assign]
    asyncio.run(flow._route(f, ["C1"]))  # type: ignore[arg-type]
    assert _routes(f)[-2:] == [["F2"], ["F2"]]  # failed once, cleared, routed again
