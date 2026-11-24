"""Q3: placement via API — move/rotate/flip, courtyard + bbox queries, collision detection, airwire
metrics, groups, speed; generic placement prototype (place.py) on ecc83 and OpenDriveHub, judged by
metrics, KiCad DRC (courtyard overlap) and renders."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from kipy.board_types import Group
from kipy.proto.common.types import DocumentType

import boards
import flow
import place
from harness import RESULTS, Recorder, Server, cli, main_guard


def drc_counts(pcb: Path, out: Path) -> dict:
    cli("pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb), timeout=600)
    d = json.loads(out.read_text())
    types: dict[str, int] = {}
    for v in d.get("violations", []):
        types[v["type"]] = types.get(v["type"], 0) + 1
    types["unconnected_items"] = len(d.get("unconnected_items", []))
    return types


def reference_metrics(r: Recorder, base: Path) -> None:
    """The demo's human placement as a yardstick (same nets, same footprints)."""
    p = boards.ecc83(base / "ref")
    shutil.copyfile(p.dir / "ecc83-ref.kicad_pcb", p.pcb)
    with Server(p.pro) as srv:
        k = srv.client(60000)
        b = flow.open_board(k, p)
        parts = place.read_parts(b)
        parts = [x for x in parts if x.pads]
        L, hp, _ = place.airwire_length(parts)
        bb = b.client.send  # noqa: F841
        xs = [x.abox() for x in parts]
        r.note("ecc83_ref.airwire_mm", round(L, 1))
        r.note("ecc83_ref.hpwl_mm", round(hp, 1))
        r.note("ecc83_ref.parts_extent_mm", (round(max(a[2] for a in xs) - min(a[0] for a in xs), 1),
                                              round(max(a[3] for a in xs) - min(a[1] for a in xs), 1)))


def run() -> int:
    r = Recorder("q03_place")
    base = Path(tempfile.mkdtemp(prefix="q03-"))
    reference_metrics(r, base)
    for name in ("ecc83", "odh"):
        p, net = flow.prepare(base, name, None)
        with Server(p.pro) as srv:
            k = srv.client(300000)
            b, _, info = flow.build(k, p, net, name, placed=False)
            outline = info["outline"]
            parts0 = place.read_parts(b)
            L0, H0, _ = place.airwire_length(parts0)
            r.note(f"{name}.import_positions_airwire_mm", round(L0, 1))

            # queries: bbox with/without text, courtyard from shapes, speed
            fps = b.get_footprints()
            t0 = time.monotonic()
            bbs = b.get_item_bounding_box(fps)
            r.note(f"{name}.bbox_all_s", round(time.monotonic() - t0, 3))
            with_crt = sum(1 for f in fps if any(s.layer in (50, 51) for s in f.definition.shapes))
            r.check(f"{name}.courtyards_present", with_crt >= len(fps) - 4,
                    f"{with_crt}/{len(fps)} footprints have F/B.CrtYd shapes; bboxes={len([x for x in bbs if x])}")

            parts, info = flow.place_all(b, outline)
            r.note(f"{name}.timing", {k_: v for k_, v in info.items() if k_.endswith("_s") or k_ in ("blocks", "refine_moves", "overlaps", "outside")})
            r.note(f"{name}.clusters", info["clusters"])
            r.note(f"{name}.global_nets", info["global_nets"])
            L1, H1, _ = place.airwire_length(parts)
            area, keep = flow.area_and_keepouts(outline, parts)
            ov = place.overlaps(parts)
            out = place.outside(parts, area)
            r.note(f"{name}.placed_airwire_mm", round(L1, 1))
            r.note(f"{name}.placed_hpwl_mm", round(H1, 1))
            r.check(f"{name}.placement_legal_by_boxes", not ov and not out, f"overlaps={ov[:10]} outside={out}")
            b.save()
            r.check(f"{name}.airwire_improved", L1 < L0, f"{L0:.0f} -> {L1:.0f} mm")

            # groups: one group per cluster via API (for the user to move blocks in the GUI)
            t0 = time.monotonic()
            by_ref = {x.ref: x.fp for x in parts}
            groups = []
            for cname, refs in info["clusters"].items():
                if len(refs) < 2:
                    continue
                g = Group()
                g.proto.name = f"blk_{cname}"
                g.proto.items.extend([by_ref[x].id for x in refs])
                groups.append(g)
            try:
                made = b.create_items(groups)
                got = b.get_groups()
                r.check(f"{name}.groups_via_api", len(got) == len(groups),
                        f"created {len(made)} groups, read back {len(got)}, {time.monotonic() - t0:.2f}s; "
                        f"first={got[0].name if got else None} n_items={len(got[0].proto.items) if got else 0}")
            except Exception as e:  # noqa: BLE001
                r.check(f"{name}.groups_via_api", False, repr(e))
            b.save()

            # KiCad DRC (exact courtyard polygons) as the collision oracle
            t0 = time.monotonic()
            counts = drc_counts(p.pcb, base / f"{name}-drc.json")
            r.note(f"{name}.drc_s", round(time.monotonic() - t0, 2))
            r.check(f"{name}.drc_no_courtyard_overlap", counts.get("courtyards_overlap", 0) == 0, counts)

            # renders: placement (fab + courtyard + silk) and with airwire overlay
            segs = flow.draw_airwires(b, parts, skip=set(n for n in info["global_nets"] if place.is_ground(n)))
            b.save()
            t = boards.render(p.pcb, RESULTS / f"q03_{name}_placed.svg", layers="F.Cu,F.Fab,F.CrtYd,Edge.Cuts,User.1")
            r.note(f"{name}.render_s", round(t, 2))
            b.remove_items(segs)
            b.save()
            shutil.copyfile(p.pcb, base / f"{name}-placed.kicad_pcb")
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
