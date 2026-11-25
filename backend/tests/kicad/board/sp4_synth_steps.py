"""The `sp4_synth` circuit as agent tool steps (P1b, spec §18.2): a USB-C powered
STM32F042 board, 26 SMD-heavy parts on two sheets, built from KiCad's empty project by
`test_sp4_synth_build.py` through the real schematic tools (`run_script` of
`tests/kicad/edit/test_scenarios.py`). Aliases are the references the parts get.

Pins are named by number: the MCU's USB pins are called "PA9/PA11" / "PA10/PA12", and
the USB-C receptacle repeats VBUS/GND/D+/D- (A4 and A1 carry the stacked hidden pins).
References follow the placement order (root sheet first), so the ESD part is U2 and the
buck on the Power sheet U3.
"""

from __future__ import annotations

from typing import Any

Step = tuple[Any, ...]

R0603 = "Resistor_SMD:R_0603_1608Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
MCU = "MCU_ST_STM32F0:STM32F042F6Px"
MCU_FP = "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm"
XTAL = "Device:Crystal_GND24"
XTAL_FP = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"
USB_C = "Connector:USB_C_Receptacle_USB2.0_16P"
USB_C_FP = "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12"
ESD = "Power_Protection:USBLC6-2SC6"
SOT23_6 = "Package_TO_SOT_SMD:SOT-23-6"
LED_FP = "LED_SMD:LED_0603_1608Metric"
HEADER = "Connector_Generic:Conn_01x04"
HEADER_FP = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
HOLE = "Mechanical:MountingHole"
HOLE_FP = "MountingHole:MountingHole_2.2mm_M2"
BUCK = "Regulator_Switching:TPS563200"
INDUCTOR_FP = "Inductor_SMD:L_Changjiang_FNR4030S"
POWER = "/Power/"


def _part(alias: str, lib_id: str, at: list[float], footprint: str, **kw: Any) -> Step:
    return ("sch_place_symbol", {"lib_id": lib_id, "at": at, "footprint": footprint, **kw}, alias)


ROOT_STEPS: list[Step] = [
    _part("U1", MCU, [165.1, 101.6], MCU_FP),
    _part("C1", "Device:C", [203.2, 58.42], C0603, value="100nF"),
    _part("C2", "Device:C", [215.9, 58.42], C0603, value="100nF"),
    _part("C3", "Device:C", [228.6, 58.42], C0805, value="4.7uF"),
    _part("C4", "Device:C", [114.3, 160.02], C0603, value="12pF"),
    _part("C5", "Device:C", [139.7, 160.02], C0603, value="12pF"),
    _part("C6", "Device:C", [124.46, 63.5], C0603, value="100nF"),
    _part("R1", "Device:R", [137.16, 63.5], R0603, value="10k"),
    _part("R2", "Device:R", [76.2, 137.16], R0603, value="5.1k"),
    _part("R3", "Device:R", [88.9, 137.16], R0603, value="5.1k"),
    _part("R4", "Device:R", [198.12, 96.52], R0603, rotation=90, value="1k"),
    _part("Y1", XTAL, [127.0, 144.78], XTAL_FP, value="8MHz"),
    _part("J1", USB_C, [45.72, 101.6], USB_C_FP),
    _part("U2", ESD, [99.06, 73.66], SOT23_6),
    _part("D1", "Device:LED", [212.09, 96.52], LED_FP, rotation=180, value="green"),
    _part("J2", HEADER, [246.38, 137.16], HEADER_FP, mirror="y", value="SWD"),
    _part("H1", HOLE, [241.3, 38.1], HOLE_FP),
    _part("H2", HOLE, [256.54, 35.56], HOLE_FP),
    # nets by label first, so the few wires below route around them
    ("sch_add_label", {"net": "USB_DP", "at": ["{J1}.A6", "{J1}.B6", "{U2}.1", "{U2}.6",
                                               "{U1}.18"]}),
    ("sch_add_label", {"net": "USB_DN", "at": ["{J1}.A7", "{J1}.B7", "{U2}.3", "{U2}.4",
                                               "{U1}.17"]}),
    ("sch_add_label", {"net": "CC1", "at": ["{J1}.A5", "{R2}.1"]}),
    ("sch_add_label", {"net": "CC2", "at": ["{J1}.B5", "{R3}.1"]}),
    ("sch_add_label", {"net": "NRST", "at": ["{U1}.4", "{C6}.1"]}),
    ("sch_add_label", {"net": "BOOT0", "at": ["{U1}.1", "{R1}.1"]}),
    ("sch_add_label", {"net": "OSC_IN", "at": ["{U1}.2", "{Y1}.1", "{C4}.1"]}),
    ("sch_add_label", {"net": "OSC_OUT", "at": ["{U1}.3", "{Y1}.3", "{C5}.1"]}),
    ("sch_add_label", {"net": "SWDIO", "at": ["{U1}.19", "{J2}.2"]}),
    ("sch_add_label", {"net": "SWCLK", "at": ["{U1}.20", "{J2}.3"]}),
    # status LED on PA4: U1 -> R4 -> D1 -> GND, straight wires
    ("sch_connect", {"from": "{U1}.10", "to": "{R4}.1"}),
    ("sch_connect", {"from": "{R4}.2", "to": "{D1}.2"}),
    # rails (Y1.4 is stacked under Y1.2, J1's hidden VBUS/GND pins under A4/A1)
    ("sch_add_power", {"net": "VBUS", "at": ["{J1}.A4", "{U2}.5"]}),
    ("sch_add_power", {"net": "+3V3", "at": ["{U1}.16", "{C1}.1", "{C2}.1", "{C3}.1",
                                             "{J2}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{J1}.A1", "{J1}.SH", "{U2}.2", "{U1}.15",
                                            "{C1}.2", "{C2}.2", "{C3}.2", "{C4}.2", "{C5}.2",
                                            "{C6}.2", "{R1}.2", "{Y1}.2", "{R2}.2", "{R3}.2",
                                            "{D1}.1", "{J2}.4"]}),
    ("sch_add_power", {"net": "PWR_FLAG", "at": ["{J1}.A4", "{J1}.A1", "{C3}.1"]}),
    ("sch_connect", {"from": "{U1}.5", "to": "{U1}.16"}),  # VDDA beside VDD: one symbol
    ("sch_no_connect", {"pins": ["{J1}.A8", "{J1}.B8"]}),
    ("sch_no_connect", {"pins": ["{U1}.6", "{U1}.7", "{U1}.8", "{U1}.9", "{U1}.11",
                                 "{U1}.12", "{U1}.13", "{U1}.14"]}),
]  # fmt: skip

# The buck sheet connects through the global power nets only (no sheet pins).
POWER_STEPS: list[Step] = [
    ("sch_add_sheet", {"name": "Power", "file": "power.kicad_sch", "at": [25.4, 30.48],
                       "size": [38.1, 20.32]}),
    _part("U3", BUCK, [101.6, 101.6], SOT23_6, sheet=POWER),
    _part("L1", "Device:L", [137.16, 93.98], INDUCTOR_FP, rotation=90, value="2.2uH",
          sheet=POWER),
    _part("C7", "Device:C", [71.12, 109.22], C0805, value="10uF", sheet=POWER),
    _part("C8", "Device:C", [162.56, 109.22], C0805, value="22uF", sheet=POWER),
    _part("C9", "Device:C", [175.26, 109.22], C0805, value="22uF", sheet=POWER),
    _part("C10", "Device:C", [119.38, 81.28], C0603, value="100nF", sheet=POWER),
    _part("R5", "Device:R", [149.86, 114.3], R0603, value="33.2k", sheet=POWER),
    _part("R6", "Device:R", [149.86, 132.08], R0603, value="10k", sheet=POWER),
    ("sch_add_label", {"net": "SW", "at": ["{U3}.2", "{C10}.2", "{L1}.1"]}),
    ("sch_add_label", {"net": "FB", "at": ["{U3}.4", "{R5}.2"]}),
    ("sch_connect", {"from": "{R6}.1", "to": "{R5}.2"}),
    ("sch_connect", {"from": "{U3}.6", "to": "{C10}.1"}),
    ("sch_add_power", {"net": "VBUS", "at": ["{U3}.3", "{U3}.5", "{C7}.1"]}),
    ("sch_add_power", {"net": "GND", "at": ["{U3}.1", "{C7}.2", "{C8}.2", "{C9}.2",
                                            "{R6}.2"]}),
    ("sch_add_power", {"net": "+3V3", "at": ["{L1}.2", "{C8}.1", "{C9}.1", "{R5}.1"]}),
]  # fmt: skip

# The table of phase-p1b-synth-fixture.md (alias = reference -> lib id, footprint).
EXPECTED_PARTS: dict[str, tuple[str, str]] = {
    "U1": (MCU, MCU_FP),
    **{c: ("Device:C", C0603) for c in ("C1", "C2", "C4", "C5", "C6", "C10")},
    **{c: ("Device:C", C0805) for c in ("C3", "C7", "C8", "C9")},
    **{r: ("Device:R", R0603) for r in ("R1", "R2", "R3", "R4", "R5", "R6")},
    "Y1": (XTAL, XTAL_FP),
    "J1": (USB_C, USB_C_FP),
    "U2": (ESD, SOT23_6),
    "D1": ("Device:LED", LED_FP),
    "J2": (HEADER, HEADER_FP),
    "H1": (HOLE, HOLE_FP),
    "H2": (HOLE, HOLE_FP),
    "U3": (BUCK, SOT23_6),
    "L1": ("Device:L", INDUCTOR_FP),
}
