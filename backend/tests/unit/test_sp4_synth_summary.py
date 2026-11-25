"""The `sp4_synth` fixture's tool steps and the netlist / ERC summary that compares two
generator runs and the committed golden (P1b, pure: no KiCad)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from tests.kicad.board.sp4_synth_steps import EXPECTED_PARTS, POWER_STEPS, ROOT_STEPS
from tests.kicad.board.test_sp4_synth_build import erc_summary, netlist_summary

TABLE_LIB_IDS = {
    "MCU_ST_STM32F0:STM32F042F6Px",
    "Device:C",
    "Device:R",
    "Device:Crystal_GND24",
    "Connector:USB_C_Receptacle_USB2.0_16P",
    "Power_Protection:USBLC6-2SC6",
    "Device:LED",
    "Connector_Generic:Conn_01x04",
    "Mechanical:MountingHole",
    "Regulator_Switching:TPS563200",
    "Device:L",
}


def _placements() -> list[tuple[str, dict[str, object], str]]:
    return [(s[0], s[1], s[2]) for s in ROOT_STEPS + POWER_STEPS if s[0] == "sch_place_symbol"]


def test_steps_consistent() -> None:
    defined: set[str] = set()
    for step in ROOT_STEPS + POWER_STEPS:
        used = set(re.findall(r"\{(\w+)\}", json.dumps(step[1])))
        assert used <= defined, (step, used - defined)
        if len(step) > 2:
            assert step[2] not in defined, step
            defined.add(step[2])
    places = _placements()
    assert all(args.get("footprint") for _, args, _ in places)
    assert {args["lib_id"] for _, args, _ in places} == TABLE_LIB_IDS
    assert 24 <= len(places) <= 28
    assert {alias: (args["lib_id"], args["footprint"]) for _, args, alias in places} == (
        EXPECTED_PARTS
    )
    root = [s[1] for s in ROOT_STEPS if s[0] == "sch_place_symbol"]
    power = [s[1] for s in POWER_STEPS if s[0] == "sch_place_symbol"]
    assert all("sheet" not in args for args in root)
    assert power and all(args.get("sheet") == "/Power/" for args in power)


def test_power_sheet_created_before_its_parts() -> None:
    assert POWER_STEPS[0][0] == "sch_add_sheet"
    assert POWER_STEPS[0][1]["file"] == "power.kicad_sch"


# -- netlist / ERC summary -------------------------------------------------------------

NETLIST = """<?xml version="1.0" encoding="utf-8"?>
<export version="E">
  <design>
    <source>/tmp/x/sp4_synth.kicad_sch</source>
    <date>2026-11-25T10:00:00+0000</date>
    <tool>Eeschema 11.0</tool>
    <sheet number="1" name="/" tstamps="/">
      <title_block><title/></title_block>
    </sheet>
    <sheet number="2" name="/Power/" tstamps="/{sheet_uuid}/">
      <title_block><title/></title_block>
    </sheet>
  </design>
  <components>
    <comp ref="R1">
      <value>10k</value>
      <footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <libsource lib="Device" part="R" description="Resistor"/>
      <property name="Sheetname" value="Root"/>
      <sheetpath names="/" tstamps="/"/>
      <tstamps>{r1_uuid}</tstamps>
    </comp>
    <comp ref="C10">
      <value>100nF</value>
      <footprint>Capacitor_SMD:C_0603_1608Metric</footprint>
      <libsource lib="Device" part="C" description="Capacitor"/>
      <sheetpath names="/Power/" tstamps="/{sheet_uuid}/"/>
      <tstamps>{c10_uuid}</tstamps>
    </comp>
  </components>
  <nets>
    <net code="2" name="GND" class="Default">
      <node ref="R1" pin="2" pintype="passive"/>
      <node ref="C10" pin="2" pintype="passive"/>
    </net>
    <net code="1" name="/BOOT0" class="Default">
      <node ref="R1" pin="1" pintype="passive"/>
    </net>
    <net code="3" name="Net-(C10-Pad1)" class="Default">
      <node ref="C10" pin="1" pintype="passive"/>
    </net>
  </nets>
</export>
"""


def _xml(**uuids: str) -> ET.Element:
    return ET.fromstring(NETLIST.format(**uuids))


def test_summary_ignores_uuids() -> None:
    a = _xml(sheet_uuid="aaaa-1", r1_uuid="1111", c10_uuid="2222")
    b = _xml(sheet_uuid="bbbb-2", r1_uuid="3333", c10_uuid="4444")
    assert netlist_summary(a) == netlist_summary(b)
    assert "aaaa" not in json.dumps(netlist_summary(a))


def test_summary_sorted() -> None:
    s = netlist_summary(_xml(sheet_uuid="s", r1_uuid="r", c10_uuid="c"))
    assert s["parts"] == {
        "C10": {
            "value": "100nF",
            "footprint": "Capacitor_SMD:C_0603_1608Metric",
            "lib_id": "Device:C",
            "sheet": "/Power/",
        },
        "R1": {
            "value": "10k",
            "footprint": "Resistor_SMD:R_0603_1608Metric",
            "lib_id": "Device:R",
            "sheet": "/",
        },
    }
    assert list(s["parts"]) == ["C10", "R1"]
    assert s["nets"] == {
        "/BOOT0": ["R1.1"],  # single-node nets are kept: they reveal a dangling pin
        "GND": ["C10.2", "R1.2"],
        "Net-(C10-Pad1)": ["C10.1"],
    }
    assert list(s["nets"]) == sorted(s["nets"])
    assert s["sheets"] == ["/", "/Power/"]


ERC = {
    "$schema": "https://schemas.kicad.org/erc.v1.json",
    "date": "2026-11-25T10:00:00+0000",
    "kicad_version": "11.0.0",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "/abc",
            "violations": [
                {"type": "pin_not_connected", "severity": "error", "description": "x",
                 "items": [{"uuid": "u1"}]},
                {"type": "lib_symbol_issues", "severity": "warning", "description": "y",
                 "items": [{"uuid": "u2"}]},
            ],
        },
        {
            "path": "/Power/",
            "uuid_path": "/abc/def",
            "violations": [
                {"type": "lib_symbol_issues", "severity": "warning", "description": "z",
                 "items": [{"uuid": "u3"}]},
                {"type": "label_dangling", "severity": "warning", "description": "w",
                 "items": [{"uuid": "u4"}]},
                {"type": "pin_not_connected", "severity": "exclusion", "description": "e",
                 "items": [{"uuid": "u5"}]},
            ],
        },
    ],
}  # fmt: skip


def test_erc_summary_counts_by_type() -> None:
    assert erc_summary(ERC) == {
        "errors": 1,
        "warnings": {"label_dangling": 1, "lib_symbol_issues": 2},
    }
    assert list(erc_summary(ERC)["warnings"]) == ["label_dangling", "lib_symbol_issues"]
    assert erc_summary({"sheets": []}) == {"errors": 0, "warnings": {}}
