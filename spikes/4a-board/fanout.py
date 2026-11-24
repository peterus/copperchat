"""Generic plane fanout: connect every SMD pad of a plane net to the planes with a short 45/90-degree
stub + via, legalised by the KiCad DRC task (try candidate directions; keep what DRC accepts).
THT pads of the net reach inner planes through their hole and need nothing."""

from __future__ import annotations

import math

from kipy.board_types import Track, Via
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer, PadType

import drc

MM = 1_000_000
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]


def _pad_extent(pad) -> float:
    cl = pad.proto.pad_stack.copper_layers
    if not cl:
        return 0.5
    s = cl[0].size
    return max(s.x_nm, s.y_nm) / 2 / MM


def fanout(board, net_name: str, *, via_d=0.6, drill=0.3, width=0.3, gap=0.45) -> dict:
    net = next(n for n in board.get_nets() if n.name == net_name)
    pads = [p for p in board.get_pads() if p.net.name == net_name and p.pad_type == PadType.PT_SMD]
    todo = {p.id.value: p for p in pads}
    attempt = {k: 0 for k in todo}
    kept = 0
    rounds = 0
    while todo and rounds < len(DIRS) * 2:
        rounds += 1
        batch = []
        owners = []
        for pid, p in list(todo.items()):
            i = attempt[pid]
            if i >= len(DIRS) * 2:
                del todo[pid]
                continue
            dx, dy = DIRS[i % len(DIRS)]
            dist = _pad_extent(p) + gap + via_d / 2 + (0.5 if i >= len(DIRS) else 0.0)
            n = math.hypot(dx, dy)
            vx = p.position.x / MM + dx / n * dist
            vy = p.position.y / MM + dy / n * dist
            # snap so the stub stays exactly 0/45/90 degrees
            if dx and dy:
                d = dist / math.sqrt(2)
                vx, vy = p.position.x / MM + dx * d, p.position.y / MM + dy * d
            v = Via()
            v.position = Vector2.from_xy(round(vx * MM), round(vy * MM))
            v.net, v.diameter, v.drill_diameter = net, round(via_d * MM), round(drill * MM)
            t = Track()
            t.start, t.end = p.position, v.position
            t.layer, t.width, t.net = BoardLayer.BL_F_Cu, round(width * MM), net
            batch += [v, t]
            owners += [pid, pid]
        if not batch:
            break
        made = board.create_items(batch)
        board.refill_zones(block=True, max_poll_seconds=120)  # the via must land in the filled planes
        viol, _ = drc.api_drc(board, zones_filled=True)
        ids = {m.id.value: owners[i] for i, m in enumerate(made)}
        bad_owners = {ids[i.value] for x in viol for i in x.items if i.value in ids
                      and drc.board_rules_pb2.DrcErrorType.Name(x.error_type) not in ("DRCET_UNCONNECTED_ITEMS",)}
        remove = [m for i, m in enumerate(made) if owners[i] in bad_owners]
        if remove:
            board.remove_items(remove)
        for pid in set(owners):
            if pid in bad_owners:
                attempt[pid] += 1
            else:
                kept += 1
                todo.pop(pid, None)
    return {"smd_pads": len(pads), "fanned_out": kept, "failed": len(todo), "drc_rounds": rounds}
