"""`verify.check_expect` (v0.5.0 #4, spec §7): item-level groups, exactly one moved group
joins the declared net, nothing else changes."""

from __future__ import annotations

from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit.changeset import ExpectNet, NetIntent
from copperchat.kicad_tools.edit.verify import check_expect

P = "/"


def pin(sym: str, n: str) -> tuple[str, str, str]:
    return (P, sym, n)


def item(uid: str) -> tuple[str, str]:
    return (P, uid)


def intent(net: str, *moved: tuple[str, ...]) -> NetIntent:
    return NetIntent(expect=ExpectNet(net, frozenset(moved)))


BEFORE = Netlist(
    {"/SDA": frozenset({pin("R1", "1"), pin("U1", "5")}), "/X": frozenset({pin("R2", "1")})}
)
BEFORE_ITEMS = {item("w1"): "/SDA", item("w2"): "/X", item("lbl"): "SIG"}  # a floating label


def test_floating_label_onto_wire_passes() -> None:
    after_items = {item("w1"): "SIG", item("w2"): "/X", item("lbl"): "SIG"}  # takes its name
    after = Netlist({"SIG": BEFORE.nets["/SDA"], "/X": BEFORE.nets["/X"]})
    assert check_expect(BEFORE, BEFORE_ITEMS, after, after_items, intent("/SDA", item("lbl"))) == []


def test_no_connection_refused() -> None:
    problems = check_expect(BEFORE, BEFORE_ITEMS, BEFORE, BEFORE_ITEMS, intent("/SDA", item("lbl")))
    assert problems and "would not connect to '/SDA'" in problems[0]


def test_wrong_net_refused_names_the_result() -> None:
    after_items = {item("w1"): "/SDA", item("w2"): "SIG", item("lbl"): "SIG"}
    after = Netlist({"/SDA": BEFORE.nets["/SDA"], "SIG": BEFORE.nets["/X"]})
    problems = check_expect(BEFORE, BEFORE_ITEMS, after, after_items, intent("/SDA", item("lbl")))
    assert any("would not connect to '/SDA'" in p for p in problems), problems
    assert any("'/X'" in p and "would also connect to 'SIG'" in p for p in problems), problems


def test_two_pins_onto_target_refused() -> None:
    """Review Focus 1: a part whose two pins (two nets) both land on the target net."""
    before = Netlist(
        {
            "/SDA": frozenset({pin("U1", "5")}),
            "/A": frozenset({pin("R1", "1")}),
            "/B": frozenset({pin("R1", "2")}),
        }
    )
    after = Netlist({"/SDA": frozenset({pin("U1", "5"), pin("R1", "1"), pin("R1", "2")})})
    problems = check_expect(before, {}, after, {}, intent("/SDA", pin("R1", "1"), pin("R1", "2")))
    assert problems and "not one" in problems[0], problems


def test_merge_of_a_group_not_moved_refused() -> None:
    after = Netlist({"/SDA": BEFORE.nets["/SDA"] | BEFORE.nets["/X"]})
    after_items = {item("w1"): "/SDA", item("w2"): "/SDA", item("lbl"): "SIG"}
    problems = check_expect(BEFORE, BEFORE_ITEMS, after, after_items, intent("/SDA", item("lbl")))
    assert problems and "not a net of the moved items" in problems[0], problems


def test_split_elsewhere_refused() -> None:
    before = Netlist(
        {"/SDA": frozenset({pin("U1", "5")}), "/C": frozenset({pin("C1", "1"), pin("C2", "1")})}
    )
    before_items = {item("lbl"): "L"}
    after = Netlist(
        {
            "L": frozenset({pin("U1", "5")}),
            "/C": frozenset({pin("C1", "1")}),
            "Net-(C2)": frozenset({pin("C2", "1")}),
        }
    )
    problems = check_expect(
        before, before_items, after, {item("lbl"): "L"}, intent("/SDA", item("lbl"))
    )
    assert any("'/C'" in p and "split" in p for p in problems), problems


def test_items_only_on_one_side_are_left_out() -> None:
    """A dragged wire replaced by new segments (new UUIDs) does not count."""
    after_items = {item("w-new"): "SIG", item("w2"): "/X", item("lbl"): "SIG"}
    after = Netlist({"SIG": BEFORE.nets["/SDA"], "/X": BEFORE.nets["/X"]})
    assert check_expect(BEFORE, BEFORE_ITEMS, after, after_items, intent("/SDA", item("lbl"))) == []


def test_moved_item_unlisted_before_counts_as_its_own_group() -> None:
    """KiCad may leave a lone label out of the netlist: it still is a moved group."""
    before_items = {item("w1"): "/SDA", item("w2"): "/X"}
    after_items = {item("w1"): "/SDA", item("w2"): "/X", item("lbl"): "/SDA"}
    assert (
        check_expect(BEFORE, before_items, BEFORE, after_items, intent("/SDA", item("lbl"))) == []
    )


def test_split_of_the_joined_net_refused() -> None:
    """Only part of the moved net joins the target, the rest is cut off."""
    before = Netlist(
        {"/SDA": frozenset({pin("U1", "5")}), "/A": frozenset({pin("R1", "1"), pin("R2", "1")})}
    )
    after = Netlist(
        {
            "/SDA": frozenset({pin("U1", "5"), pin("R1", "1")}),
            "Net-(R2)": frozenset({pin("R2", "1")}),
        }
    )
    problems = check_expect(before, {}, after, {}, intent("/SDA", pin("R1", "1")))
    assert any("'/A'" in p and "split" in p for p in problems), problems


def test_two_unlisted_pins_of_one_part_stay_separate_groups() -> None:
    before = Netlist({"/SDA": frozenset({pin("U1", "5")})})
    after = Netlist({"/SDA": frozenset({pin("U1", "5"), pin("R1", "1"), pin("R1", "2")})})
    problems = check_expect(before, {}, after, {}, intent("/SDA", pin("R1", "1"), pin("R1", "2")))
    assert problems and "not one" in problems[0], problems
