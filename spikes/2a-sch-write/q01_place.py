"""Q1: place symbols from stock + project libraries headless: refs, units, rotation,
mirror; pin connection points from API data (transform) verified by netlist."""

import itertools
import tempfile
from pathlib import Path

from geom import SSO_DEG, pin_ends_abs, sym_transform, transform
from harness import (
    Recorder,
    Server,
    by_id,
    cli_netlist,
    commit,
    fresh_fixture,
    keep_artifact,
    load_libraries,
    local_label,
    main_guard,
    mm,
    nets_by_pin,
    netlist_components,
    same_net,
    require_kicad,
    root_sch,
    sid,
    update_symbols,
    v2,
)


def main() -> int:
    require_kicad()
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    rec = Recorder("q01_place")
    proj = fresh_fixture("blank")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q01-"))
    with Server(proj) as srv:
        k = srv.client(timeout_ms=30000)
        sch = k.get_schematic()

        rec.attempt(
            "place_before_load_all_libraries_refused",
            lambda: sch.place_symbol_from_library("Device:R", v2(25.4, 25.4)),
            expect_ok=False,
        )
        rec.note("load_all_libraries_s", round(load_libraries(k), 2))

        # --- stock library, no reference: headless does not auto-annotate normal symbols
        r_auto = sch.place_symbol_from_library("Device:R", v2(25.4, 25.4))
        rec.check("stock_place_no_ref_unannotated", r_auto.reference.endswith("?"), r_auto.reference)
        r_ref = sch.place_symbol_from_library("Device:R", v2(38.1, 25.4), reference="R101")
        rec.check("stock_place_with_ref", r_ref.reference == "R101", r_ref.reference)
        rec.check(
            "lib_id_read_back",
            str(r_ref.definition.id) == "Device:R",
            r_ref.definition.id,
        )
        # power symbols are "always annotate" -> numbered even headless
        gnd = sch.place_symbol_from_library("power:GND", v2(50.8, 25.4))
        rec.check("power_symbol_auto_annotated", gnd.reference.startswith("#PWR") and not gnd.reference.endswith("?"), gnd.reference)

        # --- project library (${KIPRJMOD} sym-lib-table)
        tube = [
            sch.place_symbol_from_library("ecc83-pp:ECC83", v2(76.2 + 25.4 * i, 50.8), unit=u, reference="U1")
            for i, u in enumerate((1, 2, 3))
        ]
        rec.check("project_lib_place", all(t.definition.id.library == "ecc83-pp" for t in tube), [t.definition.id for t in tube])
        rec.check("multi_unit_units", [t.unit for t in tube] == [1, 2, 3], [(t.reference, t.unit) for t in tube])
        rec.note("multi_unit_reference_read_back", [t.reference for t in tube])
        rec.attempt(
            "unit_out_of_range_refused",
            lambda: sch.place_symbol_from_library("Device:R", v2(0, 0), unit=2),
            expect_ok=False,
        )
        rec.attempt(
            "unknown_symbol_refused",
            lambda: sch.place_symbol_from_library("Device:NoSuchPart", v2(0, 0)),
            expect_ok=False,
        )

        # --- rotation via placement, mirror via update_items (transform), all 12 requests
        orients = [(O.SSO_0, 0), (O.SSO_90, 90), (O.SSO_180, 180), (O.SSO_270, 270)]
        placed = []
        for n, ((o, deg), mir) in enumerate(itertools.product(orients, (None, "x", "y"))):
            at = (25.4 + 30.48 * (n % 6), 101.6 + 30.48 * (n // 6))
            s = sch.place_symbol_from_library(
                "Amplifier_Operational:LM358", v2(*at), orientation=o, unit=1, reference=f"U{10 + n}"
            )
            if mir:
                s.transform.mirror_x = mir == "x"
                s.transform.mirror_y = mir == "y"
                s = update_symbols(sch, [s])[0]
            placed.append((s, deg, mir))
        fresh = by_id(sch, [sid(s) for s, _, _ in placed])
        # compare the resulting transform MATRIX with the requested one (equivalent forms ok)
        bad = {
            s.reference: {"asked": (deg, mir), "got": (SSO_DEG[int(fresh[sid(s)].transform.orientation)], fresh[sid(s)].transform.mirror_x, fresh[sid(s)].transform.mirror_y)}
            for s, deg, mir in placed
            if sym_transform(fresh[sid(s)]) != transform(deg, mir == "x", mir == "y")
        }
        rec.check("rotate_then_mirror_matrix_as_requested", not bad, bad or "all 12")
        rec.note("orientation_read_back", {s.reference: (SSO_DEG[int(fresh[sid(s)].transform.orientation)], fresh[sid(s)].transform.mirror_x, fresh[sid(s)].transform.mirror_y) for s, _, _ in placed})

        # workaround: request every distinct transform in a form that round-trips
        # (orientation + at most mirror_x; 180+mirror_x is sent as 0+mirror_y)
        canonical = [(0, None), (90, None), (180, None), (270, None), (0, "x"), (90, "x"), (270, "x"), (0, "y")]
        bad = {}
        for n, (deg, mir) in enumerate(canonical):
            s = sch.place_symbol_from_library(
                "Amplifier_Operational:LM358", v2(25.4 + 30.48 * n, 190.5), orientation=orients[deg // 90][0], unit=1, reference=f"U{40 + n}"
            )
            if mir:
                s.transform.mirror_x = mir == "x"
                s.transform.mirror_y = mir == "y"
                s = update_symbols(sch, [s])[0]
            got = by_id(sch, [sid(s)])[sid(s)]
            if sym_transform(got) != transform(deg, mir == "x", mir == "y"):
                bad[got.reference] = (deg, mir)
            placed.append((got, deg, mir))
            fresh[sid(got)] = got
        distinct = {transform(d, m == "x", m == "y") for d, m in canonical}
        rec.check("canonical_8_transforms_round_trip", not bad and len(distinct) == 8, bad or f"{len(distinct)} distinct")

        # pin ends from API data + transform: put a local label exactly on each computed end;
        # the netlist must put that pin on the label's net.
        labels, expect = [], {}
        for s, _, _ in placed:
            s = fresh[sid(s)]
            for pin, at in pin_ends_abs(s).items():
                name = f"{s.reference}_P{pin}"
                labels.append(local_label(name, at))
                expect[(s.reference, pin)] = name
        with commit(sch, "labels on computed pin ends"):
            sch.create_items(labels)
        api = nets_by_pin(sch)
        wrong = {kp: (api.get(kp), n) for kp, n in expect.items() if not same_net(api.get(kp), n)}
        rec.check("pin_ends_from_api_data_all_transforms_api_netlist", not wrong, wrong or f"{len(expect)} pins")
        rec.note("pin_ends_example", {placed[1][0].reference: pin_ends_abs(fresh[sid(placed[1][0])])})

        sch.save()
        cli = cli_netlist(root_sch(proj), tmp)
        wrong = {kp: (cli.get(kp), n) for kp, n in expect.items() if not same_net(cli.get(kp), n)}
        rec.check("pin_ends_transform_cli_netlist_after_save", not wrong, wrong or f"{len(expect)} pins")
        comps = netlist_components((tmp / "cli.net").read_text())
        rec.check("multi_unit_one_component_in_netlist", "U1" in comps and "U1A" not in comps, sorted(c for c in comps if c.startswith("U1"))[:5])
        rec.check("cli_netlist_lib_ids", comps["U1"]["lib"] == "ecc83-pp:ECC83" and comps["R101"]["lib"] == "Device:R", (comps["U1"]["lib"], comps["R101"]["lib"]))
        keep_artifact(proj, "q01_place")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
