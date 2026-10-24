"""World.sheet_pin: '<sheet name>.<pin name>' when the sheet symbol sits in a parent file
used by two instances (one item for sch_move / sch_delete). Synthetic world (no KiCad)."""

from __future__ import annotations

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import PinEnd
from copperchat.kicad_tools.edit.labels import World
from copperchat.kicad_tools.edit.scene import SHEET_PIN, Scene, SheetPinInfo

ROOT = SheetRef("/r", "1", "top.kicad_sch", "", "/")
AMP_A = SheetRef("/r/a", "2", "amp.kicad_sch", "AmpA", "/AmpA/")
AMP_B = SheetRef("/r/b", "3", "amp.kicad_sch", "AmpB", "/AmpB/")
# the sheet symbol 's' (named 'Bias') inside amp.kicad_sch, seen in both instances
BIAS_A = SheetRef("/r/a/s", "4", "bias.kicad_sch", "Bias", "/AmpA/Bias/")
BIAS_B = SheetRef("/r/b/s", "5", "bias.kicad_sch", "Bias", "/AmpB/Bias/")


def _world() -> World:
    w = object.__new__(World)
    w.sheets = {r.path: r for r in (ROOT, AMP_A, AMP_B, BIAS_A, BIAS_B)}
    w._planned = {}
    w._scenes = {}
    for parent in (AMP_A, AMP_B):
        sc = Scene(sheet=parent)
        pin = PinEnd((parent.path, "s", "VB"), "Bias", "VB", "VB", SHEET_PIN, (10, 10), "left")
        sc.sheet_pins["p1"] = SheetPinInfo("p1", "s", f"{parent.path}/s", pin)
        w._scenes[parent.path] = sc
    return w


def test_shared_parent_is_one_item_for_targets() -> None:
    w = _world()
    pin = w.sheet_pin("Bias.VB", one_item=True)
    assert pin is not None and pin.key == ("/r/a", "s", "VB")
    info = w.sheet_pin_info(pin.key)
    assert info is not None and info.uuid == "p1"


def test_shared_parent_stays_ambiguous_for_connect_and_names_the_uuid_form() -> None:
    with pytest.raises(StructuredToolError) as exc:
        _world().sheet_pin("Bias.VB")
    assert exc.value.code == "ambiguous_pin"
    assert "UUID" in exc.value.suggestion


def test_lookup_past_the_deadline_says_so() -> None:
    w = _world()
    w.deadline = 0.0
    with pytest.raises(StructuredToolError) as exc:
        w.check_time("sch_move", lookup=True)
    assert exc.value.code == "timeout" and "looking up" in exc.value.what
