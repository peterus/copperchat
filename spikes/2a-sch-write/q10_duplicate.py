"""Q10: duplicate a block through API item objects (spec 6.2 sch_duplicate): wires /
labels / junctions / no-connects via GetItems -> new UUIDs -> offset -> create_items;
symbols (A) via place_symbol_from_library + field/attribute copy, (B) via create_items of
the read symbol object with a new UUID. Are fields, unit, mirror, body style kept?"""

import uuid

from google.protobuf.json_format import MessageToDict

from geom import pin_ends_abs
from harness import (
    MM,
    Recorder,
    Server,
    by_id,
    commit,
    fresh_fixture,
    junction,
    keep_artifact,
    load_libraries,
    local_label,
    main_guard,
    nets_by_pin,
    no_connect,
    require_kicad,
    sid,
    update_symbols,
    v2,
    wire,
)

DX, DY = 0.0, 76.2


def shift(vec, dx=DX, dy=DY):
    vec.x_nm += round(dx * MM)
    vec.y_nm += round(dy * MM)


def dup_item(item, suffix: str):
    """Copy of a non-symbol item with a new UUID, moved by (DX, DY)."""
    cls = type(item)
    c = cls(proto=item.proto)
    c.proto.id.value = str(uuid.uuid4())
    p = c.proto
    name = cls.__name__
    if name == "SchematicLine":
        shift(p.start)
        shift(p.end)
    elif name in ("Junction", "NoConnectMarker"):
        shift(p.position)
    elif name in ("LocalLabel", "GlobalLabel", "HierarchicalLabel"):
        shift(p.position)
        shift(p.text.position)
        for f in getattr(p, "fields", []):
            shift(f.text.position)
        if name == "GlobalLabel" and p.HasField("intersheet_refs_field"):
            shift(p.intersheet_refs_field.text.position)
        if name == "LocalLabel":
            p.text.text += suffix
    return c


def sym_fields(sym):
    out = {}
    pos = sym.position
    for f in (sym.reference_field, sym.value_field, sym.footprint_field, sym.datasheet_field, sym.description_field, *sym.user_fields):
        out[f.name] = (
            f.text.value if f.name != "Reference" else "<ref>",
            round((f.text.position.x - pos.x) / MM, 4),
            round((f.text.position.y - pos.y) / MM, 4),
            f.visible,
        )
    return out


def sym_sig(sym) -> dict:
    return {
        "lib": str(sym.definition.id),
        "unit": sym.unit,
        "body_style": sym.body_style,
        "transform": MessageToDict(sym.transform.proto),
        "attributes": MessageToDict(sym.attributes.proto),
        "fields": sym_fields(sym),
        "show_pin_numbers": sym.show_pin_numbers,
    }


def copy_onto(src, dst):
    """Copy everything but identity/position from src to the freshly placed dst."""
    for a in ("value_field", "footprint_field", "datasheet_field", "description_field"):
        f = getattr(dst, a)
        sf = getattr(src, a)
        f.proto.CopyFrom(sf.proto)
        shift(f.proto.text.position)
    ref = dst.reference
    dst.reference_field.proto.CopyFrom(src.reference_field.proto)
    shift(dst.reference_field.proto.text.position)
    dst.reference = ref
    dst.proto.ClearField("user_fields")
    for uf in src.user_fields:
        n = dst.proto.user_fields.add()
        n.CopyFrom(uf.proto)
        shift(n.text.position)
    dst.attributes = src.attributes
    dst.transform = src.transform
    dst.body_style = src.body_style
    dst.show_pin_numbers = src.show_pin_numbers
    dst.show_pin_names = src.show_pin_names


def main() -> int:
    require_kicad()
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O
    from kipy.schematic_types import GlobalLabel

    from q02_fields import field

    rec = Recorder("q10_duplicate")
    proj = fresh_fixture("blank")
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        load_libraries(k)
        # --- source block
        r1 = sch.place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R101")
        c1 = sch.place_symbol_from_library("Device:C", v2(76.2, 63.5), orientation=O.SSO_90, reference="C101")
        u1 = sch.place_symbol_from_library("Amplifier_Operational:LM358", v2(127.0, 50.8), unit=2, reference="U101")
        g1 = sch.place_symbol_from_library("74xx:74LS00", v2(177.8, 50.8), unit=3, reference="U102")
        r1 = by_id(sch, [sid(r1)])[sid(r1)]
        r1.value = "10k"
        r1.footprint_field.text.value = "Resistor_SMD:R_0805_2012Metric"
        r1.user_fields = [field("MPN", "RC0805FR-0710KL", (55.88, 55.88), visible=False)]
        r1.attributes.do_not_populate = True
        r1.value_field.text.position = v2(53.34, 49.53)
        u1 = by_id(sch, [sid(u1)])[sid(u1)]
        u1.transform.mirror_x = True
        u1.value = "TLV9002"
        g1 = by_id(sch, [sid(g1)])[sid(g1)]
        g1.body_style = 2
        c1 = by_id(sch, [sid(c1)])[sid(c1)]
        c1.transform.mirror_x = True
        c1.value = "100n"
        for s in (r1, u1, g1, c1):
            update_symbols(sch, [s])
        src_syms = by_id(sch, [sid(r1), sid(c1), sid(u1), sid(g1)])
        pr, pc, pu = (pin_ends_abs(src_syms[sid(x)]) for x in (r1, c1, u1))
        gl = GlobalLabel()
        gl.text.value = "VCC_G"
        gl.position = v2(*pr["1"])
        mid = (pr["2"][0], pc["1"][1])
        with commit(sch, "block wires"):
            made = sch.create_items(
                [
                    wire(pr["2"], mid),
                    wire(mid, pc["1"]),
                    wire(mid, (mid[0], mid[1] + 7.62)),
                    junction(mid),
                    local_label("SIG", (mid[0], mid[1] + 7.62)),
                    gl,
                    no_connect(pu["5"]),
                    local_label("OUT", pu["7"]),
                    local_label("SIG", pc["2"]),
                ]
            )
        src_ids = [sid(m) for m in made]
        src_items = by_id(sch, src_ids)
        rec.check("source_block_built", len(src_items) == len(made), f"{len(src_items)}/{len(made)} non-symbol items")
        nets_src = nets_by_pin(sch)

        # --- duplicate non-symbol items
        dups = [dup_item(src_items[i], "_CH2") for i in src_ids if i in src_items]
        with commit(sch, "duplicate items"):
            created = sch.create_items(dups)
        rec.check(
            "dup_items_new_uuids",
            sorted(sid(c) for c in created) == sorted(sid(d) for d in dups) and not set(map(sid, created)) & set(src_ids),
            f"{len(created)} created",
        )
        back = by_id(sch, [sid(d) for d in dups])
        rec.check(
            "dup_items_all_present_after_push",
            len(back) == len(dups),
            f"{len(back)}/{len(dups)} (junction kept: {any(type(b).__name__ == 'Junction' for b in back.values())})",
        )

        # --- symbols, method A: place from library + copy
        results_a = {}
        for s_id, new_ref in ((sid(r1), "R201"), (sid(c1), "C201"), (sid(u1), "U201"), (sid(g1), "U202")):
            src = src_syms[s_id]
            at = (src.position.x / MM + DX, src.position.y / MM + DY)
            orient = src.transform.orientation
            d = sch.place_symbol_from_library(str(src.definition.id), v2(*at), orientation=orient, unit=src.unit, reference=new_ref)
            d = by_id(sch, [sid(d)])[sid(d)]
            copy_onto(src, d)
            update_symbols(sch, [d])
            d = by_id(sch, [sid(d)])[sid(d)]
            a, b = sym_sig(src), sym_sig(d)
            results_a[src.reference] = {kk: (a[kk], b[kk]) for kk in a if a[kk] != b[kk]}
        rec.check("dup_symbols_place_and_copy_identical", not any(results_a.values()), results_a)

        # --- connectivity of the copy (method A at +DY)
        nets = nets_by_pin(sch)
        rec.check(
            "dup_connectivity_mirrors_source",
            nets.get(("R201", "2")) is not None
            and nets.get(("R201", "2")) == nets.get(("C201", "1"))
            and nets.get(("R101", "2")) != nets.get(("R201", "2")),
            (nets.get(("R201", "2")), nets.get(("C201", "1")), nets.get(("R101", "2"))),
        )
        rec.check(
            "dup_local_label_suffixed",
            (nets.get(("C201", "2")) or "").endswith("SIG_CH2") and (nets_src.get(("C101", "2")) or "").endswith("SIG"),
            (nets.get(("C201", "2")), nets_src.get(("C101", "2"))),
        )
        rec.check(
            "dup_global_label_shared",
            nets.get(("R201", "1")) == nets.get(("R101", "1")) == "VCC_G",
            (nets.get(("R201", "1")), nets.get(("R101", "1"))),
        )
        sch.save()
        keep_artifact(proj, "q10_duplicate_A")
        # the definition reported for a De Morgan symbol after a body style change
        g = by_id(sch, [sid(g1)])[sid(g1)]
        lib_pins = len(k.get_library_symbol("74xx:74LS00").pins)
        rec.check(
            "definition_keeps_all_body_style_pins",
            len(g.definition.pins) == lib_pins,
            f"{len(g.definition.pins)} pins reported for body_style={g.body_style} vs {lib_pins} in the library",
        )

        # --- symbols, method B: create_items with the read object, new UUID + reference
        results_b, made_b = {}, []
        for s_id, new_ref in ((sid(r1), "R301"), (sid(c1), "C301"), (sid(u1), "U301"), (sid(g1), "U302")):
            src = src_syms[s_id]
            d = type(src)(proto=src.proto)
            d.proto.id.value = str(uuid.uuid4())
            shift(d.proto.position, DX, 2 * DY)
            for f in (
                d.proto.reference_field,
                d.proto.value_field,
                d.proto.footprint_field,
                d.proto.datasheet_field,
                d.proto.description_field,
                *d.proto.user_fields,
            ):
                shift(f.text.position, DX, 2 * DY)
            d.reference = new_ref
            try:
                with commit(sch, "dup symbol B"):
                    got = sch.create_items([d])[0]
                got = by_id(sch, [sid(got)])[sid(got)]
                made_b.append(got)
                a, b = sym_sig(src), sym_sig(got)
                results_b[src.reference] = {kk: (a[kk], b[kk]) for kk in a if a[kk] != b[kk]}
                results_b[src.reference + "_ref"] = got.reference
            except Exception as e:  # noqa: BLE001
                results_b[src.reference] = f"{type(e).__name__}: {e}"
        diffs = {kk: v for kk, v in results_b.items() if not kk.endswith("_ref")}
        rec.check(
            "dup_symbols_create_items_refs",
            all(results_b.get(kk) in ("R301", "C301", "U301", "U302") for kk in results_b if kk.endswith("_ref")),
            {kk: v for kk, v in results_b.items() if kk.endswith("_ref")},
        )
        rec.check(
            "dup_symbols_create_items_props_identical_except_lib",
            all(isinstance(v, dict) and set(v) <= {"lib"} for v in diffs.values()),
            diffs,
        )
        rec.check(
            "dup_symbols_create_items_keep_library_link",
            all(isinstance(v, dict) and "lib" not in v for v in diffs.values()),
            {kk: v.get("lib") if isinstance(v, dict) else v for kk, v in diffs.items()},
        )

        sch.save()
        keep_artifact(proj, "q10_duplicate_B")

    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
