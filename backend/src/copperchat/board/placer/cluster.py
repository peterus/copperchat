"""Clusters of a block (B-C11, spec §9.2): each anchor with the satellites that belong next
to it. Pure, deterministic (natural-ref sort keys, no randomness).

Rules (P3a's block rules restricted to the block's refs):

* switcher passives (inductor, input/output caps, feedback parts) → their switching
  regulator; a crystal and its load caps → its IC; a decoupling cap → its IC;
* a smaller anchor that shares signal nets with a bigger anchor of the block (e.g. an ESD
  array next to its connector) → that anchor, so it is laid out beside the pins it serves;
* other parts → the anchor sharing the most signal nets (a chain behind a satellite, like a
  series resistor and its LED, follows the satellite's anchor), leftovers → the anchor
  sharing the most nets, else a cluster of their own.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from copperchat.board.classify import ANCHOR_MIN_PADS, Classification
from copperchat.board.model import BoardModel, Part
from copperchat.board.staging import natural_key

_FIXTURE = frozenset({"mounting_hole", "fiducial", "test_point"})
_ANCHOR_CLASSES = frozenset({"anchor", "switching_regulator", "module_with_antenna"})


@dataclass(frozen=True)
class Cluster:
    anchor: str | None
    members: tuple[str, ...]  # anchor first, then the satellites in natural ref order


class NetSets:
    """Per part: all nets and the signal nets (not ground, not supply) of its pads."""

    def __init__(self, model: BoardModel, cls: Classification, refs: Sequence[str]) -> None:
        self.all: dict[str, set[str]] = {}
        self.signal: dict[str, set[str]] = {}
        for ref in refs:
            nets = {q.net for q in model.part(ref).pads if q.net}
            self.all[ref] = nets
            self.signal[ref] = {
                n for n in nets if cls.net_kind.get(n, "signal") not in ("ground", "supply")
            }


def is_anchor(part: Part, cls: Classification) -> bool:
    kind = cls.part_class.get(part.ref, "other")
    if kind in _FIXTURE:
        return False
    return kind in _ANCHOR_CLASSES or len(part.pads) >= ANCHOR_MIN_PADS


def _best(
    ref: str, heads: Sequence[str], nets: dict[str, set[str]], model: BoardModel
) -> str | None:
    count = {h: len(nets[ref] & nets[h]) for h in heads if h != ref}
    count = {h: n for h, n in count.items() if n > 0}
    if not count:
        return None
    return min(count, key=lambda h: (-count[h], -len(model.part(h).pads), natural_key(h)))


def build_clusters(
    model: BoardModel, cls: Classification, refs: Sequence[str]
) -> tuple[Cluster, ...]:
    """The clusters of `refs` (unknown refs ignored), biggest anchor first, then natural
    order of the anchors; each part in exactly one cluster."""
    known = {p.ref for p in model.parts}
    wanted = sorted({r for r in refs if r in known}, key=natural_key)
    ns = NetSets(model, cls, wanted)
    anchors = [r for r in wanted if is_anchor(model.part(r), cls)]
    anchors.sort(key=lambda r: (-len(model.part(r).pads), natural_key(r)))
    owner: dict[str, str] = {}

    # a smaller anchor serving a bigger one (signal nets) joins it
    heads: list[str] = []
    for a in anchors:
        bigger = [h for h in heads if len(model.part(h).pads) > len(model.part(a).pads)]
        h = _best(a, bigger, ns.signal, model)
        if h is not None:
            owner[a] = h
        else:
            heads.append(a)
    head_set = set(heads)

    def give(ref: str, head: str) -> None:
        if ref in ns.all and ref not in head_set and ref not in owner:
            # the head may itself have joined another anchor: follow to the cluster head
            while head in owner:
                head = owner[head]
            if head in head_set:
                owner[ref] = head

    for s in cls.switchers:
        for ref in (s.inductor, *s.in_caps, *s.out_caps, *s.fb_parts):
            give(ref, s.ic)
    for x in cls.crystals:
        if x.ic:
            give(x.ref, x.ic)
            for ref in x.load_caps:
                give(ref, x.ic)
    for d in cls.decoupling:
        give(d.cap, d.ic)

    def head_of(ref: str) -> str:
        while ref in owner:
            ref = owner[ref]
        return ref

    rest = [r for r in wanted if r not in head_set and r not in owner]
    # signal nets: to an anchor directly, else behind an already owned part (chains)
    pending = list(rest)
    progress = True
    while pending and progress:
        progress = False
        for ref in list(pending):
            h = _best(ref, heads, ns.signal, model)
            if h is None:
                via: dict[str, int] = defaultdict(int)
                for r in owner:
                    shared = len(ns.signal[ref] & ns.signal[r])
                    if shared:
                        via[head_of(r)] += shared
                if via:
                    h = min(via, key=lambda k: (-via[k], natural_key(k)))
            if h is not None:
                owner[ref] = h
                pending.remove(ref)
                progress = True
    own: list[str] = []
    for ref in pending:
        h = _best(ref, heads, ns.all, model)
        if h is None:
            own.append(ref)
        else:
            owner[ref] = h

    members: dict[str, list[str]] = {h: [] for h in heads}
    for ref in owner:
        members[head_of(ref)].append(ref)
    out = [Cluster(h, (h, *sorted(members[h], key=natural_key))) for h in heads]
    out.extend(Cluster(r, (r,)) for r in own)
    return tuple(out)
