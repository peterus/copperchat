---
mpn: CC1234
manufacturer: CopperChat Test Semiconductors (fictional)
datasheet: datasheet.pdf (CCS-1234, rev B, 2026-03)
user_edited: false
---

# CC1234 — 4.5–18 V input, 1 A synchronous buck converter

## Summary

CC1234 is a **fictional** part used only as a test fixture: a 4.5–18 V input,
1 A synchronous step-down converter with integrated high- and low-side
MOSFETs, 0.6 V feedback reference and fixed 1.4 MHz switching frequency.
These notes cover the CC1234DBV (SOT-23-6). The examples use VIN = 12 V,
VOUT = 3.3 V, IOUT = 1 A.

## Packages and pinout

### SOT-23-6 (6 pins, 2.9 × 1.6 mm)

| Pin | Name | Type | Function | If unused |
|---|---|---|---|---|
| 1 | BST | power | Bootstrap supply of the high-side driver; 100 nF from BST to SW [D1 p.3] | must be connected |
| 2 | GND | ground | Power and signal ground [D1 p.3] | n/a — always connected |
| 3 | FB | analog | Feedback input, regulates to 0.6 V; connect the divider tap [D1 p.3] | must be connected |
| 4 | EN | input | Enable, high > 1.2 V, low < 0.4 V, internal 1 MΩ pull-down [D1 p.3] | tie to VIN for always-on (max 18 V) |
| 5 | VIN | power | Input supply of the power stage and controller [D1 p.3] | n/a — always connected |
| 6 | SW | output | Switch node to the inductor [D1 p.3] | must be connected |

## Absolute maximum ratings

| Parameter | Min | Max | Unit | Source |
|---|---|---|---|---|
| VIN, EN to GND | -0.3 | 20 | V | [D1 p.4] |
| SW to GND | -1 | 20 | V | [D1 p.4] |
| BST to SW | -0.3 | 6 | V | [D1 p.4] |
| Junction temperature | -40 | 150 | °C | [D1 p.4] |

## Recommended operating conditions

| Parameter | Min | Typ | Max | Unit | Source |
|---|---|---|---|---|---|
| Input voltage VIN | 4.5 | | 18 | V | [D1 p.4] |
| Output current | 0 | | 1 | A | [D1 p.4] |
| Output voltage | 0.6 | | 0.9 × VIN | V | [D1 p.4] |
| Ambient temperature | -40 | 25 | 85 | °C | [D1 p.4] |

## Typical application circuit

Buck converter for 12 V → 3.3 V / 1 A [D1 p.10]: VIN (pin 5) to the input rail
with C1 next to pin 5 and pin 2; EN (pin 4) to VIN through R3; SW (pin 6) to
L1, the other end of L1 is VOUT; C2 from BST (pin 1) to SW (pin 6); C3 from
VOUT to GND; divider R1 (VOUT to FB) and R2 (FB to GND) with the tap on FB
(pin 3). R3 is optional when EN is driven by logic.

| Ref | Part | Value | Rating | Purpose | Source |
|---|---|---|---|---|---|
| C1 | capacitor, X7R | 10 µF | 25 V, 1206 | input decoupling at VIN | [D1 p.10] |
| C2 | capacitor, X7R | 100 nF | 16 V, 0402 | bootstrap BST–SW | [D1 p.10] |
| L1 | inductor, shielded | 4.7 µH | Isat ≥ 1.6 A, DCR ≤ 60 mΩ | output inductor | [D1 p.11] |
| C3 | capacitor, X7R | 22 µF | 10 V, 0805 | output capacitor | [D1 p.11] |
| R1 | resistor, 1 % | 100 kΩ | 0402 | feedback divider, top | [D1 p.11] |
| R2 | resistor, 1 % | 22.1 kΩ | 0402 | feedback divider, bottom | [D1 p.11] |
| R3 | resistor | 100 kΩ | 0402 | EN pull-up to VIN (optional) | [D1 p.10] |

## Design equations

- Output voltage: VOUT = 0.6 V × (1 + R1/R2), R1 = top resistor, R2 = bottom resistor [D1 p.11]
- Example: R1 = 100 kΩ, R2 = 22.1 kΩ → VOUT = 0.6 V × (1 + 4.525) = 3.31 V
- Inductor ripple: ΔIL = VOUT × (VIN − VOUT) / (VIN × L × fSW), fSW = 1.4 MHz [D1 p.11]
- Example: 3.3 V × 8.7 V / (12 V × 4.7 µH × 1.4 MHz) = 0.36 A (36 % of 1 A) → 4.7 µH

## Required external components

- Input: 10 µF X7R 25 V within 2 mm of VIN/GND; add 47 µF bulk if the supply is more than 10 cm away [D1 p.12]
- Bootstrap: 100 nF X7R ≥ 10 V from BST to SW, no series resistor [D1 p.10]
- Output: 22 µF X7R 10 V; ceramic only, ESR below 10 mΩ is stable [D1 p.11]
- EN: 100 kΩ pull-up to VIN or a logic signal; never leave floating when the internal pull-down is not wanted [D1 p.3]

## Layout guidelines

- Keep the input loop VIN → C1 → GND as small as possible; C1 on the same layer as the IC [D1 p.12]
- Route SW short and wide; keep FB and the divider away from SW and L1 [D1 p.12]
- Solid ground plane under the IC; connect GND (pin 2) with at least 2 vias of 0.3 mm drill next to the pin [D1 p.12]
- Feedback divider close to FB; sense VOUT at C3, not at the inductor [D1 p.12]

## Package and footprint

| Package | Body (mm) | Pitch (mm) | Thermal pad | KiCad symbol | KiCad footprint |
|---|---|---|---|---|---|
| SOT-23-6 | 2.9 × 1.6 | 0.95 | none | no exact match — closest: Regulator_Switching:TPS62203DBV, differences: pin order | Package_TO_SOT_SMD:SOT-23-6 |

The KiCad SOT-23-6 footprint matches the recommended land pattern [D1 p.18];
pin 1 is marked by the chamfered corner.

## Errata and pitfalls

- EN has a 1 MΩ pull-down: with EN floating the converter stays off [D1 p.3]
- Output capacitance above 100 µF slows start-up beyond the 2 ms soft-start and can trip the current limit [D1 p.13]

## Sources

- [D1] CopperChat Test Semiconductors CC1234 datasheet, CCS-1234 rev B (2026-03), https://example.invalid/cc1234.pdf, accessed 2026-10-17
