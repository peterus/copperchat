"""Deterministic tool check (no model): builds the three scenario circuits with fixed tool
calls, prints every result, checks them with the golden checkers. Validates the prototype
before any model money is spent. Usage: ./run.sh python scripted.py [s1|s2|s3]..."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import Server

import kit
import scenarios
import tools

SCRIPTS = {
    "s1": [
        (
            "sch_place_symbol",
            {
                "lib_id": "Connector_Generic:Conn_01x02",
                "at": [60.96, 76.2],
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            },
        ),
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [81.28, 63.5], "value": "1k", "footprint": "Resistor_SMD:R_0805_2012Metric"}),
        (
            "sch_place_symbol",
            {"lib_id": "Device:LED", "at": [81.28, 81.28], "rotation": 90, "value": "red", "footprint": "LED_SMD:LED_0805_2012Metric"},
        ),
        ("sch_connect", {"from": "J1.1", "to": "R1.1"}),
        ("sch_connect", {"from": "R1.2", "to": "D1.A"}),
        ("sch_connect", {"from": "D1.K", "to": "J1.2"}),
        ("sch_get_pin", {"pins": ["J1.1", "D1.2"]}),
    ],
    "s2": [
        (
            "sch_place_symbol",
            {
                "lib_id": "Connector_Generic:Conn_01x02",
                "at": [50.8, 76.2],
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            },
        ),
        ("sch_place_symbol", {"lib_id": "Regulator_Linear:AMS1117-3.3", "at": [101.6, 73.66]}),
        ("sch_place_symbol", {"lib_id": "Device:C", "at": [83.82, 81.28], "value": "10uF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}),
        (
            "sch_place_symbol",
            {"lib_id": "Device:C", "at": [119.38, 81.28], "value": "22uF", "footprint": "Capacitor_SMD:C_0805_2012Metric"},
        ),
        ("sch_add_power", {"net": "+5V", "at": ["J1.1", "U1.VI", "C1.1"]}),
        ("sch_add_power", {"net": "GND", "at": ["J1.2", "U1.GND", "C1.2", "C2.2"]}),
        ("sch_add_power", {"net": "+3V3", "at": ["U1.VO", "C2.1"]}),
        ("sch_add_power", {"net": "PWR_FLAG", "at": ["J1.1", "J1.2"]}),
        ("sch_add_label", {"net": "VIN_TEST", "at": "U1.VI"}),
        ("run_erc", {}),
    ],
    "s3": [
        ("lib_get_symbol", {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S"}),
        ("sch_place_symbol", {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "at": [101.6, 101.6]}),
        ("sch_place_symbol", {"lib_id": "Sensor_Temperature:TMP102xxDRL", "at": [170.18, 101.6]}),
        ("sch_place_symbol", {"lib_id": "Device:C", "at": [63.5, 101.6], "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}),
        ("sch_add_power", {"net": "+3V3", "at": ["U1.VCC", "C1.1", "U2.V+"]}),
        ("sch_add_power", {"net": "GND", "at": ["U1.GND", "C1.2", "U2.GND", "U2.ADD0"]}),
        ("sch_connect", {"from": "U1.PB0", "to": "U2.SDA", "via": "label", "net_name": "I2C_SDA"}),
        ("sch_connect", {"from": "U1.PB2", "to": "U2.SCL", "via": "label", "net_name": "I2C_SCL"}),
        ("sch_no_connect", {"pins": ["U1.1", "U1.2", "U1.3", "U1.6", "U2.ALERT"]}),
        ("sch_add_power", {"net": "PWR_FLAG", "at": ["U1.VCC", "U1.GND"]}),
        (
            "sch_place_symbol",
            {"lib_id": "Device:C", "at": [203.2, 101.6], "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"},
        ),
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [137.16, 83.82], "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric"}),
        ("sch_place_symbol", {"lib_id": "Device:R", "at": [147.32, 83.82], "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric"}),
        ("sch_add_power", {"net": "+3V3", "at": ["C2.1", "R1.1", "R2.1"]}),
        ("sch_add_power", {"net": "GND", "at": ["C2.2"]}),
        ("sch_connect", {"from": "R1.2", "to": "I2C_SDA"}),
        ("sch_connect", {"from": "R2.2", "to": "I2C_SCL"}),
        ("run_erc", {}),
    ],
    # v2 additions: find_space, move, delete, misuse guards
    "v2": [
        ("sch_place_symbol", {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "at": [101.6, 101.6]}),
        ("sch_find_space", {"lib_id": "Device:C", "near": "U1.VCC"}),
        ("sch_place_symbol", {"lib_id": "Device:C", "at": [60.96, 60.96], "value": "100nF"}),
        ("sch_move", {"ref": "C1", "to": [78.74, 63.5], "rotation": 90}),
        ("sch_add_power", {"net": "GND", "at": ["U1.GND", "C1.2"]}),
        ("sch_place_symbol", {"lib_id": "power:GND", "at": [50.8, 50.8]}),
        ("sch_add_label", {"net": "+3V3", "at": "U1.VCC"}),
        ("sch_connect", {"from": "U1.VCC", "to": "+3V3"}),
        ("sch_move", {"ref": "C1", "to": [60.96, 60.96]}),
        ("lib_get_symbol", {"lib_id": "Device:LED"}),
        ("sch_delete", {"refs": ["C1"]}),
        ("sch_list_symbols", {"include_power": True}),
    ],
}


def main() -> int:
    wanted = sys.argv[1:] or list(SCRIPTS)
    rc = 0
    for name in wanted:
        proj = kit.fresh_blank()
        out = Path("results/scripted") / name
        with Server(proj) as srv:
            k = srv.client(timeout_ms=60000)
            kit.load_libraries(k)
            ctx = tools.Ctx(k=k, sch=k.get_schematic(), lib=kit.Library(k), project_dir=proj, out_dir=out, version=2 if name == "v2" else 1)
            for tool, args in SCRIPTS[name]:
                res = tools.call(ctx, tool, args)
                print(f"--- {tool} {json.dumps(args)}\n{json.dumps(res)[:1500]}")
            tools.render_png(ctx, name="final.png")
            if name not in scenarios.CHECKS:
                continue
            score = scenarios.score(name, ctx)
            print("SCORE", json.dumps(score, indent=1))
            rc |= 0 if score["golden_ok"] else 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
