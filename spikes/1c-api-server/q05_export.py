"""Q5: PNG/SVG export via API jobs for sch + pcb; pixel->mm mapping."""

import importlib
import tempfile
from pathlib import Path

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad, run_cli

DPI = 300


def find_msg(name: str):
    for mod in ("kipy.proto.board.board_jobs_pb2", "kipy.proto.schematic.schematic_jobs_pb2",
                "kipy.proto.common.commands.editor_commands_pb2"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        if hasattr(m, name):
            return mod, getattr(m, name)
    return None, None


def job(rec, name, fn):
    try:
        res = fn()
    except Exception as e:  # noqa: BLE001
        rec.check(f"run_{name}", False, f"{type(e).__name__}: {e}")
        return []
    paths = [Path(p) for p in res.output_paths]
    ok = bool(res) and bool(paths) and all(p.exists() for p in paths)
    rec.check(f"run_{name}", ok, res)
    return paths


def edge_bbox_mm(board):
    from kipy.proto.board.board_types_pb2 import BoardLayer

    xs, ys = [], []
    for s in board.get_shapes():
        if s.layer != BoardLayer.BL_Edge_Cuts:
            continue
        box = board.get_item_bounding_box(s)
        if box is None:
            continue
        xs += [box.pos.x, box.pos.x + box.size.x]
        ys += [box.pos.y, box.pos.y + box.size.y]
    return (min(xs) / 1e6, min(ys) / 1e6, max(xs) / 1e6, max(ys) / 1e6) if xs else None


def main() -> int:
    require_kicad()
    from kipy.board_jobs import PlotSettings as BoardPlot
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from kipy.proto.schematic.schematic_jobs_pb2 import SJSM_ALL_SHEETS

    rec = Recorder("q05_export")
    out = Path(tempfile.mkdtemp(prefix="c1-exp-"))
    proj = fresh_fixture("ecc83")
    for name in ("RunSchematicJobExportPng", "RunBoardJobExportPng",
                 "RunSchematicJobExportSvg", "RunBoardJobExportSvg"):
        mod, cls = find_msg(name)
        rec.check(f"proto_{name}", cls is not None, mod or "message not in this kipy revision")
        if cls is not None:
            rec.note(f"fields_{name}", [f.name for f in cls.DESCRIPTOR.fields])

    with Server(proj) as srv:
        k = srv.client(timeout_ms=120000)
        sch, board = k.get_schematic(), k.get_board()
        sch_png = job(rec, "sch_png", lambda: sch.export_png(str(out / "sch_png"), dpi=DPI,
                                                            sheet_mode=SJSM_ALL_SHEETS))
        job(rec, "sch_svg", lambda: sch.export_svg(str(out / "sch_svg"),
                                                  sheet_mode=SJSM_ALL_SHEETS))
        ps = BoardPlot()
        ps.layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_F_SilkS,
                     BoardLayer.BL_Edge_Cuts]
        # scale 0 (proto default) auto-fits the board to the page; 1.0 keeps board mm == page mm
        ps.scale = 1.0
        pcb_png = job(rec, "pcb_png", lambda: board.export_png(str(out / "pcb.png"),
                                                              plot_settings=ps, dpi=DPI))
        job(rec, "pcb_svg", lambda: board.export_svg(str(out / "pcb.svg"), plot_settings=ps))
        job(rec, "pcb_svg_fit", lambda: board.export_svg(str(out / "pcb_fit.svg"),
                                                        plot_settings=ps, fit_page_to_board=True))
        bbox = edge_bbox_mm(board)
        rec.note("edge_cuts_bbox_mm", bbox)

        from PIL import Image

        for p in sch_png + pcb_png:
            for f in ([p] if p.is_file() else sorted(p.glob("*.png"))):
                with Image.open(f) as im:
                    content = im.getchannel("A").getbbox() if "A" in im.getbands() else None
                    rec.note(f"png_{f.name}", {"size": im.size, "mode": im.mode,
                                               "content_bbox_px": content})
        if sch_png:
            f = sch_png[0] if sch_png[0].is_file() else next(sch_png[0].glob("*.png"))
            with Image.open(f) as im:
                w_mm, h_mm = (v * 25.4 / DPI for v in im.size)
            # Schematic coordinates are page coordinates (origin = page top-left), so a
            # page-sized PNG maps px -> mm with the same formula.
            rec.check("sch_png_is_full_page_a4", abs(w_mm - 297) < 1 and abs(h_mm - 210) < 1,
                      f"{w_mm:.1f} x {h_mm:.1f} mm")
            c1 = next(s for s in sch.get_symbols() if s.reference == "C1")
            rec.note("sch_C1_mm_and_px", (c1.position.x / 1e6, c1.position.y / 1e6,
                                          round(c1.position.x / 1e6 * DPI / 25.4),
                                          round(c1.position.y / 1e6 * DPI / 25.4)))
        if pcb_png and bbox:
            with Image.open(pcb_png[0]) as im:
                w_px, h_px = im.size
                content = im.getchannel("A").getbbox()
            mm_per_px = 25.4 / DPI
            page_w_mm, page_h_mm = w_px * mm_per_px, h_px * mm_per_px
            rec.note("pcb_png_page_mm", (round(page_w_mm, 2), round(page_h_mm, 2)))
            if content:
                cx0, cy0, cx1, cy1 = (v * mm_per_px for v in content)
                rec.note("pcb_png_content_mm", tuple(round(v, 2) for v in (cx0, cy0, cx1, cy1)))
                # Is the PNG page in board coordinates (origin = page top-left)?
                dx, dy = cx0 - bbox[0], cy0 - bbox[1]
                rec.note("content_minus_edge_mm", (round(dx, 3), round(dy, 3)))
                rec.check("pixel_mm_mapping_page_coords",
                          abs(dx) < 0.2 and abs(dy) < 0.2
                          and abs((cx1 - cx0) - (bbox[2] - bbox[0])) < 0.2,
                          f"x_mm = px * 25.4 / {DPI}: content {cx0:.2f},{cy0:.2f} vs edge "
                          f"{bbox[0]:.2f},{bbox[1]:.2f}")

    # Fallback path (finding 4): kicad-cli SVG on the saved files
    sch_file = next(proj.glob("*.kicad_sch"))
    pcb_file = next(proj.glob("*.kicad_pcb"))
    r1 = run_cli("sch", "export", "svg", "-o", str(out / "cli_sch"), str(sch_file))
    rec.check("cli_sch_svg", r1.returncode == 0, (r1.stdout + r1.stderr)[-300:])
    r2 = run_cli("pcb", "export", "svg", "--layers", "F.Cu,B.Cu,Edge.Cuts", "--mode-single",
                 "-o", str(out / "cli_pcb.svg"), str(pcb_file))
    rec.check("cli_pcb_svg", r2.returncode == 0, (r2.stdout + r2.stderr)[-300:])
    svg = next(out.rglob("cli_pcb*.svg"), None)
    if svg:
        head = svg.read_text()[:600]
        rec.note("svg_header", head)
        rec.check("svg_units_mm", 'mm"' in head or "viewBox" in head, head[:200])
    rec.note("outputs", sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()))
    rec.note("output_dir", str(out))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
