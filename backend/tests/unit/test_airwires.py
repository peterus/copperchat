from typing import Any

from copperchat.kicad_tools.pcb import _airwires


def pad(uuid: str, x: float, y: float) -> dict[str, Any]:
    return {"uuid": uuid, "position_mm": {"x": x, "y": y}}


def test_each_island_joins_the_nearest_earlier_pad() -> None:
    clusters = [[pad("a", 0, 0), pad("b", 10, 0)], [pad("c", 11, 0)], [pad("d", 0, 5)]]
    wires = _airwires("N", clusters)
    assert [(w["from_pad"], w["to_pad"]) for w in wires] == [("c", "b"), ("d", "a")]
    assert all(w["net"] == "N" for w in wires)
