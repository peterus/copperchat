"""Q2: board setup through the API — outline (rounded rectangle on Edge.Cuts), M2 mounting holes,
2 <-> 4 copper layers + stackup, design rules (JLCPCB-ish minimums), net classes + assignments,
custom rules, keepout rule area. What is settable via API, what only via file/project?"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from kipy.board_types import BoardRectangle, Zone
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board import board_commands_pb2, board_pb2, board_rules_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneType
from kipy.proto.common.types import DocumentType, project_settings_pb2
from kipy.project_types import NetClass

import boards
from harness import Recorder, Server, cli, main_guard

MM = 1_000_000
W, H, R = 60 * MM, 40 * MM, 3 * MM  # board size + corner radius
X0, Y0 = 100 * MM, 100 * MM


def drc_json(p: boards.Proj, out: Path) -> dict:
    res = cli("pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "-o", str(out), str(p.pcb),
              timeout=600)
    return json.loads(out.read_text()) if out.exists() else {"error": res.stdout + res.stderr}


def run() -> int:
    r = Recorder("q02_setup")
    base = Path(tempfile.mkdtemp(prefix="q02-"))
    p = boards.ecc83(base)
    net, _ = p.netlist()
    with Server(p.pro) as srv:
        k = srv.client(120000)
        k.open_document(str(p.pcb), DocumentType.DOCTYPE_PCB)
        b = k.get_board()
        b.import_netlist(str(net))

        # --- outline: one rectangle with corner radius on Edge.Cuts -------------------------
        rect = BoardRectangle()
        rect.layer = BoardLayer.BL_Edge_Cuts
        rect.top_left = Vector2.from_xy(X0, Y0)
        rect.bottom_right = Vector2.from_xy(X0 + W, Y0 + H)
        rect.proto.shape.rectangle.corner_radius.value_nm = R
        rect.proto.shape.attributes.stroke.width.value_nm = 50_000
        made = b.create_items(rect)
        back = b.get_shapes()
        rr = [s for s in back if s.layer == BoardLayer.BL_Edge_Cuts]
        r.check("outline_rounded_rect", len(made) == 1 and rr and rr[0].proto.shape.rectangle.corner_radius.value_nm == R,
                f"edge shapes={len(rr)} radius={rr[0].proto.shape.rectangle.corner_radius.value_nm if rr else None}")
        bb = b.client.send(board_commands_pb2.GetBoardBoundingBox(board=b.document,
                           mode=board_commands_pb2.BoardBoundingBoxMode.Value("BBBM_BOARD_EDGES_ONLY")
                           if "BBBM_BOARD_EDGES_ONLY" in board_commands_pb2.BoardBoundingBoxMode.keys() else 0),
                           board_commands_pb2.BoardBoundingBoxResponse)
        r.note("board_bbox", f"{bb} modes={list(board_commands_pb2.BoardBoundingBoxMode.keys())}")

        # --- mounting holes: PlaceFootprintFromLibrary (needs libs loaded) vs CreateItems ------
        hole = "MountingHole:MountingHole_2.2mm_M2"
        try:
            b.place_footprint_from_library(hole, Vector2.from_xy(X0 + 3 * MM, Y0 + 3 * MM))
            r.check("place_from_library_without_load", True, "worked without load_all_libraries")
            b.remove_items([f for f in b.get_footprints() if f.reference_field.text.value.startswith("H")])
        except Exception as e:  # noqa: BLE001
            r.note("place_from_library_without_load", str(e))
        t0 = time.monotonic()
        k.get_library_footprint(hole)  # lazy per-library load (spike 3A Q7)
        r.note("lazy_lib_load_s", round(time.monotonic() - t0, 2))
        placed = []
        for i, (dx, dy) in enumerate([(3.5, 3.5), (W / MM - 3.5, 3.5), (3.5, H / MM - 3.5), (W / MM - 3.5, H / MM - 3.5)]):
            try:
                fp = b.place_footprint_from_library(hole, Vector2.from_xy(X0 + int(dx * MM), Y0 + int(dy * MM)))
                fp.reference_field.text.value = f"H{i + 1}"
                b.update_items(fp)
                placed.append(fp)
            except Exception as e:  # noqa: BLE001
                r.note(f"place_hole_{i}", str(e))
                break
        holes = [f for f in b.get_footprints() if f.reference_field.text.value.startswith("H")]
        r.check("mounting_holes_placed", len(holes) == 4,
                f"{len(holes)} holes, pads={[len(h.definition.pads) for h in holes]}, refs={[h.reference_field.text.value for h in holes]}")

        # --- layers 2 -> 4 ------------------------------------------------------------------
        r.note("copper_before", b.get_copper_layer_count())
        non_cu = [ly for ly in b.get_enabled_layers() if ly not in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu)]
        t0 = time.monotonic()
        layers = b.set_enabled_layers(4, non_cu)
        r.check("set_4_layers", b.get_copper_layer_count() == 4 and BoardLayer.BL_In1_Cu in layers,
                f"count={b.get_copper_layer_count()} in1={BoardLayer.BL_In1_Cu in layers} {time.monotonic() - t0:.2f}s")
        st = b.get_stackup()
        r.note("stackup_after_4", [(BoardLayer.Name(x.layer) if x.layer else x.user_name, x.thickness / 1000, x.material_name)
                                   for x in st.layers])
        # try to change a dielectric thickness (JLC04161H-7628: prepreg 0.2104 mm, core 1.065 mm)
        try:
            up = board_commands_pb2.UpdateBoardStackup()
            up.board.CopyFrom(b.document)
            up.stackup.CopyFrom(st.proto)
            b.client.send(up, board_commands_pb2.BoardStackupResponse)
            r.check("update_stackup", True, "UpdateBoardStackup accepted")
        except Exception as e:  # noqa: BLE001
            r.check("update_stackup_no_handler", "no handler" in str(e), e)

        # --- design rules --------------------------------------------------------------------
        dr = b.get_design_rules()
        r.note("design_rules_before", str(dr.proto if hasattr(dr, "proto") else dr)[:800])
        rules = board_rules_pb2.BoardDesignRules()
        rules.CopyFrom(dr.rules.proto if hasattr(dr, "rules") else dr.proto.rules)
        c = rules.constraints
        for field, mm in (("min_clearance", 0.127), ("min_track_width", 0.127), ("min_via_size", 0.45),
                          ("min_through_drill", 0.3), ("min_via_annular_width", 0.075), ("copper_edge_clearance", 0.3),
                          ("hole_to_hole_min", 0.5), ("hole_clearance", 0.25), ("silk_clearance", 0.15)):
            getattr(c, field).value_nm = int(mm * MM)
        del rules.predefined_sizes.tracks[:]
        for w in (0.2, 0.3, 0.5, 0.8):
            rules.predefined_sizes.tracks.add().width.value_nm = int(w * MM)
        del rules.predefined_sizes.vias[:]
        v = rules.predefined_sizes.vias.add()
        v.diameter.value_nm, v.drill.value_nm = int(0.6 * MM), int(0.3 * MM)
        cmd = board_commands_pb2.SetBoardDesignRules(board=b.document, rules=rules)
        resp = b.client.send(cmd, board_commands_pb2.BoardDesignRulesResponse)
        got = b.get_design_rules()
        gp = got.proto if hasattr(got, "proto") else got
        r.check("set_design_rules", gp.rules.constraints.min_clearance.value_nm == int(0.127 * MM)
                and len(gp.rules.predefined_sizes.tracks) == 4,
                f"clearance={gp.rules.constraints.min_clearance.value_nm} tracks={[t.width.value_nm for t in gp.rules.predefined_sizes.tracks]} "
                f"errors={list(getattr(resp, 'errors', []))}")

        # --- net classes (project) + pattern assignment ---------------------------------------
        proj = b.get_project()
        r.note("project_spec_from_board", f"name={proj.document.project.name!r} path={proj.document.project.path!r}")
        try:
            proj.get_net_classes()
        except Exception as e:  # noqa: BLE001
            r.check("project_path_mismatch", "is not open at path" in str(e), e)
            # KiCad's PROJECT::GetProjectPath() ends with '/', the spec from the board does not
            proj.document.project.path = proj.document.project.path.rstrip("/") + "/"
        nc = project_settings_pb2.NetClass(name="Power", priority=1)
        nc.board.clearance.value_nm = int(0.2 * MM)
        nc.board.track_width.value_nm = int(0.5 * MM)
        try:
            proj.set_net_classes([NetClass(nc)])
            names = [n.name for n in proj.get_net_classes()]
            r.check("set_net_classes", "Power" in names, names)
        except Exception as e:  # noqa: BLE001
            r.check("set_net_classes", False, e)
        try:
            from kipy.project_types import NetClassAssignments, NetClassPatternAssignment
            pa = NetClassPatternAssignment(project_settings_pb2.NetClassPatternAssignment(pattern="GND", netclass="Power"))
            proj.set_net_class_assignments(NetClassAssignments(pattern_assignments=[pa]))
            nets = [n for n in b.get_nets() if n.name in ("GND", "/GND")]
            ncs = b.get_netclass_for_nets(nets)
            r.check("net_class_assignment_applies", any(v.name == "Power" or "Power" in v.proto.constituents for v in ncs.values()),
                    {k_: (v.name, list(v.proto.constituents), v.proto.board.track_width.value_nm) for k_, v in ncs.items()})
        except Exception as e:  # noqa: BLE001
            r.check("net_class_assignment_applies", False, repr(e))

        # --- custom rule (text-equivalent proto) ----------------------------------------------
        rule = board_rules_pb2.CustomRule(name="pwr_width", condition="A.NetClass == 'Power'")
        con = rule.constraints.add()
        con.type = board_rules_pb2.CustomRuleConstraintType.Value(
            next(n for n in board_rules_pb2.CustomRuleConstraintType.keys() if "TRACK_WIDTH" in n))
        con.numeric.min = int(0.4 * MM)
        cr = b.client.send(board_commands_pb2.SetCustomDesignRules(board=b.document, rules=[rule]),
                           board_commands_pb2.CustomRulesResponse)
        r.check("custom_rules", board_commands_pb2.CustomRulesStatus.Name(cr.status).endswith("OK") or len(cr.rules) == 1,
                f"status={board_commands_pb2.CustomRulesStatus.Name(cr.status)} err={cr.error_text!r} n={len(cr.rules)}")

        # --- keepout rule area ----------------------------------------------------------------
        z = Zone()
        z.type = ZoneType.ZT_RULE_AREA
        z.layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu]
        z.name = "ANT_KEEPOUT"
        ra = z.proto.rule_area_settings
        ra.keepout_copper = ra.keepout_tracks = ra.keepout_vias = True
        ln = PolyLine()
        for x, y in ((X0 + 40 * MM, Y0), (X0 + W, Y0), (X0 + W, Y0 + 10 * MM), (X0 + 40 * MM, Y0 + 10 * MM)):
            ln.append(PolyLineNode.from_xy(x, y))
        ln.closed = True
        poly = PolygonWithHoles()
        poly.outline = ln
        z.outline = poly
        made = b.create_items(z)
        zs = [x for x in b.get_zones() if x.is_rule_area]
        r.check("keepout_rule_area", len(zs) == 1 and zs[0].proto.rule_area_settings.keepout_tracks,
                f"rule areas={len(zs)} name={zs[0].name if zs else None}")

        b.save()
        txt = p.pcb.read_text()
        r.check("file_has_4_cu_layers", '"In1.Cu"' in txt and '"In2.Cu"' in txt, "In1/In2 in (layers)")
        # KiCad writes (stackup ...) only once a user edited it; the API cannot (no UpdateBoardStackup handler)
        r.check("stackup_not_persisted", "(stackup" not in txt, "no (stackup ...) section in the saved board")
        pro = json.loads(p.pro.read_text())
        r.check("project_has_rules_and_netclass",
                pro["board"]["design_settings"]["rules"]["min_clearance"] == 0.127
                and any(n["name"] == "Power" for n in pro["net_settings"]["classes"]),
                f"rules.min_clearance={pro['board']['design_settings']['rules'].get('min_clearance')} "
                f"classes={[n['name'] for n in pro['net_settings']['classes']]} patterns={pro['net_settings'].get('netclass_patterns')}")
        dru = p.pro.with_suffix(".kicad_dru")
        r.check("custom_rules_file", dru.exists(), dru.read_text()[:400] if dru.exists() else "no .kicad_dru")

        # layers 4 -> 2 deletes inner content (documented in kipy); just verify the round trip
        b.set_enabled_layers(2, non_cu)
        r.check("set_2_layers", b.get_copper_layer_count() == 2, b.get_copper_layer_count())
        b.set_enabled_layers(4, non_cu)
        b.save()

    d = drc_json(p, base / "drc.json")
    types = {}
    for v in d.get("violations", []):
        types[v["type"]] = types.get(v["type"], 0) + 1
    r.check("drc_runs_with_outline", "violations" in d and "invalid_outline" not in types,
            f"violations={types} parity={sorted({x.get('description', '') for x in d.get('schematic_parity', [])})} unconnected={len(d.get('unconnected_items', []))} parity={len(d.get('schematic_parity', []))}")
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
