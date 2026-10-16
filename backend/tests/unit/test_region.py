import pytest

from copperchat.errors import StructuredToolError
from copperchat.views.region import Region, resolve_region

SNAP = {
    "footprints": [
        {"ref": "U1", "bbox_mm": {"x": 10, "y": 20, "w": 4, "h": 6}},
        {"ref": "R1", "bbox_mm": {"x": 0, "y": 0, "w": 1, "h": 1}},
    ]
}


def test_box_and_around_ref() -> None:
    assert resolve_region(None, SNAP) is None
    assert resolve_region(Region(x_mm=1, y_mm=2, w_mm=3, h_mm=4), SNAP) == (1, 2, 3, 4)
    assert resolve_region(Region(around_ref="U1", margin_mm=5), SNAP) == (5, 15, 14, 16)


def test_symbols_for_the_schematic() -> None:
    snap = {"symbols": [{"ref": "C1", "bbox_mm": {"x": 1, "y": 1, "w": 0, "h": 2}}]}
    assert resolve_region(Region(around_ref="C1", margin_mm=1), snap) == (0, 0, 2, 4)
    with pytest.raises(StructuredToolError) as info:
        resolve_region(Region(around_ref="C2"), snap)
    assert info.value.what == "Symbol C2 not found"


def test_bad_regions() -> None:
    with pytest.raises(StructuredToolError) as info:
        resolve_region(Region(around_ref="U9"), SNAP)
    assert "U1" in info.value.suggestion
    with pytest.raises(StructuredToolError):
        resolve_region(Region(x_mm=1, y_mm=2), SNAP)
    with pytest.raises(StructuredToolError):
        resolve_region(Region(x_mm=1, y_mm=2, w_mm=0, h_mm=3), SNAP)
    with pytest.raises(StructuredToolError):
        resolve_region(Region(around_ref="U1", margin_mm=-1), SNAP)
