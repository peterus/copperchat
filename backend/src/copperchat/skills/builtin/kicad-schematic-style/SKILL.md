---
name: kicad-schematic-style
description: Schematic readability conventions by circuit type. Use when judging, explaining or proposing the layout of a KiCad schematic, or when the user asks whether a schematic is readable or well structured.
---

# Schematic style by circuit type

There is no single global rule. Pick the style per section of the schematic,
based on what the circuit does. When you review, name the section, the style
you expect and the concrete deviation (reference, sheet, position in mm).

## MCU, digital and bus-heavy sections → labels

- Connect MCU pins to their peripherals with **net labels**, not long wires.
  A pin stub plus a label with the same name is the expected pattern.
- Buses (SPI, I2C, UART, parallel data, JTAG/SWD) use consistent label names:
  `SPI1_SCK`, `SPI1_MOSI`, `I2C1_SDA`, `UART2_TX`. The name says the bus
  instance and the signal; the direction is clear from the MCU side.
- Group labels of one bus together at the MCU and at the peripheral.
- Use hierarchical sheets for blocks that repeat or that have a clean
  interface (power supply, each sensor, connectors).
- Wires that cross unrelated pins are a defect: KiCad connects by geometry,
  and a wire passing over a pin end creates a silent short.

## Analog signal paths → wired, left-to-right

- Draw the signal path **left-to-right**: input on the left, output on the
  right, in the order the signal passes the stages (filter → gain →
  buffer → ADC).
- Use **wires** inside the path so the topology is visible at a glance;
  labels inside an analog path hide feedback loops and are a smell.
- Feedback networks sit above or below their amplifier, close to it.
- Reference and bias networks stay near the stage they bias.

## Power

- Supplies flow top-to-bottom or left-to-right from the source (connector,
  battery, USB) through protection and regulation to the rails.
- Use power symbols for rails (`+3V3`, `+5V`, `GND`); never a net label for
  a supply that already has a power symbol.
- Each regulator shows its input and output capacitors next to it.
- Power flags (`PWR_FLAG`) sit at the source of each externally supplied net.

## General

- Decoupling capacitors are drawn next to the pin they decouple, one per
  supply pin, not collected in a corner.
- Reference designators and values must not overlap other text or wires.
- Unused pins carry a no-connect marker; floating inputs are not "unused".
- Title block, sheet names and label names are English and consistent.
