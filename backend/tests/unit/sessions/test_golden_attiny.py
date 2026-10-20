"""The golden checker of the sessions live eval (spec §14.4): USB-C 5 V, an ATtiny85, one
LED with a resistor, a 6-pin ISP header. Pure data, no KiCad."""

from __future__ import annotations

import copy
from typing import Any

from tests.kicad.edit import golden

SCENARIO = "sessions_attiny85_usbc"
R0805 = "Resistor_SMD:R_0805_2012Metric"


def _good() -> golden.Components:
    return {
        "J1": {
            "lib": "Connector:USB_C_Receptacle_PowerOnly_6P",
            "value": "USB_C",
            "footprint": "x",
            "pins": {"A9": ("VBUS", "VBUS"), "A12": ("GND", "GND"), "A5": ("CC1", "CC1")},
        },
        "R2": {"lib": "Device:R", "value": "5.1k", "footprint": R0805,
               "pins": {"1": ("~", "CC1"), "2": ("~", "GND")}},
        "U1": {
            "lib": "MCU_Microchip_ATtiny:ATtiny85-20S",
            "value": "ATtiny85",
            "footprint": "x",
            "pins": {
                "1": ("~{RESET}/PB5", "RST"),
                "2": ("XTAL1/PB3", "unconnected-U1.2"),
                "3": ("XTAL2/PB4", "LED_DRV"),
                "4": ("GND", "GND"),
                "5": ("AREF/PB0", "MOSI"),
                "6": ("PB1", "MISO"),
                "7": ("PB2", "SCK"),
                "8": ("VCC", "VBUS"),
            },
        },
        "C1": {"lib": "Device:C", "value": "100n", "footprint": "Capacitor_SMD:C_0805_2012Metric",
               "pins": {"1": ("~", "VBUS"), "2": ("~", "GND")}},
        "R1": {"lib": "Device:R", "value": "330", "footprint": R0805,
               "pins": {"1": ("~", "LED_DRV"), "2": ("~", "LED_A")}},
        "D1": {"lib": "Device:LED", "value": "Red", "footprint": "x",
               "pins": {"1": ("K", "GND"), "2": ("A", "LED_A")}},
        "J2": {
            "lib": "Connector:AVR-ISP-6",
            "value": "ISP",
            "footprint": "x",
            "pins": {
                "1": ("MISO", "MISO"), "2": ("VCC", "VBUS"), "3": ("SCK", "SCK"),
                "4": ("MOSI", "MOSI"), "5": ("~{RST}", "RST"), "6": ("GND", "GND"),
            },
        },
    }  # fmt: skip


def _check(comps: golden.Components) -> dict[str, Any]:
    return golden.check(SCENARIO, golden.nets_of(comps), comps)


def test_reference_design_is_golden() -> None:
    res = _check(_good())
    assert res["golden"], res["missing"]


def test_generic_2x3_header_counts_by_pin_number() -> None:
    comps = _good()
    comps["J2"]["lib"] = "Connector_Generic:Conn_02x03_Odd_Even"
    comps["J2"]["pins"] = {n: (f"Pin_{n}", net) for n, (_, net) in comps["J2"]["pins"].items()}
    assert _check(comps)["golden"]


def test_vcc_through_a_fuse_or_ferrite_counts_as_usb_powered() -> None:
    comps = _good()
    for ref in ("U1", "C1", "J2"):
        pins = comps[ref]["pins"]
        for n, (name, net) in pins.items():
            if net == "VBUS":
                pins[n] = (name, "+5V")
    comps["F1"] = {"lib": "Device:Polyfuse", "value": "500mA", "footprint": "x",
                   "pins": {"1": ("~", "VBUS"), "2": ("~", "+5V")}}  # fmt: skip
    assert _check(comps)["golden"]


def test_swapped_isp_lines_fail() -> None:
    comps = _good()
    comps["J2"]["pins"]["1"] = ("MISO", "MOSI")
    comps["J2"]["pins"]["4"] = ("MOSI", "MISO")
    assert "isp_pins" in _check(comps)["missing"]


def test_missing_decoupling_led_resistor_fail() -> None:
    comps = copy.deepcopy(_good())
    comps["C1"]["pins"]["2"] = ("~", "unconnected-C1.2")
    comps["R1"]["pins"]["1"] = ("~", "VBUS")  # LED no longer driven by an MCU pin
    missing = _check(comps)["missing"]
    assert "mcu_decoupling" in missing and "led_with_resistor" in missing


def test_unpowered_mcu_fails() -> None:
    comps = _good()
    comps["U1"]["pins"]["8"] = ("VCC", "unconnected-U1.8")
    assert "mcu_powered_from_usb" in _check(comps)["missing"]


def test_led_tied_to_rails_or_reversed_fails() -> None:
    comps = _good()
    comps["U1"]["pins"]["3"] = ("XTAL2/PB4", "unconnected-U1.3")
    comps["R1"]["pins"]["1"] = ("~", "VBUS")  # a power LED, not driven by the MCU
    assert "led_with_resistor" in _check(comps)["missing"]
    comps = _good()
    comps["D1"]["pins"] = {"1": ("K", "LED_A"), "2": ("A", "GND")}  # reversed to GND
    assert "led_with_resistor" in _check(comps)["missing"]


def test_led_to_vcc_needs_the_cathode_towards_the_pin() -> None:
    comps = _good()
    comps["D1"]["pins"] = {"1": ("K", "LED_A"), "2": ("A", "VBUS")}  # sinks into the pin
    assert _check(comps)["golden"]
    comps["D1"]["pins"] = {"1": ("K", "VBUS"), "2": ("A", "LED_A")}
    assert "led_with_resistor" in _check(comps)["missing"]


def test_vcc_through_a_resistor_is_not_usb_powered() -> None:
    comps = _good()
    for ref in ("U1", "C1", "J2"):
        pins = comps[ref]["pins"]
        for n, (name, net) in pins.items():
            if net == "VBUS":
                pins[n] = (name, "+5V")
    comps["R9"] = {"lib": "Device:R", "value": "10", "footprint": R0805,
                   "pins": {"1": ("~", "VBUS"), "2": ("~", "+5V")}}  # fmt: skip
    assert "mcu_powered_from_usb" in _check(comps)["missing"]
