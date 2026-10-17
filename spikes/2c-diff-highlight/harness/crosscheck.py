#!/usr/bin/env python3
"""Cross-check a tool-recorded change log against `kicad-cli sch diff --format json`.

Match key = item UUID (last component of the diff's KIID_PATH id). Output per pair:
matched, logged-but-absent-from-diff, diff-but-unlogged (collateral), and whether the
op (add/delete/modify) agrees.  usage: crosscheck.py PAIRS_DIR  -> results/crosscheck.json
"""
import json, sys
from pathlib import Path

pairs = Path(sys.argv[1])
res = {}
for d in sorted(p for p in pairs.iterdir() if p.is_dir()):
    log = json.loads((d / "changelog.json").read_text())
    diff = json.loads((d / "diff.json").read_text())["changes"]
    by_uuid = {}
    for c in diff:
        by_uuid.setdefault(c["id"].rsplit("/", 1)[-1], []).append(c)
    opmap = {"add": "added", "delete": "removed", "modify": "modified"}
    matched, absent, wrong_op = [], [], []
    for e in log:
        hits = by_uuid.get(e["uuid"], [])
        if not hits:
            absent.append(e); continue
        if any(h["kind"] == opmap[e["op"]] for h in hits):
            matched.append({"uuid": e["uuid"], "op": e["op"], "kind": e["kind"], "ref": e.get("ref"),
                            "instances": len(hits)})
        else:
            wrong_op.append({"log": e, "diff_kinds": [h["kind"] for h in hits]})
    logged = {e["uuid"] for e in log}
    unlogged = [{"uuid": u, "kind": h["kind"], "type": h["typeName"], "ref": h.get("refdes")}
                for u, hs in by_uuid.items() if u not in logged for h in hs]
    res[d.name] = dict(logged=len(log), diff_changes=len(diff), matched=matched, logged_absent_from_diff=absent,
                       wrong_op=wrong_op, unlogged_in_diff=unlogged)
    print(f"{d.name}: log {len(log)} / diff {len(diff)} -> matched {len(matched)}, absent {len(absent)}, "
          f"wrong-op {len(wrong_op)}, unlogged {len(unlogged)}")
out = Path(__file__).resolve().parent.parent / "results" / "crosscheck.json"
out.write_text(json.dumps(res, indent=1))
