from __future__ import annotations

from typing import Any

from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.filtering import matches, natural_key, not_found
from copperchat.tools.spec import ToolContext, ToolResult


def _sort_key(s: dict[str, Any]) -> tuple[Any, ...]:
    return (natural_key(s["ref"]), s["unit"], s["sheet"], s["uuid"])


def _symbols(h: KicadHandle) -> list[dict[str, Any]]:
    """One entry per placed unit per sheet instance (spike 1C, Q3)."""
    return sorted((access.symbol_dict(s) for s in h.schematic().get_symbols()), key=_sort_key)


def _symbol_with_pins(ref: str) -> Any:
    def run(h: KicadHandle) -> tuple[list[dict[str, Any]], list[str]]:
        hits: list[dict[str, Any]] = []
        refs: list[str] = []
        for s in h.schematic().get_symbols():
            refs.append(str(s.reference))
            if str(s.reference) == ref:
                hits.append(access.symbol_dict(s) | {"pins": access.symbol_pins(s)})
        return sorted(hits, key=_sort_key), refs

    return run


def _netlist(h: KicadHandle) -> dict[str, list[dict[str, str]]]:
    return access.netlist_nodes(h.schematic())


async def sch_list_symbols(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args.get("filter")
    syms = [
        s
        for s in await ctx.session.call(_symbols)
        if matches(pattern, s["ref"], s["value"], s["footprint"])
    ]
    return ToolResult(data={"count": len(syms), "symbols": syms})


async def sch_get_symbol(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ref = str(args["ref"])
    units, refs = await ctx.session.call(_symbol_with_pins(ref))
    if not units:
        raise not_found("Symbol", ref, refs)
    nets = await ctx.session.call(_netlist)
    net_of = {(n["ref"], n["pin"]): name for name, nodes in nets.items() for n in nodes}
    first = units[0]
    pins = [
        p | {"unit": u["unit"], "net": net_of.get((ref, p["pin"]))}
        for u in units
        for p in u["pins"]
    ]
    return ToolResult(
        data={
            "ref": ref,
            "value": first["value"],
            "footprint": first["footprint"],
            "libId": first["libId"],
            "fields": first["fields"],
            "units": [{k: u[k] for k in ("uuid", "unit", "position_mm", "sheet")} for u in units],
            "pins": sorted(pins, key=lambda p: (natural_key(p["pin"]), p["unit"])),
        }
    )


def _net_members(name: str) -> Any:
    def run(h: KicadHandle) -> tuple[list[dict[str, Any]] | None, list[str]]:
        sch = h.schematic()
        nets = access.netlist_nodes(sch)
        if name not in nets:
            return None, list(nets)
        wanted = {n["ref"] for n in nets[name]}
        placed: dict[tuple[str, str], dict[str, Any]] = {}
        for s in sch.get_symbols():
            if str(s.reference) not in wanted:
                continue
            sym = access.symbol_dict(s)
            for p in access.symbol_pins(s):
                placed.setdefault(
                    (sym["ref"], p["pin"]),
                    {"uuid": sym["uuid"], "sheet": sym["sheet"], "position_mm": p["position_mm"]},
                )
        pins = [
            {
                "ref": n["ref"],
                "pin": n["pin"],
                "function": n["function"],
                "type": n["type"],
                **placed.get(
                    (n["ref"], n["pin"]), {"uuid": None, "sheet": None, "position_mm": None}
                ),
            }
            for n in nets[name]
        ]
        return sorted(pins, key=lambda p: (natural_key(p["ref"]), natural_key(p["pin"]))), []

    return run


async def sch_get_net(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    name = str(args["name"])
    pins, names = await ctx.session.call(_net_members(name))
    if pins is None:
        raise not_found("Net", name, names)
    return ToolResult(data={"name": name, "pins": pins})


def symbol_boxes(h: KicadHandle) -> list[dict[str, Any]]:
    """Per placed unit: ref, sheet and the mm box spanned by its anchor and pins."""
    out: list[dict[str, Any]] = []
    for s in h.schematic().get_symbols():
        sym = access.symbol_dict(s)
        points = [sym["position_mm"], *(p["position_mm"] for p in access.symbol_pins(s))]
        xs = [p["x"] for p in points]
        ys = [p["y"] for p in points]
        out.append(
            {
                "ref": sym["ref"],
                "sheet": sym["sheet"],
                "bbox_mm": {
                    "x": min(xs),
                    "y": min(ys),
                    "w": round(max(xs) - min(xs), 4),
                    "h": round(max(ys) - min(ys), 4),
                },
            }
        )
    return sorted(out, key=lambda s: (natural_key(s["ref"]), s["sheet"]))
