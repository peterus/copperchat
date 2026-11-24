"""Generic routing/placement quality metrics from API data (Q6). No design-specific knowledge:
parts are classified by pad count and nets by name/pad count only."""

from __future__ import annotations

import math
from collections import defaultdict

from kipy.board_types import ArcTrack, Track

import place

MM = 1_000_000
EPS = 0.002  # mm, endpoint coincidence


def _k(x: float, y: float) -> tuple[int, int]:
    return (round(x / EPS), round(y / EPS))


def routing(board, parts: list[place.Part]) -> dict:
    tracks = board.get_tracks()
    vias = board.get_vias()
    segs = [t for t in tracks if isinstance(t, Track)]
    arcs = [t for t in tracks if isinstance(t, ArcTrack)]
    glob = place.global_nets(parts)

    # endpoints per net: tracks, vias, pads
    ends: dict[tuple[str, tuple[int, int]], int] = defaultdict(int)
    dirs: dict[tuple[str, tuple[int, int], int], list[float]] = defaultdict(list)
    length: dict[str, float] = defaultdict(float)
    off45 = []
    for t in segs:
        net = t.net.name
        ax, ay, bx, by = t.start.x / MM, t.start.y / MM, t.end.x / MM, t.end.y / MM
        L = math.hypot(bx - ax, by - ay)
        length[net] += L
        if L < 1e-6:
            continue
        ang = math.degrees(math.atan2(by - ay, bx - ax)) % 45.0
        if min(ang, 45.0 - ang) > 0.5:
            off45.append((net, round(L, 2)))
        for (px, py), (qx, qy) in (((ax, ay), (bx, by)), ((bx, by), (ax, ay))):
            ends[(net, _k(px, py))] += 1
            dirs[(net, _k(px, py), t.layer)].append(math.degrees(math.atan2(qy - py, qx - px)))
    for a in arcs:
        length[a.net.name] += a.length() / MM if hasattr(a, "length") else 0.0
        for v in (a.start, a.end):
            ends[(a.net.name, _k(v.x / MM, v.y / MM))] += 1
    anchors: set[tuple[str, tuple[int, int]]] = set()
    via_count: dict[str, int] = defaultdict(int)
    for v in vias:
        anchors.add((v.net.name, _k(v.position.x / MM, v.position.y / MM)))
        via_count[v.net.name] += 1
    pad_shapes = []
    for p in board.get_pads():
        anchors.add((p.net.name, _k(p.position.x / MM, p.position.y / MM)))
        pad_shapes.append((p.net.name, p.position.x / MM, p.position.y / MM))

    # bends: direction change at a 2-track junction (same layer); 0/45/90 ok, > 90 = acute
    bends = defaultdict(int)
    acute = 0
    for (net, key, _layer), ds in dirs.items():
        if len(ds) != 2:
            continue
        inner = abs((ds[0] - ds[1] + 180) % 360 - 180)  # angle between the two outgoing directions
        change = round(180 - inner)
        bends[change] += 1
        if inner < 89.5:
            acute += 1
    # dangling ends: a track end touching nothing else (no other track, via or pad centre nearby)
    dangling = 0
    for (net, key), n in ends.items():
        if n == 1 and (net, key) not in anchors:
            x, y = key[0] * EPS, key[1] * EPS
            if not any(pn == net and math.hypot(px - x, py - y) < 0.8 for pn, px, py in pad_shapes):
                dangling += 1

    # detour: routed length / MST airwire length per net (signal nets with >= 2 pads)
    L_air = {}
    _, _, edges = place.airwire_length(parts)
    for net, a, b in edges:
        L_air[net] = L_air.get(net, 0.0) + math.dist(a, b)
    detour = {n: length[n] / L_air[n] for n in L_air if n not in glob and L_air[n] > 0.5 and length.get(n, 0) > 0}
    worst = sorted(detour.items(), key=lambda kv: -kv[1])[:8]
    longest = sorted(((n, round(length[n], 1)) for n in length if n not in glob), key=lambda kv: -kv[1])[:5]
    return {
        "segments": len(segs), "arcs": len(arcs), "vias": len(vias),
        "track_length_mm": round(sum(length.values()), 1),
        "non_45_segments": len(off45), "non_45_examples": off45[:5],
        "bend_histogram": dict(sorted(bends.items())), "acute_bends": acute,
        "dangling_ends": dangling,
        "detour_median": round(sorted(detour.values())[len(detour) // 2], 2) if detour else None,
        "detour_worst": [(n, round(v, 2)) for n, v in worst],
        "longest_signal_nets_mm": longest,
        "vias_per_net_max": max(via_count.items(), key=lambda kv: kv[1]) if via_count else None,
    }


def decoupling(parts: list[place.Part]) -> list[dict]:
    """Small 2-pad C* parts with one ground pad and one supply-net (global net) pad = decoupling caps.
    Distance from the cap's supply pad to the nearest supply pad of a >= 5-pad part on that net."""
    glob = place.global_nets(parts)
    big = [p for p in parts if len(p.pads) >= 5]
    out = []
    for c in parts:
        if len(c.pads) != 2 or not c.ref.upper().startswith("C"):
            continue
        nets = [q.net for q in c.pads]
        if not any(place.is_ground(n) for n in nets):
            continue
        sup = [q for q in c.pads if q.net and not place.is_ground(q.net) and q.net in glob]
        if not sup:
            continue
        q = sup[0]
        cx, cy = c.pad_pos(q)
        best = None
        for ic in big:
            for ip in ic.pads:
                if ip.net == q.net:
                    d = math.dist((cx, cy), ic.pad_pos(ip))
                    if best is None or d < best[0]:
                        best = (d, ic.ref)
        if best:
            out.append({"cap": c.ref, "net": q.net, "ic": best[1], "dist_mm": round(best[0], 2),
                        "supply_is_global": q.net in glob})
    return sorted(out, key=lambda d: -d["dist_mm"])


def compactness(parts: list[place.Part]) -> dict:
    """Per schematic sheet: bbox area of its parts / sum of their courtyard areas (1.0 = perfectly packed)."""
    by: dict[str, list[place.Part]] = defaultdict(list)
    for p in parts:
        if not p.fixed:
            by[p.sheet].append(p)
    out = {}
    for s, ps in by.items():
        bs = [p.abox() for p in ps]
        area = (max(b[2] for b in bs) - min(b[0] for b in bs)) * (max(b[3] for b in bs) - min(b[1] for b in bs))
        out[s] = round(area / max(sum(p.w * p.h for p in ps), 1e-9), 2)
    return out
