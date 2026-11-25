"""`pcb_status`'s pure summary against hand-counted builder boards (review round 4)."""

from __future__ import annotations

from copperchat.board.model import Pad, board_hash, placement_hash
from copperchat.kicad_tools.board.status import MAX_UNPLACED_REFS, connection_count, status_data
from tests.unit.board.builders import board, part, zone


def _pad(ref: str, number: str, net: str) -> Pad:
    return Pad(ref, number, net, 0.0, 0.0, 1.0, 1.0, 0.0, "rect", frozenset({"F.Cu"}), True, None)


def test_connection_count_by_hand() -> None:
    # net A: 3 pads -> 2 connections; net B: 2 pads -> 1; net C: 1 pad -> 0; netless -> 0
    m = board(
        parts=(
            part("R1", pads=(_pad("R1", "1", "A"), _pad("R1", "2", "B"))),
            part("R2", pads=(_pad("R2", "1", "A"), _pad("R2", "2", "C"))),
            part("R3", pads=(_pad("R3", "1", "A"), _pad("R3", "2", "B"), _pad("R3", "3", ""))),
        )
    )
    assert connection_count(m) == 3


def test_status_counts_by_hand() -> None:
    inside = part("R1", 50.0, 40.0, pads=(_pad("R1", "1", "A"), _pad("R1", "2", "A")))
    outside = part("R2", 500.0, 40.0)
    no_courtyard = part("R3", 50.0, 40.0, courtyard=())
    m = board(
        parts=(inside, outside, no_courtyard),
        zones=(zone(kiid="z1"), zone(kiid="z2", rule_area=True, keepout=frozenset({"tracks"}))),
    )
    data = status_data(m, unrouted=0)
    assert data["exists"] is True
    assert data["footprints"] == 3
    assert (data["placed"], data["unplaced"]) == (1, 2)
    assert data["unplaced_refs"] == ["R2", "R3"]
    assert data["unplaced_omitted"] == 0
    assert (data["connections"], data["routed"], data["unrouted"]) == (1, 1, 0)
    assert data["zones"] == 1  # the rule area is not a copper zone
    assert data["outline"] == {
        "kind": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 100.0,
        "h": 80.0,
        "corner_radius": 0.0,
    }
    assert data["placement_hash"] == placement_hash(m)
    assert data["board_hash"] == board_hash(m)


def test_unplaced_refs_natural_order_and_truncation() -> None:
    n = MAX_UNPLACED_REFS + 7
    m = board(parts=tuple(part(f"R{i}", 900.0, 900.0) for i in range(n, 0, -1)))
    data = status_data(m, unrouted=0)
    assert data["unplaced"] == n
    assert data["unplaced_refs"] == [f"R{i}" for i in range(1, MAX_UNPLACED_REFS + 1)]
    assert data["unplaced_omitted"] == 7


def test_routed_clamped_at_zero() -> None:
    m = board(parts=(part("R1", pads=(_pad("R1", "1", "A"), _pad("R1", "2", "A"))),))
    data = status_data(m, unrouted=5)
    assert (data["connections"], data["routed"], data["unrouted"]) == (1, 0, 5)
