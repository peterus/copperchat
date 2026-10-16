from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.filtering import matches, natural_key, not_found
from copperchat.tools.spec import ToolContext, ToolResult


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    pa, pb = a["position_mm"], b["position_mm"]
    return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])


def _airwires(net: str, clusters: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Join copper islands of one net: each island to the nearest pad of the earlier ones."""
    wires: list[dict[str, Any]] = []
    joined = list(clusters[0])
    for cluster in clusters[1:]:
        a, b = min(((p, q) for p in cluster for q in joined), key=lambda pair: _distance(*pair))
        wires.append(
            {
                "net": net,
                "from_mm": a["position_mm"],
                "to_mm": b["position_mm"],
                "from_pad": a["uuid"],
                "to_pad": b["uuid"],
            }
        )
        joined += cluster
    return wires


def _unconnected(board: Any, pads: list[Any]) -> list[dict[str, Any]]:
    """Unrouted connections: pads of one net that are not copper-connected.

    `GetConnectedItems` returns everything copper-connected to a pad (tracks,
    vias, zones, other pads), so one call per island is enough.
    """
    by_net: dict[str, list[Any]] = defaultdict(list)
    for pad in pads:
        if pad.net.name:
            by_net[str(pad.net.name)].append(pad)
    out: list[dict[str, Any]] = []
    for net in sorted(by_net, key=natural_key):
        net_pads = by_net[net]
        if len(net_pads) < 2:
            continue
        seen: set[str] = set()
        clusters: list[list[dict[str, Any]]] = []
        for pad in net_pads:
            if access.kiid(pad) in seen:
                continue
            connected = {access.kiid(i) for i in board.get_connected_items(pad)}
            connected.add(access.kiid(pad))
            cluster = [access.pad_dict(q) for q in net_pads if access.kiid(q) in connected]
            seen |= {c["uuid"] for c in cluster}
            clusters.append(cluster)
        if len(clusters) > 1:
            out += _airwires(net, clusters)
    return out


def board_snapshot(h: KicadHandle) -> dict[str, Any]:
    """Everything the PCB tools and the agent renderer need, in mm, in one worker call.

    Includes `unconnected` (one GetConnectedItems call per copper island), so
    callers pass `CONNECTIVITY_TIMEOUT_FACTOR` x the API timeout; use
    `board_layout` when connectivity is not needed.
    """
    return board_layout(h) | {"unconnected": _unconnected(h.board(), list(h.board().get_pads()))}


# board_snapshot makes one API call per copper island; an unrouted board has many.
CONNECTIVITY_TIMEOUT_FACTOR = 4.0


async def snapshot_with_connectivity(ctx_session: Any) -> dict[str, Any]:
    timeout = ctx_session.settings.kicad_api_timeout_s * CONNECTIVITY_TIMEOUT_FACTOR
    snap: dict[str, Any] = await ctx_session.call(board_snapshot, timeout=timeout)
    return snap


def board_layout(h: KicadHandle) -> dict[str, Any]:
    """Footprints (pads, boxes), tracks, vias, outline and nets - no connectivity."""
    board = h.board()
    footprints = list(board.get_footprints())
    boxes = access.bounding_boxes(board, footprints)
    fps: list[dict[str, Any]] = []
    for fp, box in zip(footprints, boxes, strict=True):
        d = access.footprint_dict(fp)
        d["pads"] = sorted(
            (access.pad_dict(p) for p in fp.definition.pads),
            key=lambda p: (natural_key(p["number"]), p["uuid"]),
        )
        d["bbox_mm"] = box
        fps.append(d)
    _, outline = access.outline_segments(board)
    return {
        "footprints": sorted(fps, key=lambda f: (natural_key(f["ref"]), f["uuid"])),
        "tracks": [access.track_dict(t) for t in board.get_tracks()],
        "vias": [access.via_dict(v) for v in board.get_vias()],
        "outline": outline,
        "nets": sorted((str(n.name) for n in board.get_nets() if n.name), key=natural_key),
    }


async def pcb_list_footprints(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    snap = await ctx.session.call(board_layout)
    fps = [
        {k: v for k, v in f.items() if k not in ("pads", "bbox_mm")}
        for f in snap["footprints"]
        if matches(args.get("filter"), f["ref"], f["value"], f["footprint"])
    ]
    return ToolResult(data={"count": len(fps), "footprints": fps})


async def pcb_get_footprint(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ref = str(args["ref"])
    snap = await ctx.session.call(board_layout)
    hit = next((f for f in snap["footprints"] if f["ref"] == ref), None)
    if hit is None:
        raise not_found("Footprint", ref, (f["ref"] for f in snap["footprints"]))
    return ToolResult(data=hit)


async def pcb_get_net(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args["name"])
    snap = await snapshot_with_connectivity(ctx.session)
    if name not in snap["nets"]:
        raise not_found("Net", name, snap["nets"])
    pads = [
        {"ref": f["ref"], **p} for f in snap["footprints"] for p in f["pads"] if p["net"] == name
    ]
    return ToolResult(
        data={
            "name": name,
            "pads": pads,
            "tracks": [t for t in snap["tracks"] if t["net"] == name],
            "vias": [v for v in snap["vias"] if v["net"] == name],
            "unconnected": [u for u in snap["unconnected"] if u["net"] == name],
        }
    )


def board_boxes(h: KicadHandle) -> dict[str, Any]:
    """Board outline box and footprint boxes (mm) - cheaper than `board_snapshot`."""
    board = h.board()
    shapes, _ = access.outline_segments(board)
    outline = [b for b in access.bounding_boxes(board, shapes) if b is not None]
    footprints = list(board.get_footprints())
    boxes = access.bounding_boxes(board, footprints)
    box: dict[str, float] | None = None
    if outline:
        x0 = min(b["x"] for b in outline)
        y0 = min(b["y"] for b in outline)
        box = {
            "x": x0,
            "y": y0,
            "w": max(b["x"] + b["w"] for b in outline) - x0,
            "h": max(b["y"] + b["h"] for b in outline) - y0,
        }
    return {
        "outline": box,
        "footprints": [
            {"ref": access.field_text(fp.reference_field), "bbox_mm": b}
            for fp, b in zip(footprints, boxes, strict=True)
        ],
    }
