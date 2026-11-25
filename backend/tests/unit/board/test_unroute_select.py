"""`unroute_ops.select_copper` / `attached` (SP4 P4 Task 4): seeds extended to whole
connection components; locked components skipped unless asked; zones never selected."""

from __future__ import annotations

import time

from copperchat.board.model import Rect
from copperchat.kicad_tools.board.unroute_ops import attached, select_copper

from .builders import track, via, zone
from .builders_circuit import cap, circuit, ic


def _board(**kw: object):  # type: ignore[no-untyped-def]
    u1 = ic("U1", {"1": "SIG", "2": "GND"}, (10.0, 10.0))  # pads (10,10) SIG, (11,10) GND
    c1 = cap("C1", "SIG", "GND", (30.0, 10.0))  # pads (29.5,10) SIG, (30.5,10) GND
    r = cap("C2", "OTHER", "GND", (30.0, 30.0))
    return circuit(u1, c1, r, **kw)


# U1.1 -> (15,10) -> via (15,10) -> (29.5,10) C1.1, all F.Cu (joined only by the via)
SIG = (
    track("SIG", "F.Cu", (10.0, 10.0), (15.0, 10.0), kiid="t1"),
    track("SIG", "F.Cu", (15.0, 10.0), (29.5, 10.0), kiid="t2"),
)
SIG_VIA = via("SIG", (15.0, 10.0), kiid="v1")
# an unrelated OTHER net track and a separate SIG stub that touches no seed
OTHER = track("OTHER", "F.Cu", (29.5, 30.0), (20.0, 30.0), kiid="t3")
STUB = track("SIG", "F.Cu", (29.7, 10.0), (29.7, 5.0), kiid="t4")  # in C1.1, not on t2


def test_component_extends_across_via() -> None:
    m = _board(tracks=(*SIG, OTHER), vias=(SIG_VIA,))
    sel = select_copper(m, pads_of=["U1"])
    assert sel.track_kiids == {"t1", "t2"}
    assert sel.via_kiids == {"v1"}
    assert sel.nets == ("SIG",)
    assert sel.locked == 0


def test_pads_are_terminals() -> None:
    """The stub on C1's pad belongs to another component: moving U1 leaves it."""
    m = _board(tracks=(*SIG, STUB), vias=(SIG_VIA,))
    assert select_copper(m, pads_of=["U1"]).track_kiids == {"t1", "t2"}
    assert select_copper(m, pads_of=["C1"]).track_kiids == {"t1", "t2", "t4"}


def test_locked_component_skipped() -> None:
    locked = (SIG[0], track("SIG", "F.Cu", (15.0, 10.0), (29.5, 10.0), kiid="t2", locked=True))
    m = _board(tracks=(*locked, OTHER), vias=(SIG_VIA,))
    sel = select_copper(m, pads_of=["U1"])
    assert sel.empty and sel.locked == 1 and sel.nets == ()
    sel = select_copper(m, pads_of=["U1"], include_locked=True)
    assert sel.track_kiids == {"t1", "t2"} and sel.via_kiids == {"v1"}


def test_region_seed_extends_to_whole_connection() -> None:
    m = _board(tracks=(*SIG, OTHER), vias=(SIG_VIA,))
    sel = select_copper(m, region=Rect(25.0, 8.0, 2.0, 4.0))  # touches t2 only
    assert sel.track_kiids == {"t1", "t2"} and sel.via_kiids == {"v1"}
    assert select_copper(m, region=Rect(0.0, 50.0, 5.0, 5.0)).empty


def test_nets_seed() -> None:
    m = _board(tracks=(*SIG, OTHER, STUB), vias=(SIG_VIA,))
    sel = select_copper(m, nets=["OTHER"])
    assert sel.track_kiids == {"t3"} and sel.nets == ("OTHER",)


def test_zone_never_selected() -> None:
    z = zone("GNDPLANE", "SIG", ("F.Cu",), kiid="z1")
    m = _board(tracks=SIG, vias=(SIG_VIA,), zones=(z,))
    sel = select_copper(m, nets=["SIG"], region=Rect(0.0, 0.0, 50.0, 50.0))
    assert "z1" not in sel.track_kiids | sel.via_kiids
    assert sel.track_kiids == {"t1", "t2"}


def test_t_junction_joins() -> None:
    branch = track("SIG", "F.Cu", (20.0, 10.0), (20.0, 20.0), kiid="t5")  # ends on t2
    m = _board(tracks=(*SIG, branch), vias=(SIG_VIA,))
    assert select_copper(m, pads_of=["U1"]).track_kiids == {"t1", "t2", "t5"}


def test_attached() -> None:
    fanout = (track("GND", "F.Cu", (11.0, 10.0), (11.0, 10.6), kiid="g1"),)
    gnd_via = via("GND", (11.0, 10.6), kiid="g2")
    m = _board(tracks=SIG + fanout + (OTHER,), vias=(SIG_VIA, gnd_via))
    got = attached(m, ["U1", "C1", "C2"])
    assert got == {"C1": ("SIG",), "C2": ("OTHER",), "U1": ("GND", "SIG")}
    sel = select_copper(m, pads_of=["U1"], include_locked=True)
    assert sel.track_kiids == {"t1", "t2", "g1"} and sel.via_kiids == {"v1", "g2"}
    assert sel.nets == ("GND", "SIG")


def test_layer_matters() -> None:
    """A B.Cu track end on an F.Cu-only SMD pad is not attached."""
    m = _board(tracks=(track("SIG", "B.Cu", (10.0, 10.0), (12.0, 12.0), kiid="b1"),))
    assert attached(m, ["U1"]) == {}


def test_via_in_the_middle_of_a_track_joins() -> None:
    """A via on a same-net track (not at its end) joins it: the B.Cu leg starting in the
    via belongs to the U1 → C1 connection."""
    through = track("SIG", "F.Cu", (10.0, 10.0), (29.5, 10.0), kiid="t1")
    leg = track("SIG", "B.Cu", (15.0, 10.0), (15.0, 20.0), kiid="b1")
    m = _board(tracks=(through, leg), vias=(SIG_VIA,))
    sel = select_copper(m, pads_of=["U1"])
    assert sel.track_kiids == {"t1", "b1"} and sel.via_kiids == {"v1"}
    # the via off the track's centre line (beyond half the width) does not join it
    off = via("SIG", (15.0, 10.5), kiid="v2")
    leg2 = track("SIG", "B.Cu", (15.0, 10.5), (15.0, 20.0), kiid="b2")
    m = _board(tracks=(through, leg2), vias=(off,))
    sel = select_copper(m, pads_of=["U1"])
    assert sel.track_kiids == {"t1"} and sel.via_kiids == set()


def test_select_large_board_is_fast() -> None:
    """10k tracks on 500 nets, 692 pads: pads seed through the spatial buckets, not by
    testing every item against every pad (was ~9 s)."""
    parts = [
        cap(f"C{i}", f"N{i % 500}", "GND", (2.0 + (i % 40) * 5.0, 2.0 + (i // 40) * 5.0))
        for i in range(346)
    ]
    tracks = [
        track(
            f"N{n}",
            "F.Cu",
            (k * 2.0, 100.0 + n * 0.5),
            ((k + 1) * 2.0, 100.0 + n * 0.5),
            kiid=f"t{n}-{k}",
        )
        for n in range(500)
        for k in range(20)
    ]
    m = circuit(*parts, tracks=tuple(tracks))
    assert sum(len(p.pads) for p in m.parts) == 692
    t0 = time.perf_counter()
    sel = select_copper(m, pads_of=[p.ref for p in m.parts], include_locked=True)
    assert time.perf_counter() - t0 < 2.0
    assert sel.empty  # no track touches a pad
    t0 = time.perf_counter()
    sel = select_copper(m, nets=["N7"])
    assert time.perf_counter() - t0 < 2.0
    assert len(sel.track_kiids) == 20
