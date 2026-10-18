# s3 run 1

**Prompt:** Build a small MCU block: an ATtiny85 (stock symbol, SOIC-8) powered from +3V3 with a 100 nF decoupling capacitor at its VCC pin, and a TMP102 I2C temperature sensor (stock symbol) on the MCU's I2C pins (USI: SDA = PB0, SCL = PB2) connected through net labels I2C_SDA and I2C_SCL, with 4.7 kΩ pull-ups to +3V3. The sensor gets its own 100 nF decoupling capacitor and its address pin goes to GND. Mark unused pins no-connect. Passives 0805. The schematic is empty; nothing has to be preserved. When you are done, the ERC must have no errors and you must have looked at the result with render_view.

_init: model=claude-sonnet-5-5, tools=15, skills=['deep-research', 'copperchat:kicad-schematic-editing', 'copperchat:kicad-schematic-style', 'design', 'design-sync', 'dataviz', 'update-config', 'verify', 'debug', 'code-review', 'simplify', 'batch', 'fewer-permission-prompts', 'doctor', 'loop', 'schedule', 'claude-api', 'workflow-authoring', 'run', 'run-skill-generator', 'plugin-authoring']_

→ `Skill` {"skill": "copperchat:kicad-schematic-editing"}

  ← Launching skill: copperchat:kicad-schematic-editing

→ `lib_search_symbols` {"query": "ATtiny85"}

→ `lib_search_symbols` {"query": "TMP102"}

  ← {"query": "ATtiny85", "results": [{"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20M", "description": "20MHz, 8kB Flash, 512B SRAM, 512B EEPROM, debugWIRE, QFN-20", "keywords": "AVR 8bit Microcontroller tinyAVR", "unit_count": 1, "pin_count": null, "default_footprint": "Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm_EP2.6x2.6mm", "footprint_filters": ["QFN*1EP*4x4mm*P0.5mm*"], "power": false}, {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20P", "description": "20MHz, 8kB Flash, 512B SRAM, 512B EEPROM, debugWIRE… (+1619)

  ← {"query": "TMP102", "results": [{"lib_id": "Sensor_Temperature:TMP102xxDRL", "description": "Digital Temperature Sensor, \u00b13\u00b0C, low-Power, SMBus, 12 bit, Two-Wire Serial Interface, SOT-563", "keywords": "digital temperature sensor i2c smbus", "unit_count": 1, "pin_count": 6, "default_footprint": "Package_TO_SOT_SMD:SOT-563", "footprint_filters": ["SOT?563*"], "power": false}], "note": "pin_count null = derived symbol; lib_get_symbol shows its pins"}

→ `lib_get_symbol` {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S"}

  ← {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "description": "20MHz, 8kB Flash, 512B SRAM, 512B EEPROM, debugWIRE, SOIC-8", "reference_prefix": "U", "default_footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm", "footprint_filters": ["*SOIC*5.3x5.3mm*P1.27mm*"], "unit_count": 1, "units": [1], "size_mm": [27.94, 30.48], "pins_at_rotation_0": [{"pin": "1", "name": "~{RESET}/PB5", "type": "bidirectional", "offset": [15.24, 5.08], "points": "right"}, {"pin": "5", "name": "AREF/PB0", "type": "bidirect… (+742)

→ `lib_get_symbol` {"lib_id": "Sensor_Temperature:TMP102xxDRL"}

  ← {"lib_id": "Sensor_Temperature:TMP102xxDRL", "description": "Digital Temperature Sensor, \u00b13\u00b0C, low-Power, SMBus, 12 bit, Two-Wire Serial Interface, SOT-563", "reference_prefix": "U", "default_footprint": "Package_TO_SOT_SMD:SOT-563", "footprint_filters": ["SOT?563*"], "unit_count": 1, "units": [1], "size_mm": [20.32, 20.32], "pins_at_rotation_0": [{"pin": "5", "name": "V+", "type": "power_input", "offset": [0.0, -10.16], "points": "up"}, {"pin": "2", "name": "GND", "type": "power_input… (+525)

_thinking: _

→ `sch_place_symbol` {"lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "at": [80, 80], "reference": "U1", "value": "ATtiny85-20S"}

  ← {"placed": {"ref": "U1", "uuid": "77d19341-7301-47aa-98bc-e5eb947eb0ad", "lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "value": "ATtiny85-20S", "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm", "at": [80.01, 80.01], "rotation": 0, "mirror": "none", "bbox": [67.31, 64.77, 95.25, 95.25], "pins": [{"pin": "4", "name": "GND", "type": "power_input", "at": [80.01, 95.25], "points": "down"}, {"pin": "3", "name": "XTAL2/PB4", "type": "bidirectional", "at": [95.25, 82.55], "points": "right"}, {"pin": … (+610)

→ `sch_place_symbol` {"lib_id": "Sensor_Temperature:TMP102xxDRL", "at": [170, 80], "reference": "U2", "value": "TMP102"}

  ← {"placed": {"ref": "U2", "uuid": "543ee38f-b816-49e8-96fb-2e7b1873f23d", "lib_id": "Sensor_Temperature:TMP102xxDRL", "value": "TMP102", "footprint": "Package_TO_SOT_SMD:SOT-563", "at": [170.18, 80.01], "rotation": 0, "mirror": "none", "bbox": [160.02, 69.85, 180.34, 90.17], "pins": [{"pin": "2", "name": "GND", "type": "power_input", "at": [170.18, 90.17], "points": "down"}, {"pin": "4", "name": "ADD0", "type": "input", "at": [160.02, 82.55], "points": "left"}, {"pin": "6", "name": "SDA", "type":… (+369)

→ `sch_place_symbol` {"lib_id": "Device:C", "at": [62, 72], "reference": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}

  ← ERROR {"error": {"what": "Device:C at [62.23, 72.39] would overlap ['U1']", "why": "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection", "suggestion": "nearest free position: [62.23, 59.69]", "size_mm": [4.06, 7.62]}}

→ `sch_place_symbol` {"lib_id": "Device:C", "at": [190, 65], "reference": "C2", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}

  ← ERROR {"error": {"what": "Device:C at [190.5, 64.77] would overlap ['U2']", "why": "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection", "suggestion": "nearest free position: [193.04, 64.77]", "size_mm": [4.06, 7.62]}}

→ `sch_place_symbol` {"lib_id": "Device:R", "at": [125, 62], "reference": "R1", "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric"}

  ← {"placed": {"ref": "R1", "uuid": "4f58de03-f0e4-4ca7-8129-32695f1fd401", "lib_id": "Device:R", "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric", "at": [124.46, 62.23], "rotation": 0, "mirror": "none", "bbox": [123.444, 58.42, 125.476, 66.04], "pins": [{"pin": "2", "name": "", "type": "passive", "at": [124.46, 66.04], "points": "down"}, {"pin": "1", "name": "", "type": "passive", "at": [124.46, 58.42], "points": "up"}]}, "notes": [], "item_changes": 1}

→ `sch_place_symbol` {"lib_id": "Device:R", "at": [135, 62], "reference": "R2", "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric"}

  ← {"placed": {"ref": "R2", "uuid": "10dbd80c-c4c8-4cdc-bef0-408a1ac54baf", "lib_id": "Device:R", "value": "4.7k", "footprint": "Resistor_SMD:R_0805_2012Metric", "at": [134.62, 62.23], "rotation": 0, "mirror": "none", "bbox": [133.604, 58.42, 135.636, 66.04], "pins": [{"pin": "2", "name": "", "type": "passive", "at": [134.62, 66.04], "points": "down"}, {"pin": "1", "name": "", "type": "passive", "at": [134.62, 58.42], "points": "up"}]}, "notes": [], "item_changes": 1}

**assistant:** Place caps at suggested positions (C1 above-left; C2 right).

→ `sch_place_symbol` {"lib_id": "Device:C", "at": [62.23, 59.69], "reference": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}

  ← {"placed": {"ref": "C1", "uuid": "a419af8c-b75c-442f-b758-329cee66d2de", "lib_id": "Device:C", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric", "at": [62.23, 59.69], "rotation": 0, "mirror": "none", "bbox": [60.198, 55.88, 64.262, 63.5], "pins": [{"pin": "1", "name": "", "type": "passive", "at": [62.23, 55.88], "points": "up"}, {"pin": "2", "name": "", "type": "passive", "at": [62.23, 63.5], "points": "down"}]}, "notes": [], "item_changes": 1}

→ `sch_place_symbol` {"lib_id": "Device:C", "at": [193.04, 64.77], "reference": "C2", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}

  ← {"placed": {"ref": "C2", "uuid": "f03551ee-b6ca-4403-a8cd-ffd9e89cf088", "lib_id": "Device:C", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric", "at": [193.04, 64.77], "rotation": 0, "mirror": "none", "bbox": [191.008, 60.96, 195.072, 68.58], "pins": [{"pin": "2", "name": "", "type": "passive", "at": [193.04, 68.58], "points": "down"}, {"pin": "1", "name": "", "type": "passive", "at": [193.04, 60.96], "points": "up"}]}, "notes": [], "item_changes": 1}

_thinking: _

→ `sch_add_power` {"net": "+3V3", "at": ["U1.8", "C1.1", "U2.5", "C2.1", "R1.1", "R2.1"]}

→ `sch_add_power` {"net": "GND", "at": ["U1.4", "C1.2", "U2.2", "C2.2", "U2.4"]}

→ `sch_add_label` {"net": "I2C_SDA", "at": ["U1.5", "U2.6", "R1.2"]}

  ← {"net": "+3V3", "symbol": "power:+3V3", "placed": [{"pin": "U1.8", "power_symbol": "#PWR01", "uuid": "72e7accf-d094-48be-87fb-f7240055e186", "at": [80.01, 62.23], "rotation": 0, "wire_segments": 1}, {"pin": "C1.1", "power_symbol": "#PWR02", "uuid": "55373545-42af-445f-9e5f-c60c994032e4", "at": [62.23, 53.34], "rotation": 0, "wire_segments": 1}, {"pin": "U2.5", "power_symbol": "#PWR03", "uuid": "4eefa4df-0f6c-4a92-90f3-5f3398343ec7", "at": [170.18, 64.77], "rotation": 0, "wire_segments": 1}, {"pi… (+494)

→ `sch_add_label` {"net": "I2C_SCL", "at": ["U1.7", "U2.1", "R2.2"]}

  ← ERROR {"error": {"what": "no room for power:GND at C1.2", "why": "stub/symbol would hit other items", "suggestion": "use sch_add_label, or move the part to free space"}}

→ `sch_no_connect` {"pins": ["U1.1", "U1.2", "U1.3", "U1.6", "U2.3"]}

  ← {"net": "I2C_SDA", "created": [{"kind": "SchematicLine", "uuid": "3bf43deb-9819-4b6c-8144-a446c9217570"}, {"kind": "LocalLabel", "uuid": "c0764e72-48a2-4a73-8dcf-5eaa94596c75"}, {"kind": "SchematicLine", "uuid": "5a6d060d-b539-4c68-b4fa-e67dcba766aa"}, {"kind": "LocalLabel", "uuid": "fa94fab8-431d-483c-bbc6-e8b9c26239a0"}, {"kind": "SchematicLine", "uuid": "86b27ca1-0e5c-4adb-84e4-256ca10e9529"}, {"kind": "LocalLabel", "uuid": "41f5f219-88e5-4341-81f4-fdabbff0bc2f"}], "members": ["R1.2", "U1.5",… (+72)

  ← {"net": "I2C_SCL", "created": [{"kind": "SchematicLine", "uuid": "0abd6ca0-2bb5-4b67-89e7-8a6d24b82aae"}, {"kind": "LocalLabel", "uuid": "855c8ef5-0b11-45f4-ad99-5f3d734c799d"}, {"kind": "SchematicLine", "uuid": "658316b0-c49a-4c2d-bbdc-15d654c9118b"}, {"kind": "LocalLabel", "uuid": "c2d91e44-c0fc-4922-b49b-bee1ae2e3a85"}, {"kind": "SchematicLine", "uuid": "c9bd73c9-9f02-4930-9edb-0489a0cdf970"}, {"kind": "LocalLabel", "uuid": "0198bfdb-d8c8-4cb4-bda0-fa4c1c75f8b1"}], "members": ["R2.2", "U1.7",… (+72)

  ← {"created": 5, "item_changes": 5}

_thinking: _

→ `sch_add_power` {"net": "GND", "at": ["U1.4", "U2.2", "C2.2", "U2.4"]}

→ `sch_add_label` {"net": "GND", "at": ["C1.2"]}

  ← {"net": "GND", "symbol": "power:GND", "placed": [{"pin": "U1.4", "power_symbol": "#PWR08", "uuid": "f6fa50cb-c54f-433d-b5bd-8c492c1508ce", "at": [85.09, 100.33], "rotation": 0, "wire_segments": 2}, {"pin": "U2.2", "power_symbol": "#PWR09", "uuid": "8684957a-cb90-4a7d-976a-e47b5d8d4c94", "at": [170.18, 92.71], "rotation": 0, "wire_segments": 1}, {"pin": "C2.2", "power_symbol": "#PWR10", "uuid": "72855446-96f1-4425-926c-9ee0c0fce229", "at": [193.04, 71.12], "rotation": 0, "wire_segments": 1}, {"pi… (+196)

  ← ERROR {"error": {"what": "no room for a label at C1.2", "why": "stub/label would hit other items", "suggestion": "move the part or use another pin side"}}

_thinking: _

→ `sch_connect` {"from": "C1.2", "to": "GND", "kind": "local"}

  ← ERROR {"error": {"what": "no room for a label at C1.2", "why": "stub/label would hit other items", "suggestion": "move the part or use another pin side"}}

_thinking: _

→ `sch_connect` {"from": "C1.2", "to": "U1.4", "via": "auto"}

  ← ERROR {"error": {"what": "no room for a label at C1.2", "why": "stub/label would hit other items", "suggestion": "move the part or use another pin side"}}

→ `render_view` {"region": [40, 45, 110, 105]}

  ← [image] [Image: source: /tmp/claude-config/projects/-tmp-s2d-run-uhf5a8rs-blank/ca2852b6-ab15-46a1-9e4e-7c89a483e643/tool-results/mcp-copperchat-blob-1792286343118-kuwii2.png] {"region_mm": [40, 45, 110, 105], "size_px": [552, 472]}

_thinking: _

**assistant:** Checking why C1's GND pin refuses a symbol: the render shows a stray stub there.

→ `sch_get_pin` {"pins": ["C1.2"]}

  ← {"pins": [{"pin": "2", "name": "", "type": "passive", "at": [62.23, 63.5], "points": "down", "net": "unconnected-(C1-Pad2)", "attached": [], "ref": "C1", "net_members": [], "free": true}]}

_thinking: _

→ `sch_connect` {"from": "C1.2", "to": "U1.4", "via": "wire"}

  ← ERROR {"error": {"what": "no clean wire route C1.2 -> U1.4", "why": "every candidate route (<= 4 segments) hits something: ['corner (80.01, 97.79) on existing wire', 'crosses body of #PWR07', 'overlaps wire (80.01, 95.25)-(80.01, 97.79)', 'touches pin #PWR07.1 at (80.01, 97.79)', 'touches wire end at (80.01, 97.79)']", "suggestion": "use via='label' (or 'auto'), or move a part so the pins face each other"}}

_thinking: _

→ `sch_connect` {"from": "C1.2", "to": "U2.2", "via": "label", "net_name": "GND"}

  ← ERROR {"error": {"what": "no room for a label at C1.2", "why": "stub/label would hit other items", "suggestion": "move the part or use another pin side"}}

_thinking: _

→ `sch_list_symbols` {"include_power": true}

  ← {"symbols": [{"ref": "#PWR01", "uuid": "72e7accf-d094-48be-87fb-f7240055e186", "lib_id": "power:+3V3", "value": "+3V3", "footprint": "", "at": [80.01, 62.23], "rotation": 0, "mirror": "none", "bbox": [79.248, 59.69, 80.772, 62.23], "unconnected_pins": []}, {"ref": "#PWR02", "uuid": "55373545-42af-445f-9e5f-c60c994032e4", "lib_id": "power:+3V3", "value": "+3V3", "footprint": "", "at": [62.23, 53.34], "rotation": 0, "mirror": "none", "bbox": [61.468, 50.8, 62.992, 53.34], "unconnected_pins": []}, … (+3923)

_thinking: _

→ `sch_place_symbol` {"lib_id": "Device:C", "at": [50, 59.69], "reference": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0805_2012Metric"}

  ← ERROR {"error": {"what": "reference C1 already used", "why": "references must be unique", "suggestion": "omit reference to auto-assign (C3)"}}

_thinking: _

→ `sch_add_label` {"net": "GND", "at": ["C1.2"], "kind": "global"}

  ← ERROR {"error": {"what": "no room for a label at C1.2", "why": "stub/label would hit other items", "suggestion": "move the part or use another pin side"}}

_thinking: _

→ `sch_connect` {"from": "C1.2", "to": "#PWR07.1", "via": "wire"}

  ← ERROR {"error": {"what": "no clean wire route C1.2 -> #PWR07.1", "why": "every candidate route (<= 4 segments) hits something: ['corner (80.01, 95.25) on existing wire', 'crosses body of U1', 'non-orthogonal segment', 'overlaps wire (80.01, 95.25)-(80.01, 97.79)', 'touches pin U1.4 at (80.01, 95.25)', 'touches wire end at (80.01, 95.25)']", "suggestion": "use via='label' (or 'auto'), or move a part so the pins face each other"}}

_thinking: _

→ `sch_place_symbol` {"lib_id": "power:GND", "at": [62.23, 69.85]}

  ← ERROR {"error": {"what": "power:GND at [62.23, 69.85] would overlap ['U1']", "why": "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection", "suggestion": "nearest free position: [52.07, 59.69]", "size_mm": [2.54, 2.54]}}

_thinking: _

→ `sch_place_symbol` {"lib_id": "power:GND", "at": [57.15, 63.5]}

  ← ERROR {"error": {"what": "power:GND at [57.15, 63.5] would overlap ['U1', 'C1']", "why": "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection", "suggestion": "nearest free position: [52.07, 58.42]", "size_mm": [2.54, 2.54]}}

**assistant:** Place it at the suggested spot and wire it to C1.2.

→ `sch_place_symbol` {"lib_id": "power:GND", "at": [52.07, 66.04]}

  ← ERROR {"error": {"what": "power:GND at [52.07, 66.04] would overlap ['U1']", "why": "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection", "suggestion": "nearest free position: [49.53, 66.04]", "size_mm": [2.54, 2.54]}}

_thinking: _

→ `sch_place_symbol` {"lib_id": "power:GND", "at": [49.53, 66.04]}

  ← {"placed": {"ref": "#PWR1", "uuid": "4ada2505-e19b-4448-ac0a-c8e32415b096", "lib_id": "power:GND", "value": "GND", "footprint": "", "at": [49.53, 66.04], "rotation": 0, "mirror": "none", "bbox": [48.26, 66.04, 50.8, 68.58], "pins": [{"pin": "1", "name": "", "type": "power_input", "at": [49.53, 66.04], "points": "up"}]}, "notes": [], "item_changes": 1}

→ `sch_connect` {"from": "C1.2", "to": "#PWR1.1", "via": "wire"}

  ← {"mode": "wire", "points": [[62.23, 63.5], [62.23, 64.77], [49.53, 64.77], [49.53, 66.04]], "created": [{"kind": "SchematicLine", "uuid": "9bc0f039-dcf9-415d-89d1-a8fcebb0d842"}, {"kind": "SchematicLine", "uuid": "17321bb9-b277-4bdd-873d-e5c870b694cc"}, {"kind": "SchematicLine", "uuid": "7abba0c7-1c91-4d7f-be6d-32139e16a945"}], "net": "GND", "connectivity_verified": true, "item_changes": 3}

_thinking: _

→ `sch_add_power` {"net": "PWR_FLAG", "at": ["R1.1"]}

→ `sch_add_power` {"net": "PWR_FLAG", "at": ["C2.2"]}

  ← {"net": "PWR_FLAG", "symbol": "power:PWR_FLAG", "placed": [{"pin": "R1.1", "power_symbol": "#FLG01", "uuid": "808464a3-866d-4736-84d1-e8024e1c34ad", "at": [116.84, 53.34], "rotation": 0, "wire_segments": 2}], "connectivity_verified": true, "item_changes": 4}

  ← {"net": "PWR_FLAG", "symbol": "power:PWR_FLAG", "placed": [{"pin": "C2.2", "power_symbol": "#FLG02", "uuid": "7a201a3f-31cf-4854-8c98-158ba6b63d54", "at": [200.66, 73.66], "rotation": 180, "wire_segments": 2}], "connectivity_verified": true, "item_changes": 4}

→ `run_erc` {}

→ `render_view` {}

  ← {"errors": 0, "warnings": 0, "violations": [], "coordinate_units": "mm"}

  ← [image] [Image: source: /tmp/claude-config/projects/-tmp-s2d-run-uhf5a8rs-blank/ca2852b6-ab15-46a1-9e4e-7c89a483e643/tool-results/mcp-copperchat-blob-1792286379006-3pog3y.png] {"region_mm": [33.3, 38.8, 216.7, 114.9], "size_px": [1400, 580]}

**assistant:** The MCU block is built, ERC reports 0 errors and 0 warnings, and I checked the layout in `render_view`.

**Parts**
- **U1:** ATtiny85-20S, `Package_SO:SOIC-8_5.3x5.3mm_P1.27mm`.
- **U2:** TMP102xxDRL, `Package_TO_SOT_SMD:SOT-563`.
- **C1:** 100 nF, 0805, on U1's VCC.
- **C2:** 100 nF, 0805, on U2's V+.
- **R1, R2:** 4.7 kΩ, 0805, pull-ups to +3V3 on SDA and SCL.

**Nets**
- **I2C_SDA:** U1 pin 5 (PB0), U2 pin 6 (SDA) and R1.
- **I2C_SCL:** U1 pin 7 (PB2), U2 pin 1 (SCL) and R2.
- **+3V3 and GND:** U1 VCC and U2 V+ on +3V3, both caps and both pull-ups too. U2's ADD0 pin goes to GND. I added a PWR_FLAG on each rail because nothing on the sheet drives them.
- **No-connects:** U1 pins 1, 2, 3 and 6, and U2's ALERT pin.

**Layout and leftovers**
- **Duplicate GND symbol at U1:** two GND symbols sit on U1's GND pin, a leftover from a partly failed call that I couldn't delete. Both are on the same net, so it's harmless, but you may want to remove one.
- **C1's GND symbol:** the tool wouldn't put a GND symbol directly under C1, so I placed one at the left and wired C1's lower pin to it.
- **C1 position:** C1 sits above and to the left of U1 and isn't right next to the VCC pin. It's wired to the same +3V3 and GND nets.


---
**result:** {"success": true, "cost_usd": 0.2125361000000001, "tool_calls": 38, "tool_errors": 14, "stop": "success"}


**final answer:**

The MCU block is built, ERC reports 0 errors and 0 warnings, and I checked the layout in `render_view`.

**Parts**
- **U1:** ATtiny85-20S, `Package_SO:SOIC-8_5.3x5.3mm_P1.27mm`.
- **U2:** TMP102xxDRL, `Package_TO_SOT_SMD:SOT-563`.
- **C1:** 100 nF, 0805, on U1's VCC.
- **C2:** 100 nF, 0805, on U2's V+.
- **R1, R2:** 4.7 kΩ, 0805, pull-ups to +3V3 on SDA and SCL.

**Nets**
- **I2C_SDA:** U1 pin 5 (PB0), U2 pin 6 (SDA) and R1.
- **I2C_SCL:** U1 pin 7 (PB2), U2 pin 1 (SCL) and R2.
- **+3V3 and GND:** U1 VCC and U2 V+ on +3V3, both caps and both pull-ups too. U2's ADD0 pin goes to GND. I added a PWR_FLAG on each rail because nothing on the sheet drives them.
- **No-connects:** U1 pins 1, 2, 3 and 6, and U2's ALERT pin.

**Layout and leftovers**
- **Duplicate GND symbol at U1:** two GND symbols sit on U1's GND pin, a leftover from a partly failed call that I couldn't delete. Both are on the same net, so it's harmless, but you may want to remove one.
- **C1's GND symbol:** the tool wouldn't put a GND symbol directly under C1, so I placed one at the left and wired C1's lower pin to it.
- **C1 position:** C1 sits above and to the left of U1 and isn't right next to the VCC pin. It's wired to the same +3V3 and GND nets.
