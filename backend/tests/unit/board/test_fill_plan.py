"""Pure planning helpers of `pcb_route_fill` (SP4 P6b Task 2): scoped ground deletion,
plane-layer defaults and the one-line DSN switch for the plane layers."""

from __future__ import annotations

import pytest

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import classify
from copperchat.kicad_tools.board.compensate import CopperSnapshot
from copperchat.kicad_tools.board.fill import (
    default_plane_layers,
    disable_layers,
    new_ground_items,
    plan_fill,
)

from .builders import board, outline, track, via, zone

FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def _snap(tracks: frozenset[str], vias: frozenset[str]) -> CopperSnapshot:
    e: frozenset[str] = frozenset()
    return CopperSnapshot(tracks, vias, e, e, e, e, ())


def test_new_ground_items_only_new() -> None:
    old_via = via("GND", kiid="v-old", locked=True)
    old_stub = track("GND", kiid="t-old", locked=True)
    m = board(
        tracks=(old_stub, track("GND", kiid="t-new"), track("N1", kiid="t-sig")),
        vias=(old_via, via("GND", kiid="v-new")),
    )
    snap = _snap(frozenset({"t-old"}), frozenset({"v-old"}))
    assert new_ground_items(snap, m, frozenset({"GND"})) == ["t-new", "v-new"]
    pre = board(tracks=(old_stub,), vias=(old_via,))
    assert new_ground_items(pre, m, frozenset({"GND"})) == ["t-new", "v-new"]


def test_plane_layers_default() -> None:
    ground = frozenset({"GND"})
    m4 = board(copper_layers=FOUR, outline=outline(0, 0, 100, 80))
    assert default_plane_layers(m4, ground) == ("In1.Cu",)
    m2 = board(outline=outline(0, 0, 100, 80))
    assert default_plane_layers(m2, ground) == ()
    six = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
    assert default_plane_layers(board(copper_layers=six), ground) == ("In1.Cu", "In4.Cu")
    # a GND zone covering 90 % of the outline on In2.Cu is the plane
    poly = ((0.0, 0.0), (90.0, 0.0), (90.0, 80.0), (0.0, 80.0))
    big = board(
        copper_layers=FOUR,
        outline=outline(0, 0, 100, 80),
        zones=(zone(layers=("In2.Cu",), poly=poly),),
    )
    assert default_plane_layers(big, ground) == ("In2.Cu",)
    # a small one or another net's zone is not
    small = board(copper_layers=FOUR, zones=(zone(layers=("In2.Cu",)),))
    assert default_plane_layers(small, ground) == ("In1.Cu",)
    other = board(copper_layers=FOUR, zones=(zone(net="+3V3", layers=("In2.Cu",), poly=poly),))
    assert default_plane_layers(other, ground) == ("In1.Cu",)


def test_plan_fill_lists_open_non_ground() -> None:
    m = board(copper_layers=FOUR)
    plan = plan_fill(m, classify(m, BoardDoc()), None)
    assert plan.plane_layers == ("In1.Cu",)
    assert plan.open == ()
    assert plan.filled_zones == ()


DSN = """(pcb x.dsn
  (structure
    (layer F.Cu
      (type signal)
      (property
        (index 0)
      )
    )
    (layer In1.Cu
      (type signal)
      (property
        (index 1)
      )
    )
    (boundary
      (path pcb 0  0 0  10 0)
    )
  )
)
"""


def test_dsn_disable_layers_one_line() -> None:
    out = disable_layers(DSN, ["In1.Cu"])
    old, new = DSN.splitlines(), out.splitlines()
    assert len(new) == len(old) + 1
    added = [ln for ln in new if ln not in old]
    assert added == ["    (autoroute_settings (layer_rule In1.Cu (active off)))"]
    assert new.index(added[0]) == old.index("    (boundary")
    assert disable_layers(DSN, []) == DSN


@pytest.mark.parametrize(
    ("text", "layers"),
    [
        (DSN, ["In2.Cu"]),
        (DSN.replace("    (boundary", "    (autoroute_settings)\n    (boundary"), ["In1.Cu"]),
        (DSN.replace("(boundary", "(keepout"), ["In1.Cu"]),
    ],
)
def test_dsn_disable_layers_refuses(text: str, layers: list[str]) -> None:
    with pytest.raises(ValueError):
        disable_layers(text, layers)
