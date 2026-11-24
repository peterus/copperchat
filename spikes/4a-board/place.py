"""Generic placement prototype for spike 4A (no design-specific rules).

1. Read parts from the board: courtyard box (F.CrtYd shapes, else item bbox) and pad offsets with nets.
2. Global nets (ground/supplies, detected by pad count or name) do not pull parts together.
3. Clusters: every "anchor" (>= 5 pads: ICs, modules, connectors) takes the small parts (<= 4 pads)
   it shares the most signal nets with; a small part on a supply net + ground next to an anchor on the
   same supply is a decoupling cap and joins that anchor. Leftovers chain via signal nets, else go to
   the biggest anchor of their schematic sheet.
4. Cluster layout: anchor at the origin, each satellite on the side of the anchor pad it connects to,
   first free spot outward (rotated so its connected pad faces the anchor).
5. Sheet blocks: clusters of one sheet shelf-packed; blocks packed into the board (largest first,
   ordered by inter-sheet connectivity); a legalizer pushes overlapping courtyards apart and clamps
   everything inside the board outline minus a margin and the keepout boxes (mounting holes).

Metrics: total airwire length (per-net Euclidean MST over pads), HPWL, overlaps, parts outside.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from kipy.geometry import Angle, Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

MM = 1_000_000
GLOBAL_NET_RE = re.compile(r"^/?(GND|AGND|DGND|PGND|VCC|VDD|VSS|VBUS|VBAT|VSYS|[+-]?\d+V\d*|\+?3V3|\+?5V|\+?1V8)$", re.I)


@dataclass
class Pad:
    net: str
    dx: float  # mm, relative to footprint position at rotation 0
    dy: float


@dataclass
class Part:
    ref: str
    sheet: str
    lib: str
    box: tuple[float, float, float, float]  # courtyard x0,y0,x1,y1 relative to position, rotation 0
    pads: list[Pad]
    fixed: bool = False
    x: float = 0.0
    y: float = 0.0
    rot: int = 0  # degrees, multiple of 90
    cluster: str = ""
    fp: object = None

    def rbox(self, rot: int | None = None) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.box
        pts = [_rot(x, y, self.rot if rot is None else rot) for x, y in ((x0, y0), (x1, y1))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def abox(self, gap: float = 0.0) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.rbox()
        return (self.x + x0 - gap, self.y + y0 - gap, self.x + x1 + gap, self.y + y1 + gap)

    def pad_pos(self, pad: Pad) -> tuple[float, float]:
        dx, dy = _rot(pad.dx, pad.dy, self.rot)
        return self.x + dx, self.y + dy

    @property
    def w(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]


def _rot(x: float, y: float, deg: int) -> tuple[float, float]:
    # KiCad: positive angle = counter-clockwise on screen, y axis points down
    d = deg % 360
    if d == 0:
        return x, y
    if d == 90:
        return y, -x
    if d == 180:
        return -x, -y
    return -y, x


def read_parts(board) -> list[Part]:
    fps = board.get_footprints()
    pads_by_parent: dict[str, list] = defaultdict(list)
    for p in board.get_pads():
        pads_by_parent[p.proto.parent.value].append(p)
    parts = []
    for f in fps:
        fx, fy = f.position.x / MM, f.position.y / MM
        crt = [s for s in f.definition.shapes if s.layer in (BoardLayer.BL_F_CrtYd, BoardLayer.BL_B_CrtYd)]
        if crt:
            bbs = [s.bounding_box() for s in crt]
            x0 = min(b.pos.x for b in bbs) / MM - fx
            y0 = min(b.pos.y for b in bbs) / MM - fy
            x1 = max(b.pos.x + b.size.x for b in bbs) / MM - fx
            y1 = max(b.pos.y + b.size.y for b in bbs) / MM - fy
        else:
            bb = board.get_item_bounding_box(f)
            x0, y0 = bb.pos.x / MM - fx, bb.pos.y / MM - fy
            x1, y1 = x0 + bb.size.x / MM, y0 + bb.size.y / MM
        rot = round(f.orientation.degrees) % 360
        if rot % 90:
            rot = 0  # prototype handles multiples of 90 only
        # courtyard box back to rotation 0 (Part.rbox applies the rotation again)
        c = [_rot(x, y, -rot) for x, y in ((x0, y0), (x1, y1))]
        x0, y0 = min(c[0][0], c[1][0]), min(c[0][1], c[1][1])
        x1, y1 = max(c[0][0], c[1][0]), max(c[0][1], c[1][1])
        pads = []
        for p in pads_by_parent.get(f.id.value, []):
            dx, dy = p.position.x / MM - fx, p.position.y / MM - fy
            dx, dy = _rot(dx, dy, -rot)
            pads.append(Pad(p.net.name, dx, dy))
        ref = f.reference_field.text.value
        parts.append(Part(ref, f.proto.symbol_sheet_name or "/", f.proto.definition.id.library_nickname,
                          (x0, y0, x1, y1), pads, x=fx, y=fy, rot=rot, fp=f))
    return parts


def global_nets(parts: list[Part], max_pads: int = 10) -> set[str]:
    count: dict[str, int] = defaultdict(int)
    for p in parts:
        for q in p.pads:
            if q.net:
                count[q.net] += 1
    names = {n for n, c in count.items() if c > max_pads}
    names |= {n for n in count if GLOBAL_NET_RE.match(n.split("/")[-1])}
    return names


def is_ground(net: str) -> bool:
    return bool(re.search(r"GND|VSS", net, re.I))


def airwire_length(parts: list[Part], skip: set[str] = frozenset()) -> tuple[float, float, list]:
    """(sum of per-net Euclidean MSTs, HPWL, MST edges). Pads of one part count as one node per pad."""
    by_net: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in parts:
        for q in p.pads:
            if q.net and q.net not in skip and not q.net.startswith("unconnected-"):
                by_net[q.net].append(p.pad_pos(q))
    total = hpwl = 0.0
    edges = []
    for net, pts in by_net.items():
        if len(pts) < 2:
            continue
        xs, ys = [a for a, _ in pts], [b for _, b in pts]
        hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))
        # Prim
        inside = {0}
        dist = {i: math.dist(pts[0], pts[i]) for i in range(1, len(pts))}
        src = dict.fromkeys(dist, 0)
        while dist:
            j = min(dist, key=dist.get)
            total += dist[j]
            edges.append((net, pts[src[j]], pts[j]))
            inside.add(j)
            del dist[j]
            for i in dist:
                d = math.dist(pts[j], pts[i])
                if d < dist[i]:
                    dist[i], src[i] = d, j
    return total, hpwl, edges


def overlaps(parts: list[Part], gap: float = 0.0) -> list[tuple[str, str]]:
    out = []
    for i, a in enumerate(parts):
        ax0, ay0, ax1, ay1 = a.abox(gap / 2)
        for b in parts[i + 1:]:
            bx0, by0, bx1, by1 = b.abox(gap / 2)
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                out.append((a.ref, b.ref))
    return out


def outside(parts: list[Part], area: tuple[float, float, float, float]) -> list[str]:
    x0, y0, x1, y1 = area
    e = 1e-6
    return [p.ref for p in parts if not p.fixed and (lambda b: b[0] < x0 - e or b[1] < y0 - e or b[2] > x1 + e or b[3] > y1 + e)(p.abox())]


@dataclass
class Cluster:
    name: str
    anchor: Part
    members: list[Part] = field(default_factory=list)

    def bbox(self, gap: float) -> tuple[float, float, float, float]:
        bs = [m.abox(gap) for m in self.members]
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    def move(self, dx: float, dy: float) -> None:
        for m in self.members:
            m.x += dx
            m.y += dy


def build_clusters(parts: list[Part], glob: set[str]) -> list[Cluster]:
    movable = [p for p in parts if not p.fixed]
    anchors = [p for p in movable if len(p.pads) >= 5]
    small = [p for p in movable if len(p.pads) < 5]
    sig = {p.ref: {q.net for q in p.pads if q.net and q.net not in glob} for p in movable}
    allnets = {p.ref: {q.net for q in p.pads if q.net} for p in movable}
    clusters = {a.ref: Cluster(a.ref, a, [a]) for a in anchors}
    unassigned = []
    for s in small:
        best, score = None, 0.0
        for a in anchors:
            sc = len(sig[s.ref] & sig[a.ref]) * 2.0
            # decoupling: supply + ground on the small part, same supply on the anchor
            supplies = {n for n in allnets[s.ref] & allnets[a.ref] if n in glob and not is_ground(n)}
            if supplies and any(is_ground(n) for n in allnets[s.ref]):
                sc += 1.0
            if a.sheet == s.sheet:
                sc += 0.25
            if sc > score:
                best, score = a, sc
        if best is not None and score >= 1.0:
            clusters[best.ref].members.append(s)
            s.cluster = best.ref
        else:
            unassigned.append(s)
    # chain leftovers through signal nets to an already clustered small part
    changed = True
    while changed and unassigned:
        changed = False
        for s in list(unassigned):
            for c in clusters.values():
                if any(sig[s.ref] & sig[m.ref] for m in c.members):
                    c.members.append(s)
                    s.cluster = c.name
                    unassigned.remove(s)
                    changed = True
                    break
    for s in unassigned:  # biggest anchor of the same sheet, else a cluster of its own
        same = [a for a in anchors if a.sheet == s.sheet]
        if same:
            a = max(same, key=lambda a: len(a.pads))
            clusters[a.ref].members.append(s)
            s.cluster = a.ref
        else:
            clusters[s.ref] = Cluster(s.ref, s, [s])
            s.cluster = s.ref
    for a in anchors:
        a.cluster = a.ref
    return list(clusters.values())


def _free(p: Part, placed: list[Part], gap: float) -> bool:
    ax0, ay0, ax1, ay1 = p.abox(gap / 2)
    for q in placed:
        bx0, by0, bx1, by1 = q.abox(gap / 2)
        if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
            return False
    return True


_SPIRAL = sorted(((i * 0.25, j * 0.25) for i in range(-60, 61) for j in range(-60, 61)),
                 key=lambda t: (math.hypot(*t), t))


def layout_cluster(c: Cluster, glob: set[str], gap: float) -> None:
    a = c.anchor
    a.x = a.y = 0.0
    a.rot = 0
    placed = [a]
    ax0, ay0, ax1, ay1 = a.abox()
    rest = [m for m in c.members if m is not a]

    def target(s: Part) -> tuple[float, float]:
        # the anchor pad(s) this part connects to (signal nets first, then supplies)
        nets = {q.net for q in s.pads if q.net}
        pads = [q for q in a.pads if q.net in nets and q.net not in glob] or \
               [q for q in a.pads if q.net in nets and not is_ground(q.net)]
        if not pads:
            return (ax1 + 1.0, 0.0)
        # pads of one net on several sides (e.g. a reference net on both rows): use the first one
        near = [q for q in pads if math.dist((q.dx, q.dy), (pads[0].dx, pads[0].dy)) < 3.0]
        return (sum(q.dx for q in near) / len(near), sum(q.dy for q in near) / len(near))

    rest.sort(key=lambda s: -len({q.net for q in s.pads} & {q.net for q in a.pads}))
    for s in rest:
        tx, ty = target(s)
        # outward direction from the anchor centre through the target pad
        cx, cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
        dx, dy = tx - cx, ty - cy
        n = math.hypot(dx, dy) or 1.0
        dx, dy = dx / n, dy / n
        # rotation: put the part's pad axis radial (2-pad parts)
        horiz = abs(dx) >= abs(dy)
        best = None
        for rot in ((0, 180) if horiz else (90, 270)) if len(s.pads) == 2 else (0, 90, 180, 270):
            s.rot = rot
            # the pad sharing a net with the anchor should face the anchor
            shared = [q for q in s.pads if q.net and q.net in {p.net for p in a.pads} and not is_ground(q.net)]
            if shared:
                px, py = _rot(shared[0].dx, shared[0].dy, rot)
                if px * dx + py * dy > 0:  # pad points away from the anchor
                    continue
            best = rot
            break
        s.rot = best if best is not None else 0
        # start just outside the anchor courtyard on the target pad's side, then nearest free spot
        if dx == 0 and dy == 0:
            dx = 1.0
        sx, sy = tx, ty
        while ax0 - 0.5 < sx < ax1 + 0.5 and ay0 - 0.5 < sy < ay1 + 0.5:
            sx += dx * 0.25
            sy += dy * 0.25
        for ox, oy in _SPIRAL:
            s.x, s.y = sx + ox, sy + oy
            if _free(s, placed, gap):
                break
        placed.append(s)


def is_edge_part(p: Part) -> bool:
    """Generic: connectors (by library class) prefer the board edge. Pin headers are left free."""
    return p.lib.startswith("Connector") and "PinHeader" not in p.lib and "PinSocket" not in p.lib


def _rotate_cluster(c: Cluster, rot: int) -> list[tuple[float, float, int]]:
    """Member (x, y, rot) after rotating the whole cluster by `rot` about the anchor origin."""
    out = []
    for m in c.members:
        x, y = _rot(m.x, m.y, rot)
        out.append((x, y, (m.rot + rot) % 360))
    return out


def _boxes_free(boxes, placed_boxes) -> bool:
    for ax0, ay0, ax1, ay1 in boxes:
        for bx0, by0, bx1, by1 in placed_boxes:
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                return False
    return True


def place_blocks(clusters: list[Cluster], parts: list[Part], area, gap: float, keepouts, glob: set[str],
                 step: float = 1.0) -> dict:
    """Greedy constructive placement of rigid cluster blocks; legal by construction.

    Order: biggest block first, then always the block most connected to what is placed. Each block is
    tried at every grid position x 4 rotations inside the area; cost = signal-net distance to placed
    pads (per-net centroids) + pull to its sheet's placed centroid (grouping) + edge pull for connectors.
    A block that does not fit anywhere is split into single parts."""
    x0, y0, x1, y1 = area
    placed_boxes = [p.abox(gap / 2) for p in parts if p.fixed] + list(keepouts)
    net_pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    sheet_pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    stats = {"split": [], "evaluated": 0}

    def block_cost(members, poses) -> float:
        cost = 0.0
        for m, (mx, my, mr) in zip(members, poses):
            for q in m.pads:
                if q.net and q.net not in glob and q.net in net_pts:
                    px, py = _rot(q.dx, q.dy, mr)
                    pts = net_pts[q.net]
                    cx = sum(a for a, _ in pts) / len(pts)
                    cy = sum(b for _, b in pts) / len(pts)
                    cost += abs(mx + px - cx) + abs(my + py - cy)
            if is_edge_part(m):
                rb = _rbox(m, mr)
                d = min(mx + rb[0] - x0, y1 - (my + rb[3]), x1 - (mx + rb[2]), my + rb[1] - y0)
                cost += 3.0 * max(d, 0.0)
        sp = sheet_pts.get(members[0].sheet)
        if sp:
            cx = sum(a for a, _ in sp) / len(sp)
            cy = sum(b for _, b in sp) / len(sp)
            mx = sum(p[0] for p in poses) / len(poses)
            my = sum(p[1] for p in poses) / len(poses)
            cost += 0.5 * len(members) * (abs(mx - cx) + abs(my - cy))
        return cost

    def commit(members, poses):
        for m, (mx, my, mr) in zip(members, poses):
            m.x, m.y, m.rot = mx, my, mr
            placed_boxes.append(m.abox(gap / 2))
            sheet_pts[m.sheet].append((mx, my))
            for q in m.pads:
                if q.net:
                    net_pts[q.net].append(m.pad_pos(q))

    def try_place(members, rel_poses_by_rot, step=step) -> bool:
        best = None
        for rel in rel_poses_by_rot:
            rbs = [_rbox(m, r) for m, (_, _, r) in zip(members, rel)]
            bx0 = min(rx + b[0] for (rx, _, _), b in zip(rel, rbs))
            by0 = min(ry + b[1] for (_, ry, _), b in zip(rel, rbs))
            bx1 = max(rx + b[2] for (rx, _, _), b in zip(rel, rbs))
            by1 = max(ry + b[3] for (_, ry, _), b in zip(rel, rbs))
            if bx1 - bx0 > x1 - x0 or by1 - by0 > y1 - y0:
                continue
            ox = x0 - bx0
            while ox + bx1 <= x1 + 1e-9:
                oy = y0 - by0
                while oy + by1 <= y1 + 1e-9:
                    boxes = [(ox + rx + b[0] - gap / 2, oy + ry + b[1] - gap / 2, ox + rx + b[2] + gap / 2, oy + ry + b[3] + gap / 2)
                             for (rx, ry, _), b in zip(rel, rbs)]
                    stats["evaluated"] += 1
                    if _boxes_free(boxes, placed_boxes):
                        poses = [(ox + rx, oy + ry, r) for rx, ry, r in rel]
                        c = block_cost(members, poses)
                        if not net_pts and not sheet_pts:  # first block: centre of the area
                            cx = sum(p[0] for p in poses) / len(poses)
                            cy = sum(p[1] for p in poses) / len(poses)
                            c = abs(cx - (x0 + x1) / 2) + abs(cy - (y0 + y1) / 2)
                        if best is None or c < best[0]:
                            best = (c, poses)
                    oy += step
                ox += step
        if best is None:
            return False
        commit(members, best[1])
        return True

    def area_of(c):
        return sum(m.w * m.h for m in c.members)

    todo = sorted(clusters, key=lambda c: -area_of(c))
    first = True
    while todo:
        if first:
            c = todo.pop(0)
            first = False
        else:
            def links(c):
                return sum(1 for m in c.members for q in m.pads if q.net and q.net not in glob and q.net in net_pts)
            # big blocks first (they are the hard ones to fit), then connectivity
            big = [c for c in todo if area_of(c) > 0.04 * (x1 - x0) * (y1 - y0)]
            c = max(big or todo, key=lambda c: (links(c), area_of(c)))
            todo.remove(c)
        rels = [_rotate_cluster(c, r) for r in (0, 90, 180, 270)]
        if not try_place(c.members, rels):
            stats["split"].append(c.name)
            for m in sorted(c.members, key=lambda m: -len(m.pads)):
                rels1 = [[(0.0, 0.0, r)] for r in (0, 90, 180, 270)]
                if not try_place([m], rels1) and not try_place([m], rels1, step=0.25):
                    stats.setdefault("unplaced", []).append(m.ref)
    return stats


def _rbox(m: Part, rot: int):
    return m.rbox(rot)


def refine(parts: list[Part], area, gap: float, keepouts, glob: set[str], passes: int = 2,
           window: float = 3.0, step: float = 0.25) -> int:
    """Move small parts (<= 4 pads) to the legal spot in a local window that minimises their airwire."""
    moves = 0
    fixed_boxes = [p.abox(gap / 2) for p in parts if p.fixed] + list(keepouts)
    net_parts: dict[str, list[Part]] = defaultdict(list)
    for p in parts:
        for q in p.pads:
            if q.net and q.net not in glob:
                net_parts[q.net].append(p)

    def cost(p: Part) -> float:
        c = 0.0
        for q in p.pads:
            if not q.net or q.net in glob:
                continue
            px, py = p.pad_pos(q)
            others = [o.pad_pos(oq) for o in net_parts[q.net] if o is not p for oq in o.pads if oq.net == q.net]
            if others:
                c += min(math.dist((px, py), o) for o in others)
        return c

    for _ in range(passes):
        for p in parts:
            if p.fixed or len(p.pads) > 4:
                continue
            others = [o.abox(gap / 2) for o in parts if o is not p and not o.fixed] + fixed_boxes
            best = (cost(p), p.x, p.y, p.rot)
            ox, oy, orot = p.x, p.y, p.rot
            n = int(window / step)
            for rot in (0, 90, 180, 270):
                for i in range(-n, n + 1):
                    for j in range(-n, n + 1):
                        p.x, p.y, p.rot = ox + i * step, oy + j * step, rot
                        b = p.abox()
                        if b[0] < area[0] or b[1] < area[1] or b[2] > area[2] or b[3] > area[3]:
                            continue
                        if not _boxes_free([p.abox(gap / 2)], others):
                            continue
                        c = cost(p) + 0.01 * (abs(i) + abs(j)) * step
                        if c < best[0] - 1e-6:
                            best = (c, p.x, p.y, p.rot)
            p.x, p.y, p.rot = best[1], best[2], best[3]
            if (p.x, p.y, p.rot) != (ox, oy, orot):
                moves += 1
    return moves


def snap(parts: list[Part], grid: float = 0.05) -> None:
    for p in parts:
        if not p.fixed:
            p.x = round(p.x / grid) * grid
            p.y = round(p.y / grid) * grid


def apply(board, parts: list[Part]) -> None:
    fps = []
    for p in parts:
        if p.fixed:
            continue
        f = p.fp
        f.position = Vector2.from_xy(round(p.x * MM), round(p.y * MM))
        f.orientation = Angle.from_degrees(p.rot)
        fps.append(f)
    board.update_items(fps)
