from __future__ import annotations

from copperchat.library.index import Index, SymbolRow


def _index(*rows: tuple[str, str, str, str]) -> Index:
    idx = Index()
    for lib_id, desc, kw, scope in rows:
        idx.add(lib_id, desc, kw, scope)  # type: ignore[arg-type]
    idx.finish()
    return idx


def test_prefix_and_keyword_match() -> None:
    idx = _index(
        ("Device:C", "Unpolarized capacitor", "cap capacitor", "global"),
        ("Device:R", "Resistor", "R res resistor", "global"),
        ("Device:R_Pack04", "4 resistor network, parallel topology", "R network", "global"),
        ("Device:Crystal", "Two pin crystal", "quartz ceramic resonator oscillator", "global"),
    )
    hits = [lib_id for lib_id, _ in idx.search("resist")]
    assert hits[0] == "Device:R"
    assert "Device:C" not in hits
    assert next(lib_id for lib_id, _ in idx.search("capacitor")) == "Device:C"


def test_all_terms_rank_above_partial_matches() -> None:
    idx = _index(
        ("Device:LED", "Light emitting diode", "LED diode", "global"),
        ("Device:D", "Diode", "diode", "global"),
        (
            "Device:LED_Small",
            "Light emitting diode, small symbol",
            "LED diode light-emitting",
            "global",
        ),
    )
    hits = [lib_id for lib_id, _ in idx.search("led diode")]
    assert hits[-1] == "Device:D"


def test_project_libraries_rank_first() -> None:
    idx = _index(
        ("Amplifier:OPA", "Op amp", "opamp", "global"),
        ("myproj:OPA", "Op amp", "opamp", "project"),
        ("Zeta:OPA", "Op amp", "opamp", "global"),
    )
    assert [lib_id for lib_id, _ in idx.search("opamp")] == [
        "myproj:OPA",
        "Amplifier:OPA",
        "Zeta:OPA",
    ]


def test_name_fallback_when_description_empty() -> None:
    idx = _index(
        ("ecc83-pp:ECC83", "", "", "project"),
        ("Valve:EL84", "Power pentode", "valve tube", "global"),
    )
    assert [lib_id for lib_id, _ in idx.search("ECC83")] == ["ecc83-pp:ECC83"]
    assert [lib_id for lib_id, _ in idx.search("ecc8")] == ["ecc83-pp:ECC83"]


def test_limit_and_library_filter() -> None:
    idx = _index(*[(f"Lib{i % 3}:R{i}", "Resistor", "resistor", "global") for i in range(30)])
    assert len(idx.search("resistor", limit=5)) == 5
    hits = idx.search("resistor", limit=50, library="Lib1")
    assert hits and all(lib_id.startswith("Lib1:") for lib_id, _ in hits)
    assert len(hits) == 10
    assert idx.search("resistor", library="Lib") == []  # nickname match is exact
    assert idx.search("", limit=5) == []
    assert idx.search("resistor", limit=0) == []


def test_exact_name_wins() -> None:
    idx = _index(
        (
            "Amplifier_Operational:LM358",
            "Low-Power, Dual Operational Amplifiers",
            "dual opamp",
            "global",
        ),
        (
            "Amplifier_Operational:LM358A",
            "Low-Power, Dual Operational Amplifiers",
            "dual opamp",
            "global",
        ),
        ("Amplifier_Operational:LM3580", "Something", "", "global"),
    )
    assert idx.search("LM358")[0][0] == "Amplifier_Operational:LM358"


def test_row_dict_shape() -> None:
    row = SymbolRow("Device:R", "Resistor", "R res", 1, 2, "", ["R_*"] * 8, "global")
    d = row.to_dict()
    assert set(d) == {
        "lib_id",
        "description",
        "keywords",
        "unit_count",
        "pin_count",
        "footprint",
        "footprint_filters",
        "scope",
    }
    assert d["footprint"] is None and len(d["footprint_filters"]) == 6
