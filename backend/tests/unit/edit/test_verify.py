"""verify.check against hand-made netlists (no KiCad)."""

from __future__ import annotations

from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit.changeset import NetIntent
from copperchat.kicad_tools.edit.verify import check, remap

R = "/root"


def k(sym: str, pin: str) -> tuple[str, str, str]:
    return (R, sym, pin)


BEFORE = Netlist(
    {
        "A": frozenset({k("r1", "1"), k("r2", "1")}),
        "B": frozenset({k("r3", "1")}),
        "C": frozenset({k("r4", "1"), k("r5", "1")}),
    }
)


def test_identical_netlist_passes() -> None:
    assert check(BEFORE, BEFORE, NetIntent()) == []


def test_expected_merge_passes_and_pulls_whole_nets() -> None:
    after = Netlist(
        {
            "A": frozenset({k("r1", "1"), k("r2", "1"), k("r3", "1")}),
            "C": frozenset({k("r4", "1"), k("r5", "1")}),
        }
    )
    assert check(BEFORE, after, NetIntent(merge=[frozenset({k("r2", "1"), k("r3", "1")})])) == []


def test_missing_merge_fails() -> None:
    problems = check(BEFORE, BEFORE, NetIntent(merge=[frozenset({k("r1", "1"), k("r3", "1")})]))
    assert problems and "one net" in problems[0]


def test_unexpected_short_fails_with_pin_labels() -> None:
    after = Netlist(
        {
            "A": frozenset({k("r1", "1"), k("r2", "1"), k("r4", "1"), k("r5", "1")}),
            "B": frozenset({k("r3", "1")}),
        }
    )
    problems = check(BEFORE, after, NetIntent(), label=lambda key: key[1].upper())
    assert any("now also connected to R4" in p for p in problems)


def test_may_change_and_names() -> None:
    after = Netlist(
        {
            "A": frozenset({k("r1", "1")}),
            "B": frozenset({k("r3", "1"), k("r2", "1")}),
            "C": frozenset({k("r4", "1"), k("r5", "1")}),
        }
    )
    assert check(BEFORE, after, NetIntent(may_change=frozenset({k("r2", "1")}))) == []
    names = NetIntent(may_change=frozenset({k("r2", "1")}), names={k("r2", "1"): "A"})
    assert check(BEFORE, after, names)


def test_new_symbol_pins_must_stay_unconnected_unless_intended() -> None:
    after = Netlist({**BEFORE.nets, "B": frozenset({k("r3", "1"), k("new", "1")})})
    assert check(BEFORE, after, NetIntent())
    assert check(BEFORE, after, NetIntent(merge=[frozenset({k("r3", "1"), k("new", "1")})])) == []


def test_deleted_symbol_pins_are_ignored_and_keep_partition() -> None:
    after = Netlist({"A": frozenset({k("r1", "1")}), "B": BEFORE.nets["B"], "C": BEFORE.nets["C"]})
    assert check(BEFORE, after, NetIntent()) == []
    assert check(BEFORE, after, NetIntent(keep_partition=True)) == []
    renamed = Netlist({"X": BEFORE.nets["A"], "Y": BEFORE.nets["B"], "Z": BEFORE.nets["C"]})
    assert check(BEFORE, renamed, NetIntent(keep_partition=True)) == []


def test_remap_renames_symbol_uuids() -> None:
    moved = Netlist({"A": frozenset({k("new", "1"), k("r2", "1")})})
    assert remap(moved, {"new": "r1"}).nets["A"] == frozenset({k("r1", "1"), k("r2", "1")})


def test_check_joined_items_and_pins() -> None:
    from copperchat.kicad_tools.edit.verify import check_joined

    items = {(R, "pin-a"): "A", (R, "wire-b"): "B", (R, "wire-a"): "A"}
    ok = NetIntent(joined=[frozenset({(R, "pin-a"), (R, "wire-a"), k("r1", "1")})])
    assert check_joined(BEFORE, items, ok) == []
    split = NetIntent(joined=[frozenset({(R, "pin-a"), (R, "wire-b")})])
    assert check_joined(BEFORE, items, split)  # two nets
    missing = NetIntent(joined=[frozenset({(R, "pin-a"), (R, "gone")})])
    assert check_joined(BEFORE, items, missing)  # an item on no net
    # a created segment KiCad merged away is left out of its group
    assert check_joined(BEFORE, items, missing, vanished={"gone"}) == []
