"""The three §12 scenario circuits: user prompts + golden connectivity checkers + scoring.

Golden = connectivity by role (connector, resistor, LED, regulator, caps, MCU, sensor), not
by reference text, so any valid reference assignment and any layout pass. The netlist is
KiCad's live GetSchematicNetlist (2A Q3); ERC is `kicad-cli sch erc` on a saved copy.
"""

from __future__ import annotations

import re
from itertools import combinations

import geo
import tools

COMMON = (
    " The schematic is empty; nothing has to be preserved. When you are done, the ERC must have no "
    "errors and you must have looked at the result with render_view."
)

PROMPTS = {
    "s1": "Draw an indicator LED circuit: a 2-pin connector brings +5 V on pin 1 and GND on pin 2; a red LED "
    "is driven from it through a series resistor of about 1 kΩ. Use 0805 parts for the resistor and the LED." + COMMON,
    "s2": "Add a 3.3 V linear regulator (AMS1117-3.3 or a similar stock LDO) supplied from a 2-pin input connector "
    "(pin 1 = +5 V, pin 2 = GND). Put a 10 µF input capacitor and a 22 µF output capacitor (0805) next to the "
    "regulator. The regulated output rail is +3V3." + COMMON,
    "s3": "Build a small MCU block: an ATtiny85 (stock symbol, SOIC-8) powered from +3V3 with a 100 nF decoupling "
    "capacitor at its VCC pin, and a TMP102 I2C temperature sensor (stock symbol) on the MCU's I2C pins "
    "(USI: SDA = PB0, SCL = PB2) connected through net labels I2C_SDA and I2C_SCL, with 4.7 kΩ pull-ups to +3V3. "
    "The sensor gets its own 100 nF decoupling capacitor and its address pin goes to GND. Mark unused pins "
    "no-connect. Passives 0805." + COMMON,
}


def netlist(ctx) -> tuple[dict, dict]:
    """comps: ref -> {lib, value, footprint, pins{num: (name, type, net)}}; nets: name -> {(ref, pin)}."""
    scene, nets = tools.snapshot_state(ctx)
    comps, by_net = {}, {}
    for s in scene.syms.values():
        if s.power:
            continue
        c = comps.setdefault(s.ref, {"lib": s.lib_id, "value": s.value, "footprint": s.footprint, "pins": {}})
        for p in s.pins.values():
            n = nets.get(p.key) or f"unconnected-{s.ref}.{p.number}"
            c["pins"][p.number] = (p.name, p.etype, n)
            by_net.setdefault(n, set()).add((s.ref, p.number))
    return comps, by_net


def _find(comps, pat) -> list[str]:
    return sorted(r for r, c in comps.items() if re.search(pat, c["lib"]))


def _pin(comps, ref, names) -> tuple[str, str] | None:
    names = {n.upper() for n in names}
    for num, (name, _t, _n) in comps[ref]["pins"].items():
        parts = set(re.sub(r"[~{}]", "", name).upper().split("/"))
        if num.upper() in names or parts & names:
            return (ref, num)
    return None


def _net(comps, rp) -> str | None:
    return comps[rp[0]]["pins"][rp[1]][2] if rp else None


def _real(net: str | None) -> bool:
    return bool(net) and not net.startswith("unconnected-")


def check_s1(comps, nets) -> dict:
    c = {}
    J, R, D = _find(comps, r"^Connector"), _find(comps, r"^Device:R"), _find(comps, r"LED")
    c["one_connector_resistor_led"] = (len(J), len(R), len(D)) == (1, 1, 1)
    if not c["one_connector_resistor_led"]:
        return c
    j, r, d = J[0], R[0], D[0]
    j1, j2 = _net(comps, (j, "1")), _net(comps, (j, "2"))
    a, k = _net(comps, _pin(comps, d, ["A"])), _net(comps, _pin(comps, d, ["K"]))
    r1, r2 = _net(comps, (r, "1")), _net(comps, (r, "2"))

    def across_r(n):  # the net on the other side of the resistor
        return r2 if n == r1 else r1 if n == r2 else None

    loop_a = j1 in (r1, r2) and a == across_r(j1) and k == j2  # J1 - R - LED(A->K) - J2
    loop_b = a == j1 and k in (r1, r2) and across_r(k) == j2  # J1 - LED(A->K) - R - J2
    c["series_loop_led_polarity"] = bool(loop_a or loop_b)
    c["no_shorts"] = all(len(nets[n]) == 2 for n in {j1, j2, a, k, r1, r2} if _real(n)) and len({j1, j2, a, k, r1, r2}) == 3
    return c


def check_s2(comps, nets) -> dict:
    c = {}
    J, U, C = _find(comps, r"^Connector"), _find(comps, r"^Regulator"), _find(comps, r"^Device:C")
    c["parts_present"] = len(J) == 1 and len(U) == 1 and len(C) >= 2
    if not c["parts_present"]:
        return c
    j, u = J[0], U[0]
    vin = _net(comps, _pin(comps, u, ["VI", "VIN", "IN"]))
    vout = _net(comps, _pin(comps, u, ["VO", "VOUT", "OUT"]))
    gnd = _net(comps, _pin(comps, u, ["GND", "ADJ/GND"]))
    c["input_from_connector_pin1"] = _real(vin) and _net(comps, (j, "1")) == vin
    c["gnd_from_connector_pin2"] = _real(gnd) and _net(comps, (j, "2")) == gnd
    caps_in = [x for x in C if {_net(comps, (x, "1")), _net(comps, (x, "2"))} == {vin, gnd}]
    caps_out = [x for x in C if {_net(comps, (x, "1")), _net(comps, (x, "2"))} == {vout, gnd}]
    c["input_cap"] = bool(caps_in)
    c["output_cap"] = bool(caps_out)
    c["three_distinct_nets"] = len({vin, vout, gnd}) == 3 and all(_real(n) for n in (vin, vout, gnd))
    c["output_named_3V3"] = bool(vout) and geo.net_key(vout) in ("+3V3", "3V3", "+3.3V")
    return c


def check_s3(comps, nets) -> dict:
    c = {}
    M, S, C, R = _find(comps, r"^MCU_"), _find(comps, r"^Sensor"), _find(comps, r"^Device:C"), _find(comps, r"^Device:R")
    c["parts_present"] = len(M) == 1 and len(S) == 1 and len(C) >= 2 and len(R) >= 2
    if not c["parts_present"]:
        return c
    m, s = M[0], S[0]
    vcc = _net(comps, _pin(comps, m, ["VCC", "VDD"]))
    gnd = _net(comps, _pin(comps, m, ["GND", "VSS"]))
    c["mcu_powered"] = _real(vcc) and _real(gnd) and vcc != gnd
    c["supply_named_3V3"] = bool(vcc) and geo.net_key(vcc) in ("+3V3", "3V3", "+3.3V")
    c["sensor_powered"] = _net(comps, _pin(comps, s, ["V+", "VDD", "VCC"])) == vcc and _net(comps, _pin(comps, s, ["GND"])) == gnd
    decaps = [x for x in C if {_net(comps, (x, "1")), _net(comps, (x, "2"))} == {vcc, gnd}]
    c["two_decoupling_caps"] = len(decaps) >= 2
    sda, scl = _net(comps, _pin(comps, s, ["SDA"])), _net(comps, _pin(comps, s, ["SCL"]))
    c["sda_to_PB0"] = _real(sda) and _net(comps, _pin(comps, m, ["PB0"])) == sda
    c["scl_to_PB2"] = _real(scl) and _net(comps, _pin(comps, m, ["PB2"])) == scl
    c["i2c_nets_distinct"] = sda != scl
    c["i2c_labels_named"] = bool(sda and scl) and {geo.net_key(sda), geo.net_key(scl)} == {"I2C_SDA", "I2C_SCL"}
    pull = lambda n: any({_net(comps, (x, "1")), _net(comps, (x, "2"))} == {n, vcc} for x in R)  # noqa: E731
    c["pullups"] = _real(sda) and pull(sda) and pull(scl)
    c["address_to_gnd"] = _net(comps, _pin(comps, s, ["ADD0", "A0", "ADDR"])) == gnd
    return c


CHECKS = {"s1": check_s1, "s2": check_s2, "s3": check_s3}


def layout_checks(ctx) -> dict:
    scene = geo.read_scene(ctx.sch)
    syms = [s for s in scene.syms.values() if s.bbox]
    overlaps = [(a.ref, b.ref) for a, b in combinations(syms, 2) if geo.boxes_overlap(a.bbox, b.bbox, -0.05)]
    off = [s.ref for s in syms if not geo.on_grid(s.pos)]
    off += [f"wire {u[:8]}" for u, a, b in scene.wires if not (geo.on_grid(a) and geo.on_grid(b))]
    off += [f"label {t}" for _u, t, p, _k in scene.labels if not geo.on_grid(p)]
    # a wire interior over a pin end that is not its own end (silent short; must never happen)
    over = [
        f"{p.label}"
        for p in scene.all_pins()
        for _u, a, b in scene.wires
        if geo.on_segment(p.pos, a, b) and not (geo.same(p.pos, a) or geo.same(p.pos, b))
    ]
    return {
        "symbols": sum(1 for s in scene.syms.values() if not s.power),
        "power_symbols": sum(1 for s in scene.syms.values() if s.power),
        "wires": len(scene.wires),
        "labels": len(scene.labels),
        "junctions": len(scene.junctions),
        "no_connects": len(scene.no_connects),
        "symbol_overlaps": overlaps,
        "off_grid": off,
        "wire_over_pin": over,
    }


def score(name: str, ctx) -> dict:
    comps, nets = netlist(ctx)
    golden = CHECKS[name](comps, nets)
    e = tools.erc(ctx)
    passives = {r: c["footprint"] for r, c in comps.items() if tools.PASSIVE_SYMBOLS.match(c["lib"])}
    return {
        "golden": golden,
        "golden_ok": bool(golden) and all(golden.values()),
        "erc_errors": e["errors"],
        "erc_warnings": e["warnings"],
        "erc_violations": [f"{v['severity']}:{v['type']}:{'; '.join(v['items'])[:120]}" for v in e["violations"]],
        "layout": layout_checks(ctx),
        "components": {r: {"lib": c["lib"], "value": c["value"], "footprint": c["footprint"]} for r, c in sorted(comps.items())},
        "passives_not_0805": [r for r, f in passives.items() if "0805" not in f],
        "item_changes": ctx.item_changes,
    }
