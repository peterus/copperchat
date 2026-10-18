"""Prompts, start states and scoring of the opt-in live editing eval (E10 Task 4, spec §12),
ported from spike 2D (`eval.py`, `scenarios.py`). Scored on golden connectivity by role
(`tests/kicad/edit/golden.py`), ERC errors, layout, tool calls, tokens and cost."""

from __future__ import annotations

from typing import Any

from tests.kicad.edit.test_scenarios import Step

COMMON = (
    " The schematic is empty; nothing has to be preserved. When you are done, the ERC must "
    "have no errors and you must have looked at the result with render_view."
)

PROMPTS: dict[str, str] = {
    "s1_led": (
        "Draw an indicator LED circuit: a 2-pin connector brings +5 V on pin 1 and GND on "
        "pin 2; a red LED is driven from it through a series resistor of about 1 kΩ. Use "
        "0805 parts for the resistor and the LED." + COMMON
    ),
    "s2_ldo": (
        "Add a 3.3 V linear regulator (AMS1117-3.3 or a similar stock LDO) supplied from a "
        "2-pin input connector (pin 1 = +5 V, pin 2 = GND). Put a 10 µF input capacitor and "
        "a 22 µF output capacitor (0805) next to the regulator. The regulated output rail is "
        "+3V3." + COMMON
    ),
    "s3_mcu_i2c_subsheet": (
        "Build a small MCU block: an ATtiny85 (stock symbol, SOIC-8) powered from +3V3 with a "
        "100 nF decoupling capacitor at its VCC pin, and a TMP102 I2C temperature sensor "
        "(stock symbol) on its own hierarchical sub-sheet named Sensor, connected to the "
        "MCU's I2C pins (USI: SDA = PB0, SCL = PB2) through sheet pins I2C_SDA and I2C_SCL, "
        "with 4.7 kΩ pull-ups to +3V3 on the MCU side. The sensor gets its own 100 nF "
        "decoupling capacitor and its address pin goes to GND. Mark unused pins no-connect. "
        "Passives 0805." + COMMON
    ),
    "s4_resize_0402": (
        "Change all 0603 resistors in this schematic to 0402 footprints. Leave everything "
        "else as it is."
    ),
}

R0603 = "Resistor_SMD:R_0603_1608Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
R0402 = "Resistor_SMD:R_0402_1005Metric"

# Start state of s4 (built with the tools before the agent runs); s1-s3 start blank.
SETUP: dict[str, list[Step]] = {
    "s4_resize_0402": [
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [76.2, 76.2], "value": "10k",
                              "footprint": R0603}, "RA"),
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [91.44, 76.2], "value": "10k",
                              "footprint": R0603}, "RB"),
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [106.68, 76.2], "value": "1k",
                              "footprint": R0603}, "RC"),
        ("sch_place_symbol", {"lib_id": "Device:C", "at": [121.92, 76.2], "value": "100nF",
                              "footprint": C0603}, "C"),
        ("sch_add_power", {"net": "+3V3", "at": ["{RA}.1", "{RB}.1", "{RC}.1", "{C}.1"]}),
        ("sch_add_power", {"net": "GND", "at": ["{RA}.2", "{RB}.2", "{RC}.2", "{C}.2"]}),
    ],
}  # fmt: skip

SCENARIOS = tuple(PROMPTS)


def partition(components: dict[str, Any]) -> set[frozenset[tuple[str, str]]]:
    """The pin partition by (ref, pin): connectivity independent of net names."""
    nets: dict[str, set[tuple[str, str]]] = {}
    for ref, c in components.items():
        for num, (_name, net) in c["pins"].items():
            nets.setdefault(net, set()).add((ref, num))
    return {frozenset(v) for v in nets.values()}


def check_resize(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """s4: every resistor 0402, every other part unchanged, connectivity identical."""
    resistors = [r for r, c in after.items() if c["lib"] == "Device:R"]
    checks = {
        "same_parts": set(before) == set(after),
        "resistors_0402": bool(resistors)
        and all(after[r]["footprint"] == R0402 for r in resistors),
        "others_unchanged": all(
            after[r]["footprint"] == c["footprint"] and after[r]["value"] == c["value"]
            for r, c in before.items()
            if c["lib"] != "Device:R" and r in after
        ),
        "values_unchanged": all(
            after[r]["value"] == c["value"] for r, c in before.items() if r in after
        ),
        "connectivity_unchanged": partition(before) == partition(after),
    }
    missing = sorted(k for k, ok in checks.items() if not ok)
    return {"golden": not missing, "missing": missing, "checks": checks}
