"""Q9: symbol bounding boxes from API data (library body graphics + pins + transform,
geom.symbol_bbox) vs. KiCad's own plot (kicad-cli sch export png of the saved file, fields
hidden, no drawing sheet). GetBoundingBox is board-only at the pin (spec 13)."""

import tempfile
from pathlib import Path

from geom import symbol_bbox
from harness import (
    Recorder,
    Server,
    by_id,
    fresh_fixture,
    keep_artifact,
    load_libraries,
    main_guard,
    require_kicad,
    root_sch,
    run_cli,
    sid,
    update_symbols,
    v2,
)

DPI = 600
TOL_MM = 0.6  # plotted stroke (0.254 mm default pen, wider for filled bodies) + pixel grid
CASES = [
    ("Device:R", 0, None, 1, None),
    ("Device:C_Polarized", 90, None, 1, None),
    ("Device:D", 0, "x", 1, None),
    ("Transistor_BJT:BC547", 0, "y", 1, None),
    ("Amplifier_Operational:LM358", 0, None, 1, None),
    ("Amplifier_Operational:LM358", 270, "x", 2, None),
    ("Amplifier_Operational:LM358", 0, None, 3, None),
    ("Regulator_Linear:AMS1117-3.3", 0, None, 1, None),
    ("Connector_Generic:Conn_01x04", 180, None, 1, None),
    ("MCU_Microchip_ATmega:ATmega328-P", 0, None, 1, None),
    ("74xx:74LS00", 0, None, 1, 2),
    ("power:GND", 0, None, 1, None),
    ("ecc83-pp:ECC83", 90, None, 1, None),
]
SLOTS = [(30 + 60 * (i % 6), 30 + 70 * (i // 6)) for i in range(len(CASES))]


def hide_fields(sym):
    for f in (sym.reference_field, sym.value_field, sym.footprint_field, sym.datasheet_field, sym.description_field, *sym.user_fields):
        f.visible = False


def main() -> int:
    require_kicad()
    from PIL import Image
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    Image.MAX_IMAGE_PIXELS = None
    orient = {0: O.SSO_0, 90: O.SSO_90, 180: O.SSO_180, 270: O.SSO_270}
    rec = Recorder("q09_bbox")
    proj = fresh_fixture("blank")
    tmp = Path(tempfile.mkdtemp(prefix="s2a-q09-"))
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        ps = sch.get_page_settings()
        from kipy.proto.common.types.base_types_pb2 import PageSize

        ps.proto.page_size = PageSize.PS_A3
        sch.set_page_settings(ps)
        rec.check("page_size_set_a3", sch.get_page_settings().proto.page_size == PageSize.PS_A3, sch.get_page_settings().proto)
        load_libraries(k)
        placed = []
        for n, ((lib, deg, mir, unit, style), at) in enumerate(zip(CASES, SLOTS, strict=True)):
            try:
                s = sch.place_symbol_from_library(lib, v2(*at), orientation=orient[deg], unit=unit, reference=f"X{n + 1}" if not lib.startswith("power:") else None)
            except Exception as e:  # noqa: BLE001
                rec.note(f"skip_{lib}", str(e))
                continue
            s = by_id(sch, [sid(s)])[sid(s)]
            hide_fields(s)
            s.transform.mirror_x = mir == "x"
            s.transform.mirror_y = mir == "y"
            if style:
                s.body_style = style
            update_symbols(sch, [s])
            placed.append((lib, deg, mir, unit, style, sid(s)))
        sch.save()
        syms = by_id(sch, [p[-1] for p in placed])
        styles = {p[-1]: syms[p[-1]].body_style for p in placed if p[4]}
        rec.check("body_style_set_via_update", all(v == 2 for v in styles.values()) and styles, styles)
        r = run_cli("sch", "export", "png", "-e", "-n", "--dpi", str(DPI), "-o", str(tmp), str(root_sch(proj)))
        pngs = sorted(tmp.glob("*.png"))
        rec.check("cli_png_export", r.returncode == 0 and pngs, (r.returncode, [p.name for p in pngs], r.stderr[-200:]))
        img = Image.open(pngs[0]).convert("RGBA")
        alpha = img.getchannel("A")
        px_per_mm = DPI / 25.4
        results, worst = {}, 0.0
        for lib, deg, mir, unit, style, i in placed:
            api = symbol_bbox(syms[i])
            if api is None:
                continue
            m = 4.0  # search window around the API box
            box = tuple(round(v * px_per_mm) for v in (api[0] - m, api[1] - m, api[2] + m, api[3] + m))
            crop = alpha.crop(box).point(lambda a: 255 if a > 40 else 0)
            bb = crop.getbbox()
            if bb is None:
                results[f"{lib} u{unit} {deg}{mir or ''}"] = {"api": api, "plot": None}
                continue
            plot = (
                round((box[0] + bb[0]) / px_per_mm, 3),
                round((box[1] + bb[1]) / px_per_mm, 3),
                round((box[0] + bb[2]) / px_per_mm, 3),
                round((box[1] + bb[3]) / px_per_mm, 3),
            )
            dev = max(abs(a - b) for a, b in zip(api, plot, strict=True))
            worst = max(worst, dev)
            results[f"{lib} u{unit} {deg}{mir or ''}{' bs2' if style else ''}"] = {"api": api, "plot": plot, "max_dev_mm": round(dev, 3)}
        rec.note("bbox_api_vs_plot", results)
        over = {kk: v for kk, v in results.items() if v.get("max_dev_mm", 99) > TOL_MM}
        rec.check("api_bbox_matches_plot_within_0_6mm", not over and len(results) == len(CASES), {"worst_mm": round(worst, 3), "over_tolerance": over})
        margin = 2.0
        outside = {
            kk: v for kk, v in results.items()
            if v["plot"] is None or not (v["plot"][0] >= v["api"][0] - margin and v["plot"][1] >= v["api"][1] - margin and v["plot"][2] <= v["api"][2] + margin and v["plot"][3] <= v["api"][3] + margin)
        }
        rec.check("api_bbox_plus_2mm_margin_contains_plot", not outside and len(results) == len(CASES), outside or f"{len(results)} symbols")
        keep_artifact(proj, "q09_bbox")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
