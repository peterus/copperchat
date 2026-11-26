"""P4 placer: clusters of a block (B-C11 `build_clusters`)."""

from __future__ import annotations

from copperchat.board.classify import classify
from copperchat.board.model import Part, Point
from copperchat.board.placer import Cluster, build_clusters

from .builders_circuit import cap, circuit, conn, ic, ind, res


def _buck(at: Point = (10.0, 10.0)) -> list[Part]:
    x, y = at
    pins = {"1": "VIN", "2": "GND", "3": "SW", "4": "BST", "5": "FB", "6": "VIN"}
    return [
        ic("U1", pins, at),
        cap("C1", "VIN", "GND", (x, y + 2)),
        cap("C2", "BST", "SW", (x + 3, y - 2)),
        ind("L1", "SW", "+3V3", (x + 3, y + 3)),
        cap("C3", "+3V3", "GND", (x + 5, y + 3)),
        res("R1", "+3V3", "FB", (x + 4, y + 5)),
        res("R2", "FB", "GND", (x + 4, y + 6)),
    ]


def _mcu(ref: str, at: Point, extra: str = "") -> Part:
    pins = {
        "1": "+3V3",
        "2": "GND",
        "3": f"A{extra}",
        "4": f"B{extra}",
        "5": f"C{extra}",
        "6": f"D{extra}",
        "7": "+3V3",
        "8": f"E{extra}",
    }
    return ic(ref, pins, at)


def _cluster_of(clusters: tuple[Cluster, ...], ref: str) -> Cluster:
    return next(c for c in clusters if ref in c.members)


def test_cluster_switcher_passives() -> None:
    parts = [*_buck(), _mcu("U2", (40.0, 40.0)), cap("C9", "+3V3", "GND", (47.0, 42.0))]
    m = circuit(*parts)
    cls = classify(m)
    assert cls.switchers and cls.switchers[0].ic == "U1"
    clusters = build_clusters(m, cls, [p.ref for p in parts])
    buck = _cluster_of(clusters, "U1")
    assert buck.anchor == "U1"
    assert buck.members[0] == "U1"
    # inductor, in/out caps, feedback divider and the bootstrap cap all follow the switcher
    assert {"L1", "C1", "C3", "R1", "R2", "C2"} <= set(buck.members)
    # C3 (+3V3 out cap) shares the rail with U2's decoupling but stays with the switcher
    assert "C3" not in _cluster_of(clusters, "U2").members
    # each part in exactly one cluster
    flat = [r for c in clusters for r in c.members]
    assert sorted(flat) == sorted(p.ref for p in parts)


def test_cluster_decoupling_follows_ic() -> None:
    u1 = ic("U1", {str(i): ("+3V3" if i in (1, 9) else f"S{i}") for i in range(1, 13)}, (10, 10))
    u2 = _mcu("U2", (40.0, 40.0))
    c1 = cap("C1", "+3V3", "GND", (47.0, 42.0))  # next to U2 pin 7
    c2 = cap("C2", "+3V3", "GND", (10.0, 12.0))  # next to U1 pin 1
    m = circuit(u1, u2, c1, c2)
    cls = classify(m)
    assert {(d.cap, d.ic) for d in cls.decoupling} == {("C1", "U2"), ("C2", "U1")}
    clusters = build_clusters(m, cls, ["C2", "U2", "C1", "U1"])
    assert clusters == (Cluster("U1", ("U1", "C2")), Cluster("U2", ("U2", "C1")))


def test_leftover_own_cluster() -> None:
    m = circuit(_mcu("U1", (40.0, 40.0)), res("R9", "X1", "X2", (10.0, 10.0)))
    clusters = build_clusters(m, classify(m), ["R9", "U1", "NOPE"])
    assert clusters == (Cluster("U1", ("U1",)), Cluster("R9", ("R9",)))


def test_small_anchor_joins_connector() -> None:
    jpins = {str(i): f"J{i}" for i in range(1, 17)}
    jpins.update({"1": "GND", "2": "VBUS", "6": "D+", "7": "D-", "16": "GND"})
    j1 = conn("J1", jpins, (10.0, 10.0), lib_id="Connector_USB:USB_C")
    esd = ic("U2", {"1": "D+", "2": "GND", "3": "D-", "4": "D-", "5": "VBUS", "6": "D+"}, (30, 10))
    mcu = ic("U1", {str(i): f"M{i}" for i in range(1, 21)}, (50.0, 40.0))
    m = circuit(j1, esd, mcu)
    cls = classify(m)
    clusters = build_clusters(m, cls, ["U1", "U2", "J1"])
    assert Cluster("J1", ("J1", "U2")) in clusters
    assert Cluster("U1", ("U1",)) in clusters
    assert len(clusters) == 2
