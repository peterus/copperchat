"""Create before/after pairs by editing COPIES of fixtures through the KiCad IPC API only.

pairs/<name>/before   = pristine fixture copy (what a snapshot copy is)
pairs/<name>/after    = the same copy edited via kipy + sch.save()
Also writes pairs/<name>/changelog.json = what a tool-recorded change log would hold
(uuid, ref, op, kind) so q_diff.py can cross-check the diff JSON against it.
"""
import json, sys, time
from common import *
from kipy.geometry import Vector2
from kipy.schematic_types import LocalLabel, SchematicLine, Junction, SchematicSymbolOrientation

PAIRS = Path(os.environ.get("PAIRS", "/tmp/c2/pairs"))
V = Vector2.from_xy
mm = lambda v: int(round(v * 1e6))


def new_line(a, b):
    l = SchematicLine(); l.start = V(*a); l.end = V(*b); return l


def pair(name, fixture):
    d = PAIRS / name
    copy_fixture(fixture, d / "before")
    copy_fixture(fixture, d / "after")
    return d


def control_noedit():
    d = pair("noedit", "ecc83")
    with Server(d / "after") as s:
        s.sch.save()
    json.dump([], open(d / "changelog.json", "w"))


def ecc83_edit():
    d = pair("ecc83_edit", "ecc83")
    log = []
    with Server(d / "after") as s:
        sch = s.sch
        syms = {x.reference: x for x in sch.get_symbols()}
        # 1 modify value
        c1 = syms["C1"]; c1.value = "22uF"
        sch.update_items([c1]); log.append(dict(op="modify", kind="symbol", ref="C1", uuid=str(c1.id.value), field="Value"))
        # 2 move
        r = next(x for x in syms.values() if x.reference.startswith("R") and not x.reference.startswith("#"))
        old = r.position; r.position = V(old.x + mm(5.08), old.y)
        sch.update_items([r]); log.append(dict(op="modify", kind="symbol", ref=r.reference, uuid=str(r.id.value), field="position"))
        # 3 add wire, label, junction
        w = sch.create_items([new_line((mm(200), mm(100)), (mm(210), mm(100)))])[0]
        log.append(dict(op="add", kind="wire", uuid=str(w.id.value)))
        lab = LocalLabel(); lab.position = V(mm(200), mm(100)); lab.text.value = "SPIKE_NET"
        lab = sch.create_items([lab])[0]; log.append(dict(op="add", kind="label", uuid=str(lab.id.value)))
        j = Junction(); j.position = V(mm(205), mm(100))
        j = sch.create_items([j])[0]; log.append(dict(op="add", kind="junction", uuid=str(j.id.value)))
        # 4 delete a wire
        wires = sch.get_lines()
        victim = wires[0]; log.append(dict(op="delete", kind="wire", uuid=str(victim.id.value)))
        sch.remove_items([victim])
        # 5 place new symbol from stock library
        p = sch.place_symbol_from_library("Device:R", V(mm(220), mm(120)), reference="R99")
        log.append(dict(op="add", kind="symbol", ref="R99", uuid=str(p.id.value)))
        sch.save()
    json.dump(log, open(d / "changelog.json", "w"), indent=1)


def hier_edit():
    d = pair("hier_edit", "complex_hierarchy")
    log = []
    with Server(d / "after") as s:
        sch = s.sch
        insts = {i.name: i for i in sch.get_hierarchy()}
        # NB: update_items on the unbound Schematic silently loses edits on sub-sheets
        # (see explore2.py / README); bind to the sheet instance with for_sheet().
        vert = sch.for_sheet(insts["ampli_ht_vertical"])
        t = next(x for x in vert.get_symbols() if x.reference == "R201")
        t.value = "SPIKE"
        vert.update_items([t]); log.append(dict(op="modify", kind="symbol", ref="R201", uuid=str(t.id.value), path=str(t.path), field="Value"))
        # a wire + label added on the sub-sheet (shared file, instanced twice)
        w = vert.create_items([new_line((mm(100), mm(100)), (mm(110), mm(100)))])[0]
        log.append(dict(op="add", kind="wire", uuid=str(w.id.value), path=str(insts["ampli_ht_vertical"].path)))
        lab = LocalLabel(); lab.position = V(mm(100), mm(100)); lab.text.value = "SUBNET"
        lab = vert.create_items([lab])[0]; log.append(dict(op="add", kind="label", uuid=str(lab.id.value), path=str(insts["ampli_ht_vertical"].path)))
        # root sheet edit
        t2 = next(x for x in sch.for_sheet(insts["complex_hierarchy"]).get_symbols() if x.reference == "C105")
        t2.value = "ROOTSPIKE"
        sch.for_sheet(insts["complex_hierarchy"]).update_items([t2])
        log.append(dict(op="modify", kind="symbol", ref="C105", uuid=str(t2.id.value), path=str(t2.path), field="Value"))
        sch.save()
    json.dump(log, open(d / "changelog.json", "w"), indent=1)


if __name__ == "__main__":
    for fn in (control_noedit, ecc83_edit, hier_edit):
        if len(sys.argv) < 2 or fn.__name__ in sys.argv[1:]:
            t = time.time(); fn(); print(fn.__name__, "done", round(time.time() - t, 1), "s")
