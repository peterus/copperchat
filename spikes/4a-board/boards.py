"""Test boards for spike 4A (runs inside the kicad-base image).

* ``ecc83(base)``  — KiCad demo ``ecc83-pp`` (15 parts, 1 sheet, local ``footprints.pretty``):
  schematic + project copied from ``/usr/share/kicad/demos``, board replaced by an EMPTY board
  created by KiCad (``CreateDocument`` + save). The demo's routed board is kept as
  ``ecc83-ref.kicad_pcb`` (a copy of a KiCad-written file) as a human-routed reference.
* ``odh(base)``    — copy of ``fixtures/odh`` (OpenDriveHub transmitter main, ~160 parts, 4 sheets,
  ``${KIPRJMOD}/../../../../footprints/OpenDriveHub.pretty``); its board is already empty.

No KiCad file is written by hand; only copies of KiCad-written files.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from kipy.proto.common.types import DocumentType

from harness import HERE, Server, cli

DEMO = Path("/usr/share/kicad/demos/ecc83")


@dataclass
class Proj:
    pro: Path

    @property
    def dir(self) -> Path:
        return self.pro.parent

    @property
    def sch(self) -> Path:
        return self.pro.with_suffix(".kicad_sch")

    @property
    def pcb(self) -> Path:
        return self.pro.with_suffix(".kicad_pcb")

    def netlist(self, out: Path | None = None) -> tuple[Path, float]:
        """kicad-cli sch export netlist (KiCad s-expression). Returns (path, seconds)."""
        out = out or self.dir / "netlist.net"
        t0 = time.monotonic()
        r = cli("sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(self.sch))
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"netlist export failed: {r.stdout}{r.stderr}")
        return out, time.monotonic() - t0


def _clean_locks(root: Path) -> None:
    for lck in root.rglob("*.lck"):
        lck.unlink()


def ecc83(base: Path) -> Proj:
    d = base / "ecc83"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for name in ("ecc83-pp.kicad_pro", "ecc83-pp.kicad_sch", "ecc83-pp.kicad_sym", "fp-lib-table", "sym-lib-table"):
        shutil.copyfile(DEMO / name, d / name)
    shutil.copytree(DEMO / "footprints.pretty", d / "footprints.pretty")
    shutil.copyfile(DEMO / "ecc83-pp.kicad_pcb", d / "ecc83-ref.kicad_pcb")
    p = Proj(d / "ecc83-pp.kicad_pro")
    with Server(None) as srv:  # an EMPTY board, written by KiCad
        k = srv.client(60000)
        k.create_document(str(p.pcb), DocumentType.DOCTYPE_PCB)
        k.get_board().save()
    _clean_locks(d)
    return p


def odh(base: Path) -> Proj:
    src = HERE / "fixtures" / "odh"
    if not src.exists():
        raise RuntimeError("fixtures/odh missing: run ./fetch_odh.sh on the host first")
    d = base / "odh"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(src, d)
    _clean_locks(d)
    return Proj(d / "hardware" / "transmitter" / "pcb" / "main" / "main.kicad_pro")


# --- shared board setup (q02 path) + render ---------------------------------------------------------

MM = 1_000_000
HOLE = "MountingHole:MountingHole_2.2mm_M2"


def setup_board(k, b, w_mm: float, h_mm: float, copper: int = 2, x0_mm: float = 100, y0_mm: float = 100,
                radius_mm: float = 2.0, hole_inset_mm: float = 3.0) -> tuple[float, float, float, float]:
    """Outline (rounded rect on Edge.Cuts), 4 M2 corner holes (H1..H4, locked), copper layer count,
    JLCPCB-ish minimum rules. Returns the outline (x0, y0, x1, y1) in mm."""
    from kipy.board_types import BoardRectangle
    from kipy.geometry import Vector2
    from kipy.proto.board import board_commands_pb2, board_rules_pb2
    from kipy.proto.board.board_types_pb2 import BoardLayer

    x0, y0, x1, y1 = x0_mm, y0_mm, x0_mm + w_mm, y0_mm + h_mm
    rect = BoardRectangle()
    rect.layer = BoardLayer.BL_Edge_Cuts
    rect.top_left = Vector2.from_xy(round(x0 * MM), round(y0 * MM))
    rect.bottom_right = Vector2.from_xy(round(x1 * MM), round(y1 * MM))
    rect.proto.shape.rectangle.corner_radius.value_nm = round(radius_mm * MM)
    rect.proto.shape.attributes.stroke.width.value_nm = 50_000
    b.create_items(rect)
    corners = [(x0 + hole_inset_mm, y0 + hole_inset_mm), (x1 - hole_inset_mm, y0 + hole_inset_mm),
               (x0 + hole_inset_mm, y1 - hole_inset_mm), (x1 - hole_inset_mm, y1 - hole_inset_mm)]
    # mounting holes that come from the schematic go to the corners; otherwise add H1..H4
    existing = [f for f in b.get_footprints() if "MountingHole" in f.proto.definition.id.entry_name
                or f.proto.definition.id.library_nickname == "MountingHole"]
    if len(existing) >= 4:
        for f, (hx, hy) in zip(sorted(existing, key=lambda f: f.reference_field.text.value), corners):
            f.position = Vector2.from_xy(round(hx * MM), round(hy * MM))
            f.locked = True
        b.update_items(existing[:4])
    else:
        k.get_library_footprint(HOLE)
        for i, (hx, hy) in enumerate(corners):
            fp = b.place_footprint_from_library(HOLE, Vector2.from_xy(round(hx * MM), round(hy * MM)))
            fp.reference_field.text.value = f"H{i + 1}"
            fp.locked = True
            b.update_items(fp)
    if copper != 2:
        non_cu = [ly for ly in b.get_enabled_layers() if ly not in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu)]
        b.set_enabled_layers(copper, non_cu)
    dr = b.get_design_rules()
    rules = board_rules_pb2.BoardDesignRules()
    rules.CopyFrom(dr.proto.rules if hasattr(dr.proto, "rules") else dr.proto)
    c = rules.constraints
    for fld, mm in (("min_clearance", 0.127), ("min_track_width", 0.127), ("min_via_size", 0.45),
                    ("min_through_drill", 0.3), ("min_via_annular_width", 0.075), ("copper_edge_clearance", 0.3),
                    ("hole_to_hole_min", 0.5), ("hole_clearance", 0.25)):
        getattr(c, fld).value_nm = round(mm * MM)
    b.client.send(board_commands_pb2.SetBoardDesignRules(board=b.document, rules=rules),
                  board_commands_pb2.BoardDesignRulesResponse)
    return (x0, y0, x1, y1)


def render(pcb: Path, out_svg: Path, layers: str = "B.Cu,F.Cu,F.SilkS,Edge.Cuts") -> float:
    """Composite board-area SVG via kicad-cli (`--mode-single --page-size-mode 2`); layers are drawn in
    the given order (last on top). run.sh rasterises results/*.svg to PNG on the host. Returns seconds."""
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    r = cli("pcb", "export", "svg", "--mode-single", "--page-size-mode", "2", "--exclude-drawing-sheet",
            "--layers", layers, "-o", str(out_svg), str(pcb), timeout=300)
    if not out_svg.exists():
        raise RuntimeError(f"render failed: {r.stdout}{r.stderr}")
    return time.monotonic() - t0
