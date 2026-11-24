"""Shared flow for q03..q08: project -> netlist import -> board setup -> generic placement -> save."""

from __future__ import annotations

import time
from pathlib import Path

from kipy.board_types import BoardSegment
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import DocumentType

import boards
import place

# board size (mm) and copper layers per test board
SPEC = {"ecc83": (62, 48, 2), "odh": (80, 60, 4)}
GAP = 0.3  # courtyard-to-courtyard gap used by the placer (mm)
MARGIN = 0.8  # courtyard-to-edge margin (mm)


def open_board(k, p: boards.Proj):
    k.open_document(str(p.pcb), DocumentType.DOCTYPE_PCB)
    return k.get_board()


def area_and_keepouts(outline, parts):
    x0, y0, x1, y1 = outline
    area = (x0 + MARGIN, y0 + MARGIN, x1 - MARGIN, y1 - MARGIN)
    keep = [p.abox(0.5) for p in parts if p.fixed]
    return area, keep


def place_all(b, outline) -> tuple[list[place.Part], dict]:
    t = {}
    t0 = time.monotonic()
    parts = place.read_parts(b)
    t["read_s"] = round(time.monotonic() - t0, 3)
    for p in parts:
        p.fixed = p.lib == "MountingHole" or "MountingHole" in p.fp.proto.definition.id.entry_name
    glob = place.global_nets(parts)
    t0 = time.monotonic()
    clusters = place.build_clusters(parts, glob)
    for c in clusters:
        place.layout_cluster(c, glob, GAP)
    area, keep = area_and_keepouts(outline, parts)
    t["blocks"] = place.place_blocks(clusters, parts, area, GAP, keep, glob)
    t["place_blocks_s"] = round(time.monotonic() - t0, 3)
    t["refine_moves"] = place.refine(parts, area, GAP, keep, glob)
    place.snap(parts, 0.05)  # on-grid positions (gap 0.3 mm absorbs the <= 0.025 mm shift)
    t["place_s"] = round(time.monotonic() - t0, 3)
    t0 = time.monotonic()
    place.apply(b, parts)
    t["apply_s"] = round(time.monotonic() - t0, 3)
    t["clusters"] = {c.name: [m.ref for m in c.members] for c in clusters}
    t["overlaps"] = place.overlaps([p for p in parts], GAP * 0.99)
    t["outside"] = place.outside(parts, area)
    t["global_nets"] = sorted(glob)
    return parts, t


def prepare(base: Path, name: str, k, *, placed: bool = True):
    """Fresh project copy, empty board -> import -> setup -> (placement). Returns (proj, board, parts, info)."""
    p = boards.ecc83(base) if name == "ecc83" else boards.odh(base)
    net, _ = p.netlist()
    return p, net


def build(k, p: boards.Proj, net: Path, name: str, *, placed: bool = True):
    b = open_board(k, p)
    b.import_netlist(str(net))
    w, h, cu = SPEC[name]
    outline = boards.setup_board(k, b, w, h, copper=cu)
    parts, info = (place_all(b, outline) if placed else ([], {}))
    info["outline"] = outline
    b.save()
    return b, parts, info


def draw_airwires(b, parts, layer=BoardLayer.BL_User_1, skip=frozenset()):
    """Temporary airwire overlay (MST edges) as graphic segments, for renders only. Returns created items."""
    _, _, edges = place.airwire_length(parts, skip)
    segs = []
    for _net, (ax, ay), (bx, by) in edges:
        s = BoardSegment()
        s.layer = layer
        s.start = Vector2.from_xy(round(ax * place.MM), round(ay * place.MM))
        s.end = Vector2.from_xy(round(bx * place.MM), round(by * place.MM))
        s.proto.shape.attributes.stroke.width.value_nm = 80_000
        segs.append(s)
    return b.create_items(segs) if segs else []
