"""Which step causes the changes the tool did not ask for (junction removed, wire merged)?
Snapshot wires/junctions after each API op and after save(), keyed by uuid."""
import json
from common import *
from kipy.geometry import Vector2
from kipy.schematic_types import Junction
V = Vector2.from_xy
mm = lambda v: int(round(v * 1e6))
d = copy_fixture("ecc83", Path("/tmp/c2/col"))
def snap(sch):
    return {"wires": {str(l.id.value): [l.start.x, l.start.y, l.end.x, l.end.y] for l in sch.get_lines()},
            "junctions": {str(j.id.value): [j.position.x, j.position.y] for j in sch.get_junctions()}}
def delta(a, b):
    out = {}
    for k in a:
        out[k] = {"added": sorted(set(b[k]) - set(a[k])), "removed": sorted(set(a[k]) - set(b[k])),
                  "changed": sorted(u for u in set(a[k]) & set(b[k]) if a[k][u] != b[k][u])}
    return out
steps = []
with Server(d) as s:
    sch = s.sch
    cur = snap(sch)
    def record(name):
        global cur
        n = snap(sch); steps.append((name, delta(cur, n))); cur = n
    j = Junction(); j.position = V(mm(205), mm(100)); j = sch.create_items([j])[0]; record("create dangling junction (205,100)")
    wires = sch.get_lines()
    victim = wires[0]; sch.remove_items([victim]); record(f"remove wire {victim.id.value}")
    sch.save(); record("save()")
    sch.revert(); record("revert() (reload from disk)")
for n, dl in steps:
    print(n, json.dumps(dl))
json.dump(steps, open("/w/spikes/2c-diff-highlight/results/collateral.json", "w"), indent=1)
