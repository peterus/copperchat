"""Connectivity verification of an applied change set against its NetIntent (E-C4).

Port of spike 2D `tools.verify` / `expected_partition`: the oracle is KiCad's live
netlist before and after the write, never CopperChat's own geometry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import NetIntent

PinLabel = Callable[[PinKey], str]
_MAX_PROBLEMS = 12


def _default_label(key: PinKey) -> str:
    return f"{key[1][:8]}.{key[2]}"


def _keys(netlist: Netlist) -> set[PinKey]:
    return {k for keys in netlist.nets.values() for k in keys}


def _groups(netlist: Netlist, universe: set[PinKey]) -> dict[PinKey, frozenset[PinKey]]:
    """Each pin of `universe` -> the pins of `universe` on its net (singleton if none)."""
    out: dict[PinKey, frozenset[PinKey]] = {}
    for keys in netlist.nets.values():
        group = frozenset(keys) & universe
        for k in group:
            out[k] = group
    for k in universe - out.keys():
        out[k] = frozenset({k})
    return out


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[PinKey, PinKey] = {}

    def find(self, k: PinKey) -> PinKey:
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, keys: Iterable[PinKey]) -> None:
        it = iter(keys)
        first = next(it, None)
        if first is None:
            return
        root = self.find(first)
        for k in it:
            self.parent[self.find(k)] = root


def remap(netlist: Netlist, uuids: Mapping[str, str]) -> Netlist:
    """Rename symbol UUIDs in every PinKey (`new -> old`), e.g. after a symbol was
    re-placed from the library during compensation."""
    if not uuids:
        return netlist
    return Netlist(
        {
            name: frozenset((p, uuids.get(u, u), n) for p, u, n in keys)
            for name, keys in netlist.nets.items()
        }
    )


def _describe(keys: Iterable[PinKey], label: PinLabel) -> str:
    names = sorted(label(k) for k in keys)
    more = f" (+{len(names) - 6} more)" if len(names) > 6 else ""
    return ", ".join(names[:6]) + more if names else "nothing"


def check_joined(
    after: Netlist,
    items: Mapping[tuple[str, str], str],
    intent: NetIntent,
    label: PinLabel | None = None,
    vanished: Iterable[str] = (),
) -> list[str]:
    """`intent.joined`: every group (pin keys and (sheet_path, item uuid) netlist items)
    on one net after the write. Created segments KiCad merged into another wire at push
    (`vanished`) are left out of their group (the wire they merged into carries on)."""
    name = label or _default_label
    gone = set(vanished)
    problems = []
    for raw in intent.joined:
        group = {k for k in raw if len(k) == 3 or k[1] not in gone}
        if len(group) < 2:
            continue
        nets = {after.net_of(k) if len(k) == 3 else items.get((k[0], k[1])) for k in group}
        if len(nets) != 1 or None in nets:
            what = sorted(name(k) if len(k) == 3 else f"item {k[1][:8]}" for k in group)
            problems.append(
                f"expected {', '.join(what)} on one net, found {sorted(map(str, nets))}"
            )
    return problems


def check(
    before: Netlist, after: Netlist, intent: NetIntent, label: PinLabel | None = None
) -> list[str]:
    """Problems (empty = verified): pins outside `may_change` keep their partition
    (apart from the merges the intent asks for); each `merge` group ends on one net;
    `names` match; with `keep_partition` the partition is identical."""
    name = label or _default_label
    problems: list[str] = []
    for group in intent.merge:
        nets = {after.net_of(k) for k in group}
        if len(nets) != 1 or None in nets:
            problems.append(
                f"expected {_describe(group, name)} on one net, found nets "
                + ", ".join(sorted(str(n) for n in nets))
            )
    for key, expected in intent.names.items():
        found = after.net_of(key)
        if found != expected:
            problems.append(f"expected {name(key)} on net {expected!r}, found {found!r}")
    b_keys, a_keys = _keys(before), _keys(after)
    if intent.keep_partition:
        universe = b_keys & a_keys
        expected_groups = _groups(before, universe)
    else:
        # Pins only in `before` belong to deleted symbols; pins only in `after` to new
        # ones (they must stay unconnected unless the intent names them).
        universe = (b_keys | a_keys) - intent.may_change - (b_keys - a_keys)
        uf = _UnionFind()
        for keys in before.nets.values():
            uf.union(keys)
        for group in intent.merge:
            uf.union(group)
        by_root: dict[PinKey, set[PinKey]] = {}
        for k in universe:
            by_root.setdefault(uf.find(k), set()).add(k)
        expected_groups = {k: frozenset(g) for g in by_root.values() for k in g}
    found_groups = _groups(after, universe)
    seen: set[frozenset[PinKey]] = set()
    for k in sorted(universe):
        exp, got = expected_groups[k], found_groups[k]
        if exp == got or got in seen:
            continue
        seen.add(got)
        joined, left = got - exp, exp - got
        parts = []
        if joined:
            parts.append(f"now also connected to {_describe(joined, name)}")
        if left:
            parts.append(f"no longer connected to {_describe(left, name)}")
        problems.append(f"{name(k)} " + " and ".join(parts))
        if len(problems) >= _MAX_PROBLEMS:
            problems.append("... more connectivity differences")
            break
    return problems
