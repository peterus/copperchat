from types import SimpleNamespace

from copperchat.kicad_tools.filtering import matches, not_found
from copperchat.kicad_tools.units import mm_to_nm, nm_to_mm, pos_mm


def test_units() -> None:
    assert nm_to_mm(1_000_000) == 1.0
    assert nm_to_mm(1_234_567) == 1.2346
    assert mm_to_nm(2.54) == 2_540_000
    assert pos_mm(SimpleNamespace(x=-500_000, y=3_000_000)) == {"x": -0.5, "y": 3.0}


def test_glob_filter_any_field_case_insensitive() -> None:
    assert matches(None, "R1")
    assert matches("", "R1")
    assert matches("r*", "R12", "10k")
    assert matches("*0805*", "C3", "100n", "Capacitor_SMD:C_0805_2012Metric")
    assert not matches("U?", "R1", "U12")
    assert matches("U?", "U1")


def test_not_found_suggests_close_matches() -> None:
    err = not_found("Symbol", "R99", ["R1", "R9", "C1", "U1"])
    d = err.to_dict()
    assert d["what"] == "Symbol R99 not found"
    assert "R9" in d["suggestion"]
    empty = not_found("Net", "XYZ", ["GND"]).to_dict()
    assert "list" in empty["suggestion"].lower()
