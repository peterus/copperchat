"""The critical set's round-2 extension (R4, spec §22.4, round-2 plan call 8). Pure.

`classify` stays state-independent; `pcb_analyze` (`jobs.analyse_job`) adds, from the board's
defined blocks, every non-ground net that lies inside one block (`"intra_block"`) and every
non-ground net with an MST connection along a straight corridor (`"corridor"`, owner rule b),
so the own router routes them (straight first) before the Freerouting fill and only the
inter-block nets are left for it.
"""

from __future__ import annotations

from collections.abc import Mapping

from copperchat.board.classify import Classification
from copperchat.board.geom import neck_link_zones, neck_width, straight_link_corridor
from copperchat.board.metrics import _class_width, _copper, _mst, _pad_key
from copperchat.board.model import BoardModel, Pad
from copperchat.board.presets import PRESETS

__all__ = [
    "BLOCK_PREFIX",
    "CRITICAL_INTRA_MAX_PADS",
    "critical_with_reasons",
    "defined_blocks",
    "extra_critical",
]

BLOCK_PREFIX = "blk:"  # a functional block is a board group named "blk:<name>" (P4)
CRITICAL_INTRA_MAX_PADS = 8  # round-2 plan call 8: a larger net is no "short" block net


def defined_blocks(model: BoardModel) -> dict[str, tuple[str, ...]]:
    """Block name (without `blk:`) → member refs, from the board's groups (first wins)."""
    out: dict[str, tuple[str, ...]] = {}
    for name, refs in model.groups:
        if name.startswith(BLOCK_PREFIX):
            out.setdefault(name[len(BLOCK_PREFIX) :], refs)
    return out


def extra_critical(
    model: BoardModel, cls: Classification, blocks: Mapping[str, tuple[str, ...]]
) -> dict[str, str]:
    """Net → reason for every non-ground net that is not yet in `cls.critical_nets` and
    (1) has 2..CRITICAL_INTRA_MAX_PADS copper pads, all on parts of one of `blocks` →
    `"intra_block"`, or else (2) has an MST connection (`metrics.connections`) whose pads
    have a straight link as M16 measures it (`geom.straight_link_corridor`: neck-wide only
    in a necked end pad's escape zone, else the net class width; preset `jlcpcb-4l` minimum
    track) → `"corridor"`. Sorted by net name."""
    block_of: dict[str, str] = {}
    for name, refs in blocks.items():
        for r in refs:
            block_of.setdefault(r, name)
    known = set(cls.critical_nets)
    classes = {nc.name: nc for nc in model.net_classes}
    class_of = dict(model.net_class_of)
    patterned = [nc for nc in model.net_classes if nc.patterns]
    min_track = PRESETS["jlcpcb-4l"].min_track
    parts = {p.ref: p for p in model.parts}
    by_net: dict[str, list[Pad]] = {}
    for q in sorted((q for p in model.parts for q in p.pads if q.net), key=_pad_key):
        by_net.setdefault(q.net, []).append(q)
    out: dict[str, str] = {}
    for net in sorted(by_net):
        if net in known or cls.net_kind.get(net) == "ground":
            continue
        pads = [q for q in by_net[net] if _copper(q)]
        if len(pads) < 2:
            continue
        homes = {block_of.get(q.ref) for q in pads}
        if len(pads) <= CRITICAL_INTRA_MAX_PADS and len(homes) == 1 and None not in homes:
            out[net] = "intra_block"
            continue
        class_w = _class_width(classes, class_of.get(net, "Default"), net, patterned) or min_track
        for a, b in _mst(pads):
            w = min(neck_width(a, class_w, min_track), neck_width(b, class_w, min_track))
            zones = neck_link_zones(parts, a, b, class_w, min_track)
            link = straight_link_corridor(
                a, b, class_w, w, zones, copper_layers=model.copper_layers
            )
            if link is not None:
                out[net] = "corridor"
                break
    return out


def critical_with_reasons(
    model: BoardModel, cls: Classification
) -> tuple[tuple[str, ...], dict[str, str]]:
    """(critical nets, net → reason): `cls.critical_nets` (reason `"class"`, their order)
    followed by `extra_critical` over the board's defined blocks - else `cls.blocks`
    (the proposals) - corridor nets before intra-block nets (the flow routes them first)."""
    blocks = defined_blocks(model) or {b.name: b.refs for b in cls.blocks}
    extra = extra_critical(model, cls, blocks)
    reasons = {n: "class" for n in cls.critical_nets}
    nets = list(cls.critical_nets)
    for kind in ("corridor", "intra_block"):
        for n, r in extra.items():
            if r == kind:
                nets.append(n)
                reasons[n] = r
    return tuple(nets), reasons
