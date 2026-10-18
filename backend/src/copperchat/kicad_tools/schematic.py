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


def _pin_nets(h: KicadHandle) -> dict[tuple[str, str], str]:
    """(ref, pin) -> net name from the live netlist (sees unsaved edits)."""
    sch = h.schematic()
    refs = {(access.sheet_path_key(s.path), access.kiid(s)): str(s.reference)
            for s in sch.get_symbols()}  # fmt: skip
    return {
        (refs[(path, uuid)], number): name
        for name, keys in access.live_netlist(h).nets.items()
        for path, uuid, number in keys
        if (path, uuid) in refs
    }


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
    net_of = await ctx.session.call(_pin_nets)
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


def _pin_function(name: str, number: str) -> str:
    """The pin's function as kicad-cli's netlist names it at the pinned commit:
    `NAME_number`, `""` for an unnamed (`~`) pin."""
    return "" if name in ("", "~") else f"{name}_{number}"


def _net_members(name: str) -> Any:
    def run(h: KicadHandle) -> tuple[list[dict[str, Any]] | None, list[str]]:
        sch = h.schematic()
        nets = access.live_netlist(h).nets
        if name not in nets:
            return None, list(nets)
        wanted = {(path, uuid) for path, uuid, _ in nets[name]}
        pins: dict[tuple[str, str], dict[str, Any]] = {}
        for s in sch.get_symbols():
            path, uuid, ref = access.sheet_path_key(s.path), access.kiid(s), str(s.reference)
            # Power symbols are no netlist components (as in kicad-cli's netlist).
            if (path, uuid) not in wanted or ref.startswith("#"):
                continue
            sym = access.symbol_dict(s)
            for p in access.symbol_pins(s):
                if (path, uuid, p["pin"]) not in nets[name]:
                    continue
                pins.setdefault(
                    (ref, p["pin"]),
                    {
                        "ref": ref,
                        "pin": p["pin"],
                        "function": _pin_function(p["name"], p["pin"]),
                        "type": p["type"],
                        "uuid": sym["uuid"],
                        "sheet": sym["sheet"],
                        "position_mm": p["position_mm"],
                    },
                )
        return sorted(
            pins.values(), key=lambda p: (natural_key(p["ref"]), natural_key(p["pin"]))
        ), []

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
