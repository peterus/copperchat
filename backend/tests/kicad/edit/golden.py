"""Golden connectivity checkers for the three reference circuits (spec §12), ported from
spike 2D `scenarios.py` / `checks.py`.

Golden = connectivity **by role** (connector, resistor, LED, regulator, caps, MCU,
sensor found by `lib_id`, nets compared by pin), never by reference text or layout, so
any valid reference assignment passes. The same checker runs on KiCad's live netlist
(`live_components`) and on `kicad-cli sch export netlist` (`cli_components_full`), and
by the opt-in live eval (`tests/live/editing_scenarios.py`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

# ref -> {"lib": lib_id, "value", "footprint", "pins": {number: (name, net)}}
Components = dict[str, dict[str, Any]]
# net name -> {(ref, pin number)}
Nets = dict[str, set[tuple[str, str]]]

SCENARIOS = ("s1_led", "s2_ldo", "s3_mcu_i2c_subsheet")
PASSIVE = re.compile(r"^Device:(R|C|L|C_Polarized|R_Small|C_Small|L_Small|FerriteBead\w*)$")


def unconnected(ref: str, pin: str) -> str:
    return f"unconnected-{ref}.{pin}"


def net_key(net: str | None) -> str:
    """'/Sensor/I2C_SDA' -> 'I2C_SDA' (the hierarchical path is not part of the name)."""
    return (net or "").rsplit("/", 1)[-1]


def _real(net: str | None) -> bool:
    return bool(net) and not str(net).startswith("unconnected-")


def nets_of(components: Components) -> Nets:
    out: Nets = {}
    for ref, c in components.items():
        for num, (_name, net) in c["pins"].items():
            out.setdefault(net, set()).add((ref, num))
    return out


# -- sources -------------------------------------------------------------------------


def live_components(h: Any) -> Components:
    """From KiCad's live `GetSchematicNetlist` (power symbols left out, every sheet
    instance; a pin without a net or on a single-pin net counts as unconnected)."""
    from copperchat.kicad_tools import access
    from copperchat.kicad_tools.edit.scene import all_symbols

    raw = h.schematic().get_symbols()
    netlist = access.live_netlist(h, raw)
    sizes = {name: len(keys) for name, keys in netlist.nets.items()}
    fp = {access.kiid(s): access.field_text(s.footprint_field) for s in raw}
    out: Components = {}
    for info in all_symbols(h, raw):
        if info.power:
            continue
        c = out.setdefault(
            info.ref,
            {"lib": info.lib_id, "value": info.value, "footprint": fp.get(info.uuid, ""),
             "pins": {}},
        )  # fmt: skip
        for p in info.pins:
            net = netlist.net_of(p.key)
            if net is None or net.startswith("unconnected-") or sizes.get(net, 0) < 2:
                net = unconnected(info.ref, p.number)
            c["pins"][p.number] = (p.name, net)
    return out


def cli_components_full(root: ET.Element) -> Components:
    """From a `kicad-cli sch export netlist --format kicadxml` tree."""
    out: Components = {}
    for comp in root.iterfind("./components/comp"):
        ref = comp.get("ref", "")
        if ref.startswith("#"):
            continue
        src = comp.find("libsource")
        lib = f"{src.get('lib', '')}:{src.get('part', '')}" if src is not None else ""
        out[ref] = {
            "lib": lib,
            "value": comp.findtext("value", ""),
            "footprint": comp.findtext("footprint", ""),
            "pins": {},
        }
    for net in root.iterfind("./nets/net"):
        name = net.get("name", "")
        nodes = [n for n in net.iterfind("node") if n.get("ref", "") in out]
        all_nodes = list(net.iterfind("node"))
        for node in nodes:
            ref, pin = node.get("ref", ""), node.get("pin", "")
            net_name = name
            if name.startswith("unconnected-") or len(all_nodes) < 2:
                net_name = unconnected(ref, pin)
            # kicad-cli names a pin function NAME_number at the pinned commit
            function = node.get("pinfunction", "").removesuffix(f"_{pin}")
            out[ref]["pins"][pin] = (function, net_name)
    return out


# -- role helpers --------------------------------------------------------------------


def _find(comps: Components, pat: str) -> list[str]:
    return sorted(r for r, c in comps.items() if re.search(pat, c["lib"]))


def _pin(comps: Components, ref: str, names: list[str]) -> tuple[str, str] | None:
    wanted = {n.upper() for n in names}
    for num, (name, _net) in comps[ref]["pins"].items():
        parts = set(re.sub(r"[~{}]", "", name).upper().split("/"))
        if num.upper() in wanted or parts & wanted:
            return (ref, num)
    return None


def _net(comps: Components, rp: tuple[str, str] | None) -> str | None:
    if rp is None:
        return None
    pins = comps[rp[0]]["pins"]
    return pins[rp[1]][1] if rp[1] in pins else unconnected(*rp)


def _two(comps: Components, ref: str) -> set[str | None]:
    return {_net(comps, (ref, "1")), _net(comps, (ref, "2"))}


def norm_value(v: str) -> str:
    v = v.lower().replace("ω", "").replace("ohm", "").replace(" ", "").replace("µ", "u")
    return v.replace("f", "").replace("4k7", "4.7k")


# -- the three circuits ----------------------------------------------------------------


def check_s1(comps: Components, nets: Nets) -> dict[str, bool]:
    c: dict[str, bool] = {}
    J, R, D = _find(comps, r"^Connector"), _find(comps, r"^Device:R"), _find(comps, r"LED")
    c["one_connector_resistor_led"] = (len(J), len(R), len(D)) == (1, 1, 1)
    if not c["one_connector_resistor_led"]:
        return c
    j, r, d = J[0], R[0], D[0]
    j1, j2 = _net(comps, (j, "1")), _net(comps, (j, "2"))
    a, k = _net(comps, _pin(comps, d, ["A"])), _net(comps, _pin(comps, d, ["K"]))
    r1, r2 = _net(comps, (r, "1")), _net(comps, (r, "2"))

    def across_r(n: str | None) -> str | None:
        return r2 if n == r1 else r1 if n == r2 else None

    loop_a = j1 in (r1, r2) and a == across_r(j1) and k == j2  # J.1 - R - LED(A->K) - J.2
    loop_b = a == j1 and k in (r1, r2) and across_r(k) == j2  # J.1 - LED(A->K) - R - J.2
    c["series_loop_led_polarity"] = bool(loop_a or loop_b)
    used = {j1, j2, a, k, r1, r2}
    c["no_shorts"] = len(used) == 3 and all(len(nets[n]) == 2 for n in used if n and _real(n))
    c["connector_rails"] = net_key(j1) == "+5V" and net_key(j2) == "GND"
    return c


def check_s2(comps: Components, nets: Nets) -> dict[str, bool]:
    c: dict[str, bool] = {}
    J, U = _find(comps, r"^Connector"), _find(comps, r"^Regulator")
    C = _find(comps, r"^Device:C")
    c["parts_present"] = len(J) == 1 and len(U) == 1 and len(C) >= 2
    if not c["parts_present"]:
        return c
    j, u = J[0], U[0]
    vin = _net(comps, _pin(comps, u, ["VI", "VIN", "IN"]))
    vout = _net(comps, _pin(comps, u, ["VO", "VOUT", "OUT"]))
    gnd = _net(comps, _pin(comps, u, ["GND", "ADJ/GND"]))
    c["input_from_connector_pin1"] = _real(vin) and _net(comps, (j, "1")) == vin
    c["gnd_from_connector_pin2"] = _real(gnd) and _net(comps, (j, "2")) == gnd
    caps_in = [x for x in C if _two(comps, x) == {vin, gnd}]
    caps_out = [x for x in C if _two(comps, x) == {vout, gnd}]
    c["input_cap"] = bool(caps_in)
    c["output_cap"] = bool(caps_out)
    c["three_distinct_nets"] = len({vin, vout, gnd}) == 3 and all(
        _real(n) for n in (vin, vout, gnd)
    )
    c["output_named_3V3"] = net_key(vout) in ("+3V3", "3V3", "+3.3V")
    c["input_cap_10u"] = any(norm_value(comps[x]["value"]) == "10u" for x in caps_in)
    c["output_cap_22u"] = any(norm_value(comps[x]["value"]) == "22u" for x in caps_out)
    return c


def check_s3(comps: Components, nets: Nets) -> dict[str, bool]:
    c: dict[str, bool] = {}
    M, S = _find(comps, r"^MCU_"), _find(comps, r"^Sensor")
    C, R = _find(comps, r"^Device:C"), _find(comps, r"^Device:R")
    c["parts_present"] = len(M) == 1 and len(S) == 1 and len(C) >= 2 and len(R) >= 2
    if not c["parts_present"]:
        return c
    m, s = M[0], S[0]
    vcc = _net(comps, _pin(comps, m, ["VCC", "VDD"]))
    gnd = _net(comps, _pin(comps, m, ["GND", "VSS"]))
    c["mcu_powered"] = _real(vcc) and _real(gnd) and vcc != gnd
    c["supply_named_3V3"] = net_key(vcc) in ("+3V3", "3V3", "+3.3V")
    c["sensor_powered"] = (
        _net(comps, _pin(comps, s, ["V+", "VDD", "VCC"])) == vcc
        and _net(comps, _pin(comps, s, ["GND"])) == gnd
    )
    c["two_decoupling_caps"] = len([x for x in C if _two(comps, x) == {vcc, gnd}]) >= 2
    sda, scl = _net(comps, _pin(comps, s, ["SDA"])), _net(comps, _pin(comps, s, ["SCL"]))
    c["sda_to_PB0"] = _real(sda) and _net(comps, _pin(comps, m, ["PB0"])) == sda
    c["scl_to_PB2"] = _real(scl) and _net(comps, _pin(comps, m, ["PB2"])) == scl
    c["i2c_nets_distinct"] = sda != scl
    c["i2c_labels_named"] = {net_key(sda), net_key(scl)} == {"I2C_SDA", "I2C_SCL"}

    def pulled(n: str | None) -> bool:
        return any(_two(comps, x) == {n, vcc} for x in R)

    c["pullups"] = _real(sda) and _real(scl) and pulled(sda) and pulled(scl)
    c["address_to_gnd"] = _net(comps, _pin(comps, s, ["ADD0", "A0", "ADDR"])) == gnd
    return c


CHECKS: dict[str, Callable[[Components, Nets], dict[str, bool]]] = {
    "s1_led": check_s1,
    "s2_ldo": check_s2,
    "s3_mcu_i2c_subsheet": check_s3,
}


def check(scenario: str, netlist: Nets, components: Components) -> dict[str, Any]:
    """{"golden": bool, "missing": [failed check names], "checks": {name: bool}}; the
    checks include the 0805 rule for passives (`passives_0805`)."""
    checks = dict(CHECKS[scenario](components, netlist))
    checks["passives_0805"] = all(
        "0805" in str(c["footprint"]) for c in components.values() if PASSIVE.match(c["lib"])
    )
    missing = sorted(k for k, ok in checks.items() if not ok)
    return {"golden": not missing, "missing": missing, "checks": checks}
