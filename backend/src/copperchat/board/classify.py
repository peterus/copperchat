"""Generic part and net classification (B-C6, spec §9.1, index call 15). Pure, no kipy import,
no design knowledge (no refs or MPNs): reference prefixes, footprint library nicknames, pad
counts, net names (last `/`-component) and net topology only.

Output is deterministic: refs are natural-sorted, nets sorted, mappings built in sorted order.
`net_kind` never uses `"high_current"` in v1 (no generic rule for it)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from copperchat.board.boardmd import BoardDoc, PartClass
from copperchat.board.geom import octi_dist, polygon_inside
from copperchat.board.model import BoardModel, Pad, Part
from copperchat.kicad_tools.filtering import natural_key

__all__ = [
    "BlockProposal",
    "Classification",
    "Crystal",
    "Decoupling",
    "DiffPair",
    "NetKind",
    "PartClass",
    "Switcher",
    "classify",
    "primary_ground",
]

NetKind = Literal[
    "ground", "supply", "switch_node", "diff_pair", "crystal", "high_current", "signal"
]


@dataclass(frozen=True)
class Decoupling:
    cap: str
    ic: str
    pin: str
    supply_net: str
    ground_net: str


@dataclass(frozen=True)
class Switcher:
    ic: str
    inductor: str
    sw_net: str
    vin_net: str
    in_caps: tuple[str, ...]
    out_caps: tuple[str, ...]
    fb_parts: tuple[str, ...]


@dataclass(frozen=True)
class Crystal:
    ref: str
    ic: str
    load_caps: tuple[str, ...]
    nets: tuple[str, ...]


@dataclass(frozen=True)
class DiffPair:
    p: str
    n: str
    anchor: str


@dataclass(frozen=True)
class BlockProposal:
    name: str
    anchor: str | None
    refs: tuple[str, ...]
    sheet: str


@dataclass(frozen=True)
class Classification:
    part_class: Mapping[str, PartClass]
    net_kind: Mapping[str, NetKind]
    decoupling: tuple[Decoupling, ...]
    switchers: tuple[Switcher, ...]
    crystals: tuple[Crystal, ...]
    pairs: tuple[DiffPair, ...]
    blocks: tuple[BlockProposal, ...]
    critical_nets: tuple[str, ...]
    overrides_applied: tuple[str, ...]


# ---- patterns ------------------------------------------------------------------------------

_GROUND = re.compile(r"^(GND|AGND|DGND|PGND|VSS)$", re.IGNORECASE)
_SUPPLY = re.compile(r"^(VCC\w*|VDD\w*|VBUS|VBAT|VSYS|[+-]?\d+V\d*|\+?\d+V\d+)$", re.IGNORECASE)
_CAP_REF = re.compile(r"^C\d")
_RES_REF = re.compile(r"^R\d")
_IND_REF = re.compile(r"^L\d")
_HOLE_REF = re.compile(r"^H\d")
_FID_REF = re.compile(r"^FID\d")
_TP_REF = re.compile(r"^TP\d")
_CONN_REF = re.compile(r"^(J|P|CN|USB)\d")
_XTAL_REF = re.compile(r"^[YX]\d")
_DEVICE = re.compile(r"^Device:([CRL])(_|$)")
_NAME_BAD = re.compile(r"[^A-Za-z0-9 _.+-]")
PAIR_SUFFIXES: tuple[tuple[str, str], ...] = (("_DP", "_DM"), ("D+", "D-"), ("+", "-"), ("P", "N"))
ANCHOR_MIN_PADS = 5
CRITICAL_TWO_PAD_MM = 10.0
_FIXTURE = frozenset({"mounting_hole", "fiducial", "test_point"})


def _leaf(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def _nick(part: Part) -> str:
    return part.lib_id.split(":", 1)[0].casefold() if ":" in part.lib_id else ""


def _lib(part: Part, *prefixes: str) -> bool:
    nick = _nick(part)
    return any(nick.startswith(p.casefold()) for p in prefixes)


def _device(part: Part, letter: str) -> bool:
    m = _DEVICE.match(part.lib_id)
    return m is not None and m.group(1) == letter


def _is_cap(p: Part) -> bool:
    return len(p.pads) == 2 and (
        bool(_CAP_REF.match(p.ref)) or _lib(p, "Capacitor") or _device(p, "C")
    )


def _is_res(p: Part) -> bool:
    return len(p.pads) == 2 and (
        bool(_RES_REF.match(p.ref)) or _lib(p, "Resistor") or _device(p, "R")
    )


def _is_ind(p: Part) -> bool:
    return bool(_IND_REF.match(p.ref)) or _lib(p, "Inductor") or _device(p, "L")


def _sheet_path(p: Part) -> str:
    return p.sheet.strip("/")


def _sheet_leaf(sheet: str) -> str:
    parts = [s for s in sheet.split("/") if s]
    return parts[-1] if parts else "root"


def _block_name(sheet: str, ref: str) -> str:
    name = _NAME_BAD.sub("_", f"{sheet}-{ref}")
    if not name[:1].isascii() or not name[:1].isalnum():
        name = "s" + name
    return name[:64]


def _refs(refs: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(refs), key=natural_key))


def _pad_key(p: Pad) -> tuple[object, ...]:
    return (natural_key(p.ref), natural_key(p.number), p.x, p.y)


def _centre(p: Part) -> tuple[float, float]:
    return (p.x, p.y)


# ---- analysis ------------------------------------------------------------------------------


class _Ctx:
    """Indexes built once: parts by ref, pads by net, nets by part."""

    def __init__(self, model: BoardModel) -> None:
        parts = sorted(model.parts, key=lambda p: (natural_key(p.ref), p.kiid))
        self.parts: dict[str, Part] = {}
        for p in parts:
            self.parts.setdefault(p.ref, p)
        self.pads_on: dict[str, list[Pad]] = defaultdict(list)
        self.nets_of: dict[str, list[str]] = {}
        for p in self.parts.values():
            nets = set()
            for pad in p.pads:
                if pad.net:
                    self.pads_on[pad.net].append(pad)
                    nets.add(pad.net)
            self.nets_of[p.ref] = sorted(nets)
        for pads in self.pads_on.values():
            pads.sort(key=_pad_key)
        self.refs_on: dict[str, list[str]] = {
            net: list(_refs(pad.ref for pad in pads)) for net, pads in self.pads_on.items()
        }

    def other_net(self, part: Part, net: str) -> str | None:
        """The net of a 2-pad part's other pad (None when both pads are on `net`)."""
        a, b = part.pads
        if a.net == net and b.net != net:
            return b.net
        if b.net == net and a.net != net:
            return a.net
        return None


def _ground_nets(nets: Iterable[str]) -> set[str]:
    out = set()
    for n in nets:
        leaf = _leaf(n)
        if _GROUND.match(leaf) or "GND" in leaf.upper():
            out.add(n)
    return out


def _supply_nets(c: _Ctx, ground: set[str]) -> set[str]:
    out = set()
    for net, pads in c.pads_on.items():
        if net in ground:
            continue
        if _SUPPLY.match(_leaf(net)):
            out.add(net)
            continue
        if len(pads) < 4 or len(c.refs_on[net]) < 3:
            continue
        for ref in c.refs_on[net]:
            p = c.parts[ref]
            if _is_cap(p) and c.other_net(p, net) in ground:
                out.add(net)
                break
    return out


def _pre_class(p: Part, zones: list[tuple[tuple[float, float], ...]]) -> PartClass | None:
    ref = p.ref
    if _HOLE_REF.match(ref) or _lib(p, "MountingHole"):
        return "mounting_hole"
    if _FID_REF.match(ref) or _lib(p, "Fiducial"):
        return "fiducial"
    if _TP_REF.match(ref) or _lib(p, "TestPoint"):
        return "test_point"
    if p.rule_areas or (
        len(p.courtyard) >= 3 and any(polygon_inside(z, p.courtyard) for z in zones)
    ):
        return "module_with_antenna"
    if _CONN_REF.match(ref) or _lib(p, "Connector"):
        return "connector"
    if _XTAL_REF.match(ref) or _lib(p, "Crystal", "Oscillator"):
        return "crystal"
    return None


@dataclass
class _Sw:
    sw: Switcher
    boot: tuple[str, ...]


def _caps_to_ground(
    c: _Ctx, net: str, ground: set[str], pre: dict[str, PartClass | None]
) -> list[str]:
    out = []
    for ref in c.refs_on.get(net, ()):
        p = c.parts[ref]
        if pre[ref] is None and _is_cap(p) and c.other_net(p, net) in ground:
            out.append(ref)
    return out


def _switchers(
    c: _Ctx,
    anchors: list[str],
    ground: set[str],
    supply: set[str],
    pre: dict[str, PartClass | None],
) -> list[_Sw]:
    out: list[_Sw] = []
    anchor_set = set(anchors)
    for a in anchors:
        if pre[a] is not None:
            continue
        ap = c.parts[a]
        for net in c.nets_of[a]:
            if net in ground:
                continue
            found = _switch_node(c, a, net, pre)
            if found is None:
                continue
            ind, out_net, boot = found
            vin = _vin_net(c, a, out_net, ground, supply, pre)
            sheet = _sheet_path(ap)
            in_caps = _sheet_caps(c, a, vin, ground, pre, anchor_set, sheet) if vin else []
            out_caps = _sheet_caps(c, a, out_net, ground, pre, anchor_set, sheet)
            fb = _fb_parts(c, a, ground, supply, net)
            out.append(
                _Sw(
                    Switcher(
                        ic=a,
                        inductor=ind,
                        sw_net=net,
                        vin_net=vin,
                        in_caps=_refs(in_caps),
                        out_caps=_refs(out_caps),
                        fb_parts=_refs(fb),
                    ),
                    _refs(boot),
                )
            )
    return out


def _switch_node(
    c: _Ctx, a: str, net: str, pre: dict[str, PartClass | None]
) -> tuple[str, str, list[str]] | None:
    """(inductor, its other net, bootstrap caps) when `net` touches only anchor `a`, exactly
    one inductor and 2-pad caps whose other net carries only `a`'s pads (index call 15)."""
    inductors: list[str] = []
    boot: list[str] = []
    for ref in c.refs_on[net]:
        if ref == a:
            continue
        p = c.parts[ref]
        if pre[ref] is None and _is_ind(p):
            inductors.append(ref)
            continue
        if pre[ref] is None and _is_cap(p):
            other = c.other_net(p, net)
            if other and set(c.refs_on[other]) == {a, ref}:
                boot.append(ref)
                continue
        return None
    if len(inductors) != 1:
        return None
    ind = c.parts[inductors[0]]
    others = sorted({pad.net for pad in ind.pads if pad.net != net})
    if len(others) != 1 or not others[0]:
        return None
    return inductors[0], others[0], boot


def _vin_net(
    c: _Ctx,
    a: str,
    out_net: str,
    ground: set[str],
    supply: set[str],
    pre: dict[str, PartClass | None],
) -> str:
    best: tuple[int, str] | None = None
    for net in c.nets_of[a]:
        if net == out_net or net not in supply:
            continue
        n = len(_caps_to_ground(c, net, ground, pre))
        if n and (best is None or n > best[0]):
            best = (n, net)
    return best[1] if best else ""


def _sheet_caps(
    c: _Ctx,
    a: str,
    net: str,
    ground: set[str],
    pre: dict[str, PartClass | None],
    anchors: set[str],
    sheet: str,
) -> list[str]:
    """Caps between `net` and ground on the switcher's sheet; when another anchor on that
    sheet is on `net`, only caps strictly nearer to the switcher (index call 15)."""
    rivals = [
        c.parts[r]
        for r in c.refs_on.get(net, ())
        if r != a and r in anchors and _sheet_path(c.parts[r]) == sheet
    ]
    me = _centre(c.parts[a])
    out = []
    for ref in _caps_to_ground(c, net, ground, pre):
        p = c.parts[ref]
        if _sheet_path(p) != sheet:
            continue
        d = octi_dist(_centre(p), me)
        if all(d < octi_dist(_centre(p), _centre(r)) for r in rivals):
            out.append(ref)
    return out


def _fb_parts(c: _Ctx, a: str, ground: set[str], supply: set[str], sw_net: str) -> list[str]:
    out: list[str] = []
    for net in c.nets_of[a]:
        if net in ground or net in supply or net == sw_net:
            continue
        others = [r for r in c.refs_on[net] if r != a]
        if others and all(_is_res(c.parts[r]) for r in others):
            out.extend(others)
    return out


def _decoupling(
    c: _Ctx,
    anchors: set[str],
    ground: set[str],
    supply: set[str],
    pre: dict[str, PartClass | None],
    excluded: set[str],
) -> list[Decoupling]:
    out = []
    for ref, p in c.parts.items():
        if pre[ref] is not None or ref in excluded or not _is_cap(p):
            continue
        a, b = p.pads
        if a.net in supply and b.net in ground:
            sp, gp = a, b
        elif b.net in supply and a.net in ground:
            sp, gp = b, a
        else:
            continue
        cands = [pad for pad in c.pads_on[sp.net] if pad.ref in anchors]
        if not cands:
            continue
        pin = min(
            cands,
            key=lambda q: (
                octi_dist((sp.x, sp.y), (q.x, q.y)),
                natural_key(q.ref),
                natural_key(q.number),
            ),
        )
        out.append(Decoupling(ref, pin.ref, pin.number, sp.net, gp.net))
    return out


def _crystals(
    c: _Ctx,
    anchors: list[str],
    ground: set[str],
    supply: set[str],
    pre: dict[str, PartClass | None],
) -> list[Crystal]:
    out = []
    for ref in c.parts:
        if pre[ref] != "crystal":
            continue
        nets = [n for n in c.nets_of[ref] if n not in ground and n not in supply]
        shared: dict[str, int] = defaultdict(int)
        for n in nets:
            for r in c.refs_on[n]:
                if r != ref and r in anchors:
                    shared[r] += 1
        ic = min(shared, key=lambda r: (-shared[r], natural_key(r))) if shared else ""
        load: list[str] = []
        for n in nets:
            load.extend(_caps_to_ground(c, n, ground, pre))
        out.append(Crystal(ref, ic, _refs(load), tuple(nets)))
    return out


def _pairs(c: _Ctx, anchors: list[str], ground: set[str], supply: set[str]) -> list[DiffPair]:
    nets = {n for n in c.pads_on if n not in ground and n not in supply}
    used: set[str] = set()
    out = []
    for net in sorted(nets):
        if net in used:
            continue
        for ps, ns in PAIR_SUFFIXES:
            if not net.endswith(ps) or len(net) == len(ps):
                continue
            partner = net[: -len(ps)] + ns
            if partner not in nets or partner in used or partner == net:
                continue
            common = set(c.refs_on[net]) & set(c.refs_on[partner]) & set(anchors)
            if not common:
                continue
            anchor = min(common, key=lambda r: (-len(c.parts[r].pads), natural_key(r)))
            out.append(DiffPair(net, partner, anchor))
            used.update((net, partner))
            break
    return out


def _blocks(
    c: _Ctx,
    part_class: dict[str, PartClass],
    struct_anchors: list[str],
    net_kind: dict[str, NetKind],
    switchers: list[_Sw],
    crystals: list[Crystal],
    decoupling: list[Decoupling],
) -> list[BlockProposal]:
    heads = [
        r
        for r in c.parts
        if part_class[r] not in _FIXTURE and (r in set(struct_anchors) or part_class[r] == "anchor")
    ]
    head_set = set(heads)
    owner: dict[str, str] = {}

    def give(ref: str, head: str) -> None:
        if head in head_set and ref not in head_set and ref not in owner and ref in c.parts:
            owner[ref] = head

    for s in switchers:
        sw = s.sw
        for ref in (sw.inductor, *s.boot, *sw.in_caps, *sw.out_caps, *sw.fb_parts):
            give(ref, sw.ic)
    for x in crystals:
        if x.ic:
            give(x.ref, x.ic)
            for ref in x.load_caps:
                give(ref, x.ic)
    for d in decoupling:
        give(d.cap, d.ic)

    anchors_on: dict[str, list[str]] = {
        net: [r for r in refs if r in head_set] for net, refs in c.refs_on.items()
    }

    def best(ref: str, nets: list[str]) -> str | None:
        count: dict[str, int] = defaultdict(int)
        for n in nets:
            for h in anchors_on.get(n, ()):
                count[h] += 1
        if not count:
            return None
        sheet = _sheet_path(c.parts[ref])
        return min(
            count,
            key=lambda h: (
                -count[h],
                0 if _sheet_path(c.parts[h]) == sheet else 1,
                natural_key(h),
            ),
        )

    rest = [r for r in c.parts if r not in head_set and r not in owner]
    for ref in rest:
        signal = [n for n in c.nets_of[ref] if net_kind[n] not in ("ground", "supply")]
        h = best(ref, signal)
        if h is not None:
            owner[ref] = h
    misc: list[str] = []
    for ref in rest:
        if ref in owner:
            continue
        h = best(ref, c.nets_of[ref])
        if h is None:
            misc.append(ref)
        else:
            owner[ref] = h

    members: dict[str, list[str]] = {h: [h] for h in heads}
    for ref, h in owner.items():
        members[h].append(ref)
    out = []
    for h in heads:
        leaf = _sheet_leaf(c.parts[h].sheet)
        out.append(BlockProposal(_block_name(leaf, h), h, _refs(members[h]), leaf))
    out.sort(key=lambda b: natural_key(b.name))
    if misc:
        out.append(BlockProposal("misc", None, _refs(misc), ""))
    return out


def _critical(
    c: _Ctx,
    ground: set[str],
    decoupling: list[Decoupling],
    switchers: list[_Sw],
    crystals: list[Crystal],
    pairs: list[DiffPair],
) -> tuple[str, ...]:
    out: set[str] = {d.supply_net for d in decoupling}
    for s in switchers:
        out.add(s.sw.sw_net)
        if s.sw.vin_net:
            out.add(s.sw.vin_net)
    for x in crystals:
        out.update(x.nets)
    for p in pairs:
        out.update((p.p, p.n))
    for net, pads in c.pads_on.items():
        if len(pads) == 2:
            a, b = pads
            if octi_dist((a.x, a.y), (b.x, b.y)) <= CRITICAL_TWO_PAD_MM:
                out.add(net)
    return tuple(sorted(out - ground))


# ---- public --------------------------------------------------------------------------------


def classify(model: BoardModel, doc: BoardDoc | None = None) -> Classification:
    """Classify parts and nets of `model` (spec §9.1); `doc` supplies `board.md` overrides."""
    c = _Ctx(model)
    all_nets = set(c.pads_on)
    all_nets.update(t.net for t in model.tracks)
    all_nets.update(a.net for a in model.arcs)
    all_nets.update(v.net for v in model.vias)
    all_nets.update(z.net for z in model.zones)
    all_nets.discard("")

    ground = _ground_nets(all_nets)
    supply = _supply_nets(c, ground)
    supply.update(n for n in all_nets - ground if _SUPPLY.match(_leaf(n)))

    keepouts = [z.outline for z in model.zones if z.rule_area and z.keepout]
    pre: dict[str, PartClass | None] = {r: _pre_class(p, keepouts) for r, p in c.parts.items()}
    anchors = [
        r for r, p in c.parts.items() if len(p.pads) >= ANCHOR_MIN_PADS and pre[r] not in _FIXTURE
    ]
    anchor_set = set(anchors)

    switchers = _switchers(c, anchors, ground, supply, pre)
    sw_caps = {r for s in switchers for r in (*s.sw.in_caps, *s.sw.out_caps)}
    decoupling = _decoupling(c, anchor_set, ground, supply, pre, sw_caps)
    crystals = _crystals(c, anchors, ground, supply, pre)
    pairs = _pairs(c, anchors, ground, supply)

    sw_ics = {s.sw.ic for s in switchers}
    dec_caps = {d.cap for d in decoupling}
    part_class: dict[str, PartClass] = {}
    for ref, p in c.parts.items():
        cls: PartClass
        pc = pre[ref]
        if pc is not None:
            cls = pc
        elif ref in sw_ics:
            cls = "switching_regulator"
        elif _is_ind(p):
            cls = "inductor"
        elif ref in dec_caps:
            cls = "decoupling_cap"
        elif ref in anchor_set:
            cls = "anchor"
        else:
            cls = "other"
        part_class[ref] = cls

    applied: list[str] = []
    for row in doc.class_overrides if doc is not None else ():
        if row.ref not in part_class:
            applied.append(f"{row.ref}: unknown ref")
            continue
        applied.append(f"{row.ref}: {part_class[row.ref]} -> {row.part_class}")
        part_class[row.ref] = row.part_class
    decoupling = [d for d in decoupling if part_class[d.cap] == "decoupling_cap"]

    kinds: dict[str, NetKind] = {}
    sw_nets = {s.sw.sw_net for s in switchers}
    pair_nets = {n for p in pairs for n in (p.p, p.n)}
    xtal_nets = {n for x in crystals for n in x.nets}
    for net in sorted(all_nets):
        kind: NetKind
        if net in ground:
            kind = "ground"
        elif net in supply:
            kind = "supply"
        elif net in sw_nets:
            kind = "switch_node"
        elif net in pair_nets:
            kind = "diff_pair"
        elif net in xtal_nets:
            kind = "crystal"
        else:
            kind = "signal"
        kinds[net] = kind

    blocks = _blocks(c, part_class, anchors, kinds, switchers, crystals, decoupling)
    critical = _critical(c, ground, decoupling, switchers, crystals, pairs)

    return Classification(
        part_class=MappingProxyType(
            {r: part_class[r] for r in sorted(part_class, key=natural_key)}
        ),
        net_kind=MappingProxyType(kinds),
        decoupling=tuple(sorted(decoupling, key=lambda d: natural_key(d.cap))),
        switchers=tuple(
            sorted((s.sw for s in switchers), key=lambda s: (natural_key(s.ic), s.sw_net))
        ),
        crystals=tuple(sorted(crystals, key=lambda x: natural_key(x.ref))),
        pairs=tuple(sorted(pairs, key=lambda p: (p.p, p.n))),
        blocks=tuple(blocks),
        critical_nets=critical,
        overrides_applied=tuple(applied),
    )


def primary_ground(cls: Classification, model: BoardModel) -> str | None:
    """The ground net with the most pads (ties: name); None when there is no ground net."""
    count: dict[str, int] = {n: 0 for n, k in cls.net_kind.items() if k == "ground"}
    for p in model.parts:
        for pad in p.pads:
            if pad.net in count:
                count[pad.net] += 1
    if not count:
        return None
    return min(count, key=lambda n: (-count[n], n))
