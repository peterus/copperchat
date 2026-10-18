"""World bookkeeping of E5 planning for shared sheet files (no KiCad)."""

from __future__ import annotations

import itertools

import pytest

from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit import labels
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.scene import Scene

ROOT = SheetRef("/r", "1", "top.kicad_sch", "", "/")
A = SheetRef("/r/a", "2", "amp.kicad_sch", "a", "/a/")
B = SheetRef("/r/b", "3", "amp.kicad_sch", "b", "/b/")


def _world(monkeypatch: pytest.MonkeyPatch, known: set[tuple[str, str, str]]) -> labels.World:
    monkeypatch.setattr(labels, "read_scene", lambda _h, sheet: Scene(sheet=sheet))
    w = object.__new__(labels.World)
    w.h = None
    w.sheets = {r.path: r for r in (ROOT, A, B)}
    w.netlist = Netlist({})
    w.item_nets = {}
    w.known = known
    w._scenes = {}
    w._planned = {}
    w._pin_nets = {}
    w._sheet_symbols = {}
    w._fake = itertools.count()
    return w


def test_planned_items_reach_every_instance_of_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world(monkeypatch, set())
    scene_a = w.scene(A)  # loaded before planning
    w.add_label(A, "SIG", (10.16, 10.16), "label", (1, 0), "/a/SIG")
    w.add_wire(A, (10.16, 10.16), (10.16, 12.7), "/a/SIG")
    assert len(scene_a.labels) == 1 and len(scene_a.wires) == 1
    scene_b = w.scene(B)  # loaded after planning: the items are replayed
    assert [lb.text for lb in scene_b.labels.values()] == ["SIG"]
    assert len(scene_b.wires) == 1
    assert w.scene(ROOT).labels == {}  # another file
    (uid,) = scene_b.labels
    assert w.nets(A)[uid] == "/a/SIG" and uid not in w.nets(B)  # net known only in A


def test_replicate_moves_on_sheet_keys_and_keeps_off_sheet_ones_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _world(monkeypatch, {("/r/b", "u1", "1")})
    pin, rep = ("/r/a", "u1", "1"), ("/r", "j1", "1")
    groups = w.replicate([pin, rep], A)
    assert groups == [frozenset({pin, rep}), frozenset({("/r/b", "u1", "1")})]
    # a symbol without pins in instance B (placed through A) contributes nothing there
    assert w.replicate([("/r/a", "u2", "1")], A) == [frozenset({("/r/a", "u2", "1")}), frozenset()]
