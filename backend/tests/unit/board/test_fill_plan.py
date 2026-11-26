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


# -- helpers of the post-processing stages (review round 1) -----------------------------------

from copperchat.board.geom import pad_entry_ends  # noqa: E402
from copperchat.kicad_tools.board.fill import (  # noqa: E402
    _redundant,
    _rip_copper,
    _widen,
    _worst_areas,
    key_net,
)

from .builders import net_class, part  # noqa: E402
from .builders_circuit import pad  # noqa: E402


def _two_pads(net: str = "N1") -> tuple:
    a = pad("R1", "1", net, (10.0, 10.0))
    b = pad("R2", "1", net, (20.0, 10.0))
    return a, b


def _board_ab(tracks: tuple, net: str = "N1", **kw: object) -> object:
    a, b = _two_pads(net)
    return board(
        parts=(part("R1", 10, 10, pads=(a,)), part("R2", 20, 10, pads=(b,))),
        tracks=tracks,
        net_classes=(net_class(track_width=0.2),),
        net_class_of=((net, "Default"),),
        **kw,
    )


def test_key_net_allows_colons() -> None:
    assert key_net("/A:B:R1.1-R2.1") == "/A:B"
    assert key_net("N1:R1.1-R2.1") == "N1"


def test_pad_entry_ends_strict() -> None:
    def in_pad(p: tuple[float, float]) -> bool:  # a pad around the origin, 0.5 mm half size
        return abs(p[0]) <= 0.5 and abs(p[1]) <= 0.5

    outside = [(0.0, 0.0), (1.0, 0.0), (1.2, 0.2), (5.0, 0.2)]  # first bend at x = 1: outside
    assert pad_entry_ends(outside, in_pad) == (False, False)
    inside = [(0.0, 0.0), (0.3, 0.0), (0.5, 0.2), (5.0, 0.2)]  # first bend at x = 0.3: inside
    assert pad_entry_ends(inside, in_pad) == (True, False)
    straight = [(0.0, 0.0), (5.0, 0.0)]  # no bend: plain end-in-pad flags
    assert pad_entry_ends(straight, in_pad) == (True, False)


def test_widen_to_class_width() -> None:
    narrow = track("N1", start=(10.0, 10.0), end=(15.0, 10.0), width=0.15, kiid="n")
    ok = track("N1", start=(15.0, 10.0), end=(20.0, 10.0), width=0.2, kiid="ok")
    m = _board_ab((narrow, ok))
    chamfered = track("N1", start=(1.0, 1.0), end=(2.0, 1.0), width=0.15, kiid="")
    out, n = _widen(m, [("x", (chamfered,))], frozenset({"n", "ok"}))
    assert n == 2
    assert out[0] == ("x", (chamfered.__class__(**{**chamfered.__dict__, "width": 0.2}),))
    assert out[1][0] == "n" and out[1][1][0].width == 0.2 and out[1][1][0].kiid == ""
    assert all(old != "ok" for old, _ in out)


def test_rip_copper_only_new_unlocked() -> None:
    new = track("N1", start=(10.0, 10.0), end=(15.0, 10.0), kiid="new")
    old = track("N1", start=(15.0, 10.0), end=(20.0, 10.0), kiid="old")
    stray = via("N1", at=(12.0, 10.0), kiid="v-stray")  # no track end reaches it afterwards
    kept = via("N1", at=(15.0, 10.0), kiid="v-kept")  # the old track still ends here
    m = _board_ab((new, old), vias=(stray, kept))
    gone, ripped = _rip_copper(m, ["N1:R1.1-R2.1"], frozenset({"old"}))
    assert gone == ["new", "v-stray"]
    assert ripped == {"N1:R1.1-R2.1"}
    locked = track("N1", start=(10.0, 10.0), end=(20.0, 10.0), kiid="lk", locked=True)
    gone, ripped = _rip_copper(_board_ab((locked,)), ["N1:R1.1-R2.1"], frozenset())
    assert (gone, ripped) == ([], set())


def test_redundant_new_copper() -> None:
    old = track("N1", start=(10.0, 10.0), end=(20.0, 10.0), kiid="old")
    dup = track("N1", start=(10.0, 10.0), end=(20.0, 10.0), layer="B.Cu", kiid="dup")
    m = _board_ab((old, dup))
    assert _redundant(m, frozenset({"old"}), frozenset()) == ["dup"]
    # needed new copper (the old one is gone) is kept
    m2 = _board_ab((dup,))
    assert _redundant(m2, frozenset(), frozenset()) == []


def test_worst_areas_cells() -> None:
    a, b = _two_pads()
    pads = {"k1": (a, b), "k2": (a, b), "k3": (b, b)}
    out = _worst_areas(["k1", "k2", "k3", "missing"], pads)
    assert out[0] == {"x": 10.0, "y": 10.0, "w": 10.0, "h": 10.0, "count": 2}
    assert len(out) == 2


def test_dsn_disable_layers_by_position() -> None:
    renamed = DSN.replace("In1.Cu", '"GND plane"')
    out = disable_layers(renamed, ["In1.Cu"], ("F.Cu", "In1.Cu"))
    assert '(autoroute_settings (layer_rule "GND plane" (active off)))' in out
    with pytest.raises(ValueError):
        disable_layers(renamed, ["In1.Cu"], ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))


def test_plan_fill_treats_plane_zone_nets_as_plane_nets() -> None:
    """A net whose copper zone covers >= 80 % of the outline (a supply plane, P7) is a plane
    net: its connections are not open for the fill and its new copper is deleted like
    ground's. A small local pour does not change anything."""
    m = _board_ab((), net="+3V3", copper_layers=FOUR)
    plan = plan_fill(m, classify(m, BoardDoc()), None)
    assert [k for k, *_ in plan.open] == ["+3V3:R1.1-R2.1"]
    small = _board_ab(
        (), net="+3V3", copper_layers=FOUR, zones=(zone(net="+3V3", layers=("In2.Cu",)),)
    )
    plan = plan_fill(small, classify(small, BoardDoc()), None)
    assert [k for k, *_ in plan.open] == ["+3V3:R1.1-R2.1"]
    assert "+3V3" not in plan.ground_nets
    full = ((0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0))
    poured = _board_ab(
        (),
        net="+3V3",
        copper_layers=FOUR,
        outline=outline(0, 0, 100, 80),
        zones=(zone(net="+3V3", layers=("In2.Cu",), poly=full),),
    )
    plan = plan_fill(poured, classify(poured, BoardDoc()), None)
    assert plan.open == ()
    assert "+3V3" in plan.ground_nets
    # the supply plane's layer stays free too; In1.Cu (D1 default) while no ground plane
    assert plan.plane_layers == ("In1.Cu", "In2.Cu")


def test_plane_coverage_summed_per_net_and_layer() -> None:
    """A split plane (two halves of one net on one layer) counts like one plane; with a
    ground plane present the D1 default is not added."""
    left = ((0.0, 0.0), (50.0, 0.0), (50.0, 80.0), (0.0, 80.0))
    right = ((50.0, 0.0), (100.0, 0.0), (100.0, 80.0), (50.0, 80.0))
    zones = (
        zone(net="GND", layers=("In2.Cu",), poly=left, kiid="z1"),
        zone(net="GND", layers=("In2.Cu",), poly=right, kiid="z2"),
    )
    m = board(copper_layers=FOUR, outline=outline(0, 0, 100, 80), zones=zones)
    assert default_plane_layers(m, frozenset({"GND"})) == ("In2.Cu",)


def test_outer_pours_are_never_plane_layers() -> None:
    """P7's default stack pours GND on F.Cu and B.Cu over the whole outline too: outer
    layers never become plane layers (2 layers: none; 4 layers: In1.Cu)."""
    full = ((0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0))
    outer = tuple(zone(net="GND", layers=(n,), poly=full, kiid=f"z-{n}") for n in ("F.Cu", "B.Cu"))
    m2 = board(outline=outline(0, 0, 100, 80), zones=outer)
    assert default_plane_layers(m2, frozenset({"GND"})) == ()
    stack = (*outer, zone(net="GND", layers=("In1.Cu",), poly=full, kiid="z-in1"))
    m4 = board(copper_layers=FOUR, outline=outline(0, 0, 100, 80), zones=stack)
    assert default_plane_layers(m4, frozenset({"GND"})) == ("In1.Cu",)
    # only outer pours: no ground plane yet -> the D1 default
    m4o = board(copper_layers=FOUR, outline=outline(0, 0, 100, 80), zones=outer)
    assert default_plane_layers(m4o, frozenset({"GND"})) == ("In1.Cu",)
