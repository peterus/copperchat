"""Q2: field updates (value, footprint, datasheet, custom fields, DNP / exclude flags)
incl. field positions and visibility; multi-unit symbols; persistence after save."""

import tempfile
import time
from pathlib import Path

from harness import (
    MM,
    Recorder,
    Server,
    by_id,
    fresh_fixture,
    keep_artifact,
    load_libraries,
    main_guard,
    mm,
    netlist_components,
    require_kicad,
    root_sch,
    run_cli,
    sid,
    update_symbols,
    v2,
)


def field(name, value, at, visible=True):
    from kipy.schematic_types import SchematicField

    f = SchematicField()
    f.name = name
    f.text.value = value
    f.text.position = v2(*at)
    f.visible = visible
    return f


def hang_probe(rec: Recorder) -> None:
    """A full-definition update of a 2nd instance of a multi-unit symbol (fresh server)."""
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=120000)
        sch = k.get_schematic()
        load_libraries(k)
        a = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(50.8, 50.8), reference="U1")
        b = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(101.6, 50.8), reference="U2")
        a.value, b.value = "A", "B"
        quick = srv.client(timeout_ms=10000).get_schematic()
        t0 = time.monotonic()
        ok = True
        try:
            quick.update_items([a])
            quick.update_items([b])  # with the definition, as returned by GetItems
        except Exception as e:  # noqa: BLE001
            ok = False
            err = f"{type(e).__name__}: {e} after {time.monotonic() - t0:.1f}s"
        alive = True
        try:
            srv.client(timeout_ms=5000).ping()
        except Exception:  # noqa: BLE001
            alive = False
        rec.check(
            "multi_unit_full_definition_update_responds",
            ok,
            "ok" if ok else f"{err}; server answers ping afterwards: {alive}",
        )
    # same edit without the definition (harness.update_symbols) on a fresh server
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=120000)
        sch = k.get_schematic()
        load_libraries(k)
        a = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(50.8, 50.8), reference="U1")
        b = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(101.6, 50.8), reference="U2")
        a.value, b.value = "A", "B"
        quick = srv.client(timeout_ms=10000).get_schematic()
        update_symbols(quick, [a])
        update_symbols(quick, [b])
        got = by_id(quick, [sid(a), sid(b)])
        rec.check(
            "multi_unit_definitionless_update_workaround",
            [got[sid(a)].value, got[sid(b)].value] == ["A", "B"]
            and len(got[sid(a)].definition.pins) == 8
            and str(got[sid(b)].definition.id) == "Amplifier_Operational:LM358",
            [(g.reference, g.value, str(g.definition.id), len(g.definition.pins)) for g in got.values()],
        )


def main() -> int:
    require_kicad()
    rec = Recorder("q02_fields")
    hang_probe(rec)

    proj = fresh_fixture("blank")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q02-"))
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)
        r = sch.place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R1")
        u_a = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(101.6, 50.8), unit=1, reference="U1")
        u_b = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(101.6, 88.9), unit=2, reference="U1")
        rec.note(
            "fields_after_place",
            {f.name: (f.text.value, mm(f.text.position), f.visible) for f in (r.reference_field, r.value_field, r.footprint_field)},
        )
        rec.check("fields_autoplaced_flag_after_headless_place", True, f"fields_autoplaced={r.fields_autoplaced}")

        r = by_id(sch, [sid(r)])[sid(r)]
        val_pos = mm(r.value_field.text.position)
        r.value = "10k"
        r.footprint_field.text.value = "Resistor_SMD:R_0805_2012Metric"
        r.datasheet_field.text.value = "https://example.invalid/rc0805.pdf"
        r.description_field.text.value = "Resistor 10k 1% 0805"
        r.value_field.text.position = v2(val_pos[0] + 2.54, val_pos[1])
        r.user_fields = [
            field("MPN", "RC0805FR-0710KL", (55.88, 55.88), visible=False),
            field("Manufacturer", "Yageo", (55.88, 58.42), visible=False),
        ]
        r.attributes.do_not_populate = True
        r.attributes.exclude_from_bill_of_materials = True
        r.attributes.exclude_from_board = False
        t0 = time.monotonic()
        upd = update_symbols(sch, [r])[0]
        rec.note("update_fields_ms", round((time.monotonic() - t0) * 1000, 1))
        g = by_id(sch, [sid(r)])[sid(r)]
        user = {f.name: (f.text.value, mm(f.text.position), f.visible) for f in g.user_fields}
        rec.check(
            "value_footprint_datasheet_description",
            (g.value, g.footprint_field.text.value, g.datasheet_field.text.value, g.description_field.text.value)
            == ("10k", "Resistor_SMD:R_0805_2012Metric", "https://example.invalid/rc0805.pdf", "Resistor 10k 1% 0805"),
            (g.value, g.footprint_field.text.value),
        )
        rec.check(
            "custom_fields_added", user.get("MPN", ("",))[0] == "RC0805FR-0710KL" and user.get("Manufacturer", ("",))[0] == "Yageo", user
        )
        rec.check("custom_field_position_visibility", user.get("MPN", (0, 0, True))[1:] == ((55.88, 55.88), False), user.get("MPN"))
        rec.check(
            "value_field_position_set",
            mm(g.value_field.text.position) == (round(val_pos[0] + 2.54, 4), val_pos[1]),
            (val_pos, mm(g.value_field.text.position)),
        )
        rec.check(
            "dnp_exclude_flags",
            (g.attributes.do_not_populate, g.attributes.exclude_from_bill_of_materials, g.attributes.exclude_from_board)
            == (True, True, False),
            g.attributes.proto,
        )
        rec.check(
            "update_response_equals_read_back",
            upd.value == g.value and len(upd.user_fields) == len(g.user_fields),
            (upd.value, len(upd.user_fields)),
        )
        # field removal: drop Manufacturer again
        g.user_fields = [f for f in g.user_fields if f.name != "Manufacturer"]
        update_symbols(sch, [g])
        g2 = by_id(sch, [sid(r)])[sid(r)]
        rec.check("custom_field_removed", [f.name for f in g2.user_fields] == ["MPN"], [f.name for f in g2.user_fields])

        # multi-unit: fields live on each unit's SCH_SYMBOL; API does not sync them
        a = by_id(sch, [sid(u_a)])[sid(u_a)]
        a.value = "TLV9002"
        a.footprint_field.text.value = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
        update_symbols(sch, [a])
        b = by_id(sch, [sid(u_b)])[sid(u_b)]
        rec.check(
            "multi_unit_fields_synced_by_kicad", b.value == "TLV9002", f"unit2 value={b.value!r} footprint={b.footprint_field.text.value!r}"
        )
        b.value = "TLV9002"
        b.footprint_field.text.value = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
        update_symbols(sch, [b])
        b = by_id(sch, [sid(u_b)])[sid(u_b)]
        rec.check("multi_unit_fields_synced_by_client", b.value == "TLV9002", b.value)

        sch.save()
        out = tmp / "q02.net"
        res = run_cli("sch", "export", "netlist", "-o", str(out), str(root_sch(proj)))
        comps = netlist_components(out.read_text()) if res.returncode == 0 else {}
        c = comps.get("R1", {})
        rec.check(
            "cli_netlist_fields",
            c.get("value") == "10k"
            and c.get("footprint") == "Resistor_SMD:R_0805_2012Metric"
            and c.get("fields", {}).get("MPN") == "RC0805FR-0710KL",
            c,
        )
        rec.check("cli_netlist_dnp_exclude_props", "dnp" in c.get("props", {}) and "exclude_from_bom" in c.get("props", {}), c.get("props"))
        rec.check("cli_netlist_multi_unit_value", comps.get("U1", {}).get("value") == "TLV9002", comps.get("U1"))
        keep_artifact(proj, "q02_fields")

    # persistence: reopen the saved files in a fresh server
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        g = next(s for s in sch.get_symbols() if s.reference == "R1")
        mpn = next((f for f in g.user_fields if f.name == "MPN"), None)
        rec.check(
            "reopen_fields_positions_persist",
            g.value == "10k"
            and mpn is not None
            and mm(mpn.text.position) == (55.88, 55.88)
            and not mpn.visible
            and abs(g.value_field.text.position.x - round((val_pos[0] + 2.54) * MM)) < 10,
            (g.value, mpn and (mm(mpn.text.position), mpn.visible), mm(g.value_field.text.position)),
        )
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
