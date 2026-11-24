"""Prototype octilinear (0/45/90 degree) grid A* router for short 2-pin nets on one layer (Q4c).

Obstacles: copper of other nets on the layer (pads incl. THT, tracks, vias) inflated by clearance +
half track width, the board edge; own-net pads are free. State = (cell, direction): turns of 45/90 cost
extra, 135-degree turns are forbidden, so paths never have acute angles. The path is simplified to
segments and committed through the API; the KiCad DRC task is the final oracle."""

from __future__ import annotations

import heapq
import math

from kipy.board_types import Track
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

MM = 1_000_000
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


class Grid:
    def __init__(self, outline, res=0.1):
        self.x0, self.y0, x1, y1 = outline
        self.res = res
        self.w = int((x1 - self.x0) / res) + 1
        self.h = int((y1 - self.y0) / res) + 1
        self.block = bytearray(self.w * self.h)
        self.owner: dict[int, str] = {}  # cell -> net for pad cores (own-net pads are passable)

    def cell(self, x, y):
        return int(round((x - self.x0) / self.res)), int(round((y - self.y0) / self.res))

    def xy(self, i, j):
        return self.x0 + i * self.res, self.y0 + j * self.res

    def mark_rect(self, cx, cy, hw, hh, net, infl):
        i0, j0 = self.cell(cx - hw - infl, cy - hh - infl)
        i1, j1 = self.cell(cx + hw + infl, cy + hh + infl)
        for j in range(max(j0, 0), min(j1, self.h - 1) + 1):
            for i in range(max(i0, 0), min(i1, self.w - 1) + 1):
                k = j * self.w + i
                if self.block[k] == 0 or self.owner.get(k) == net:
                    self.owner[k] = net
                self.block[k] = 1 if self.owner.get(k) == net else 2  # 2 = blocked for all

    def mark_segment(self, ax, ay, bx, by, r, net):
        L = math.hypot(bx - ax, by - ay)
        n = max(int(L / (self.res / 2)), 1)
        for s in range(n + 1):
            x, y = ax + (bx - ax) * s / n, ay + (by - ay) * s / n
            self.mark_rect(x, y, 0, 0, net, r)

    def free(self, i, j, net):
        if not (0 <= i < self.w and 0 <= j < self.h):
            return False
        k = j * self.w + i
        b = self.block[k]
        return b == 0 or (b == 1 and self.owner.get(k) == net)


def build_grid(board, outline, layer, clearance, width, edge=0.3, res=0.1) -> Grid:
    g = Grid(outline, res)
    infl = clearance + width / 2
    # board edge
    for i in range(g.w):
        for j in list(range(0, int((edge + width / 2) / res) + 1)) + list(range(g.h - int((edge + width / 2) / res) - 1, g.h)):
            g.block[j * g.w + i] = 2
    for j in range(g.h):
        for i in list(range(0, int((edge + width / 2) / res) + 1)) + list(range(g.w - int((edge + width / 2) / res) - 1, g.w)):
            g.block[j * g.w + i] = 2
    for p in board.get_pads():
        layers = [cl.layer for cl in p.proto.pad_stack.copper_layers]
        tht = p.proto.pad_stack.drill.diameter.x_nm > 0
        if not tht and layer not in layers and not (layers and layers[0] == BoardLayer.BL_F_Cu and layer == BoardLayer.BL_F_Cu):
            continue
        s = p.proto.pad_stack.copper_layers[0].size if layers else None
        if s is None:
            continue
        ang = p.proto.pad_stack.angle.value_degrees % 180
        hw, hh = s.x_nm / 2 / MM, s.y_nm / 2 / MM
        if 45 < ang < 135:
            hw, hh = hh, hw
        elif ang % 90:  # odd angle: conservative circle
            hw = hh = math.hypot(hw, hh)
        g.mark_rect(p.position.x / MM, p.position.y / MM, hw, hh, p.net.name or f"@{p.id.value}", infl)
    for t in board.get_tracks():
        if t.layer == layer:
            g.mark_segment(t.start.x / MM, t.start.y / MM, t.end.x / MM, t.end.y / MM,
                           infl + t.width / 2 / MM, t.net.name)
    for v in board.get_vias():
        g.mark_rect(v.position.x / MM, v.position.y / MM, v.diameter / 2 / MM, v.diameter / 2 / MM, v.net.name, infl)
    return g


def astar(g: Grid, a, b, net, window=4.0, turn_cost=0.6):
    (si, sj), (ti, tj) = g.cell(*a), g.cell(*b)
    lo_i = min(si, ti) - int(window / g.res)
    hi_i = max(si, ti) + int(window / g.res)
    lo_j = min(sj, tj) - int(window / g.res)
    hi_j = max(sj, tj) + int(window / g.res)

    def h(i, j):
        dx, dy = abs(i - ti), abs(j - tj)
        return (max(dx, dy) - min(dx, dy)) + math.sqrt(2) * min(dx, dy)

    start = (si, sj, -1)
    openh = [(h(si, sj), 0.0, start)]
    came = {start: None}
    cost = {start: 0.0}
    while openh:
        _, c, (i, j, d) = heapq.heappop(openh)
        if (i, j) == (ti, tj):
            path = []
            s = (i, j, d)
            while s:
                path.append(g.xy(s[0], s[1]))
                s = came[s]
            return path[::-1]
        if c > cost.get((i, j, d), 1e18):
            continue
        for nd, (di, dj) in enumerate(DIRS):
            if d >= 0:
                turn = min((nd - d) % 8, (d - nd) % 8)
                if turn > 2:  # > 90 degrees: would make an acute angle
                    continue
            else:
                turn = 0
            ni, nj = i + di, j + dj
            if not (lo_i <= ni <= hi_i and lo_j <= nj <= hi_j) or not g.free(ni, nj, net):
                continue
            nc = c + (1.4142 if di and dj else 1.0) + turn * turn_cost
            st = (ni, nj, nd)
            if nc < cost.get(st, 1e18):
                cost[st] = nc
                came[st] = (i, j, d)
                heapq.heappush(openh, (nc + h(ni, nj), nc, st))
    return None


def simplify(path):
    out = [path[0]]
    for k in range(1, len(path) - 1):
        ax, ay = out[-1]
        bx, by = path[k]
        cx, cy = path[k + 1]
        if abs((bx - ax) * (cy - by) - (by - ay) * (cx - bx)) > 1e-9:
            out.append(path[k])
    out.append(path[-1])
    return out


def to_tracks(path, net, layer, width):
    out = []
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        t = Track()
        t.start = Vector2.from_xy(round(ax * MM), round(ay * MM))
        t.end = Vector2.from_xy(round(bx * MM), round(by * MM))
        t.layer, t.width, t.net = layer, round(width * MM), net
        out.append(t)
    return out


def _line_meet(p0, e, p1, d):
    """Point on p0 + s*e that also lies on p1 + t*d (None if parallel)."""
    den = e[0] * d[1] - e[1] * d[0]
    if abs(den) < 1e-12:
        return None
    s = ((p1[0] - p0[0]) * d[1] - (p1[1] - p0[1]) * d[0]) / den
    return (p0[0] + s * e[0], p0[1] + s * e[1])


def fix_ends(path, a, b):
    """Grid endpoints -> exact pad centres while keeping every segment octilinear (moves the last bend)."""
    path = list(path)
    for end, exact in ((-1, b), (0, a)):
        if len(path) < 3:
            path[end] = exact
            continue
        q, r_ = (path[-2], path[-3]) if end == -1 else (path[1], path[2])
        g = path[end]
        d = (g[0] - q[0], g[1] - q[1])
        e = (q[0] - r_[0], q[1] - r_[1])
        m = _line_meet(r_, e, exact, d)
        if m is not None:
            if end == -1:
                path[-2] = m
            else:
                path[1] = m
        path[end] = exact
    return path
