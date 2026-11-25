"""KIID-diff compensation of a multi-stage board write (B-C4, spec §6.3).

`take_snapshot` records the KIIDs of every board-level item a board tool can create
(tracks, arcs, vias, zones, footprints, groups, graphics) plus the placement of every
footprint; mode "full" also keeps the data of every track and via. `compensate` brings
the live board back to it: items whose KIID is not in the snapshot are removed, moved
footprints are put back (`flip_items` for a side change), and in "full" mode missing or
changed tracks/vias are re-created / restored from the stored data. It returns what
still differs (empty = clean). Both run inside one `session.call`; the engine wraps
`compensate` in a board commit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal

from kipy.board_types import ArcTrack as KipyArcTrack
from kipy.board_types import BoardItem
from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.geometry import Angle, Vector2
from kipy.proto.board import board_commands_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import KIID

from copperchat.board.model import ArcTrack, Side, Track, Via
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.read import _layer, long_requests

NM_PER_MM = 1_000_000
ROT_EPS = 1e-6

Position = tuple[str, float, float, float, Side, bool]  # (kiid, x, y, rot, side, locked)


@dataclass(frozen=True)
class CopperSnapshot:
    """What `compensate` restores. "ids": KIIDs + footprint placement; "full": also the
    tracks/vias as model data (`tracks`, `vias`) and as serialised protobufs (`raw`:
    kiid -> bytes, so a re-created via keeps its padstack and type)."""

    track_ids: frozenset[str]
    via_ids: frozenset[str]
    zone_ids: frozenset[str]
    footprint_ids: frozenset[str]
    group_ids: frozenset[str]
    graphic_ids: frozenset[str]
    positions: tuple[Position, ...]
    tracks: tuple[Track | ArcTrack, ...] = ()
    vias: tuple[Via, ...] = ()
    raw: tuple[tuple[str, bytes], ...] = ()

    def ids(self) -> dict[str, frozenset[str]]:
        return {
            "track": self.track_ids,
            "via": self.via_ids,
            "zone": self.zone_ids,
            "footprint": self.footprint_ids,
            "group": self.group_ids,
            "graphic": self.graphic_ids,
        }


def _mm(nm: int) -> float:
    return nm / NM_PER_MM  # exact round trip through _nm (no 0.1 um rounding as nm_to_mm)


def _nm(mm: float) -> int:
    return round(mm * NM_PER_MM)


def _pt(v: Any) -> tuple[float, float]:
    return (_mm(v.x), _mm(v.y))


def _kid(item: Any) -> str:
    return str(item.id.value)


def _items(board: Any) -> dict[str, dict[str, Any]]:
    """kind -> kiid -> kipy item of every board-level item the snapshot covers."""
    graphics = [*board.get_shapes(), *board.get_text()]
    return {
        "track": {_kid(t): t for t in board.get_tracks()},
        "via": {_kid(v): v for v in board.get_vias()},
        "zone": {_kid(z): z for z in board.get_zones()},
        "footprint": {_kid(f): f for f in board.get_footprints()},
        "group": {_kid(g): g for g in board.get_groups()},
        "graphic": {_kid(g): g for g in graphics},
    }


def _side(fp: Any) -> Side:
    return "bottom" if fp.layer == BoardLayer.BL_B_Cu else "top"


def _position(fp: Any) -> Position:
    x, y = _pt(fp.position)
    return (_kid(fp), x, y, fp.orientation.degrees % 360, _side(fp), bool(fp.locked))


def _track(t: Any) -> Track | ArcTrack:
    net, layer, width = str(t.net.name), _layer(t.layer), _mm(t.width)
    if isinstance(t, KipyArcTrack):
        return ArcTrack(_kid(t), net, layer, _pt(t.start), _pt(t.mid), _pt(t.end), width, t.locked)
    return Track(_kid(t), net, layer, _pt(t.start), _pt(t.end), width, t.locked)


def _via(v: Any) -> Via:
    return Via(
        kiid=_kid(v),
        net=str(v.net.name),
        at=_pt(v.position),
        diameter=_mm(v.diameter),
        drill=_mm(v.drill_diameter),
        locked=v.locked,
    )


@long_requests
def take_snapshot(h: KicadHandle, mode: Literal["ids", "full"]) -> CopperSnapshot:
    """The compensation snapshot of the live board (one read per item kind)."""
    items = _items(h.board())
    tracks: tuple[Track | ArcTrack, ...] = ()
    vias: tuple[Via, ...] = ()
    raw: tuple[tuple[str, bytes], ...] = ()
    if mode == "full":
        tracks = tuple(_track(t) for t in items["track"].values())
        vias = tuple(_via(v) for v in items["via"].values())
        raw = tuple(
            (k, item.proto.SerializeToString())
            for kind in ("track", "via")
            for k, item in items[kind].items()
        )
    ids = {kind: frozenset(found) for kind, found in items.items()}
    return CopperSnapshot(
        track_ids=ids["track"],
        via_ids=ids["via"],
        zone_ids=ids["zone"],
        footprint_ids=ids["footprint"],
        group_ids=ids["group"],
        graphic_ids=ids["graphic"],
        positions=tuple(sorted(_position(f) for f in items["footprint"].values())),
        tracks=tracks,
        vias=vias,
        raw=raw,
    )


def _same_place(a: Position, b: Position) -> bool:
    return (
        _nm(a[1]) == _nm(b[1])
        and _nm(a[2]) == _nm(b[2])
        and abs(((a[3] - b[3] + 180) % 360) - 180) <= ROT_EPS
        and a[4] == b[4]
        and a[5] == b[5]
    )


def _kiids(ids: Iterable[str]) -> list[KIID]:
    out = []
    for value in ids:
        k = KIID()
        k.value = value
        out.append(k)
    return out


def _raw_kind(snap: CopperSnapshot) -> dict[str, str]:
    """kiid -> "track" | "arc" | "via" of the stored protobufs."""
    kinds = {v.kiid: "via" for v in snap.vias}
    for t in snap.tracks:
        kinds[t.kiid] = "arc" if isinstance(t, ArcTrack) else "track"
    return kinds


def _rebuild(kind: str, data: bytes) -> BoardItem:
    item: BoardItem
    if kind == "via":
        item = KipyVia()
    elif kind == "arc":
        item = KipyArcTrack()
    else:
        item = KipyTrack()
    item.proto.ParseFromString(data)
    return item


def _data_differs(snap: CopperSnapshot, now: dict[str, dict[str, Any]]) -> list[str]:
    """KIIDs of stored tracks/vias that exist but whose data changed (`now` may be keyed
    by the original KIID of a re-created item; its own KIID is not compared)."""
    out: list[str] = []
    for t in snap.tracks:
        live = now["track"].get(t.kiid)
        if live is not None and replace(_track(live), kiid=t.kiid) != t:
            out.append(t.kiid)
    for v in snap.vias:
        live = now["via"].get(v.kiid)
        if live is not None and replace(_via(live), kiid=v.kiid) != v:
            out.append(v.kiid)
    return out


@long_requests
def restore(h: KicadHandle, snap: CopperSnapshot) -> dict[str, str]:
    """The writes of `compensate`, planned from ONE read of the live board: inside an
    open commit KiCad stages every change until the push (reads still show the
    pre-commit board), so nothing here may depend on reading its own writes. Returns
    old -> new KIID of re-created tracks/vias whose KIID KiCad did not keep."""
    board = h.board()
    now = _items(board)
    expected = snap.ids()
    extra = [k for kind, found in now.items() for k in found if k not in expected[kind]]
    if extra:
        board.remove_items_by_id(_kiids(extra))

    # "full": re-create the deleted tracks/vias and restore the changed ones from the
    # stored protobufs (their KIIDs included).
    raw = dict(snap.raw)
    kinds = _raw_kind(snap)
    renamed: dict[str, str] = {}
    if raw:
        missing = [k for k in raw if k not in now["via" if kinds.get(k) == "via" else "track"]]
        changed = _data_differs(snap, now)
        if missing:
            created = board.create_items([_rebuild(kinds[k], raw[k]) for k in missing])
            for old, item in zip(missing, created, strict=False):
                if _kid(item) != old:
                    renamed[old] = _kid(item)
        if changed:
            board.update_items([_rebuild(kinds[k], raw[k]) for k in changed])

    # Footprint placement: side first (flip; kipy refreshes the passed item from the
    # reply), then position / rotation / locked on that item.
    fps = now["footprint"]
    wrong = [p for p in snap.positions if p[0] in fps and not _same_place(_position(fps[p[0]]), p)]
    flip = [fps[p[0]] for p in wrong if _side(fps[p[0]]) != p[4]]
    if flip:
        board.flip_items(flip, board_commands_pb2.BoardFlipDirection.BFD_LEFT_RIGHT)
    update = []
    for pos in wrong:
        fp = fps[pos[0]]
        fp.position = Vector2.from_xy(_nm(pos[1]), _nm(pos[2]))
        fp.orientation = Angle.from_degrees(pos[3])
        fp.locked = pos[5]
        update.append(fp)
    if update:
        board.update_items(update)
    return renamed


def compensate(h: KicadHandle, snap: CopperSnapshot) -> list[str]:
    """Bring the live board back to `snap` (see the module docstring); returns the
    leftovers - human-readable, empty when the board matches the snapshot again.
    For use without an open commit (each write then commits on its own); the engine
    runs `restore` inside one board commit and `leftovers` after its push."""
    return leftovers(h, snap, restore(h, snap))


@long_requests
def leftovers(
    h: KicadHandle, snap: CopperSnapshot, renamed: dict[str, str] | None = None
) -> list[str]:
    """What differs between the live board and `snap` ("extra track <kiid>", "missing
    footprint <kiid>", "moved footprint <kiid>", "changed via <kiid>")."""
    renamed = renamed or {}
    now = _items(h.board())
    out: list[str] = []
    for kind, expected_ids in snap.ids().items():
        expected = {renamed.get(k, k) for k in expected_ids}
        found = now[kind]
        out += [f"extra {kind} {k}" for k in sorted(set(found) - expected)]
        out += [f"missing {kind} {k}" for k in sorted(expected - set(found))]
    fps = now["footprint"]
    for pos in snap.positions:
        fp = fps.get(pos[0])
        if fp is not None and not _same_place(_position(fp), pos):
            out.append(f"moved footprint {pos[0]}")
    if snap.raw:
        back = {new: old for old, new in renamed.items()}
        live = {
            "track": {back.get(k, k): v for k, v in now["track"].items()},
            "via": {back.get(k, k): v for k, v in now["via"].items()},
        }
        for k in _data_differs(snap, live):
            out.append(f"changed {'via' if k in snap.via_ids else 'track'} {k}")
    return out


def counts(before: CopperSnapshot, after: CopperSnapshot) -> dict[str, int]:
    """The change-log `after` of a board call: items added / removed per kind and
    footprints moved (non-zero entries only)."""
    out: dict[str, int] = {}
    b, a = before.ids(), after.ids()
    for kind in b:
        added, removed = len(a[kind] - b[kind]), len(b[kind] - a[kind])
        if added:
            out[f"{kind}s_added"] = added
        if removed:
            out[f"{kind}s_removed"] = removed
    old = {p[0]: p for p in before.positions}
    moved = sum(1 for p in after.positions if p[0] in old and not _same_place(old[p[0]], p))
    if moved:
        out["footprints_moved"] = moved
    return out
