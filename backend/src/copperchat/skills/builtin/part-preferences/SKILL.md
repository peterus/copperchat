---
name: part-preferences
description: The user's part preferences (packages, libraries, sourcing). Follow them when choosing parts; justify any deviation.
---

# Part preferences

These are the user's standing preferences for choosing parts. Follow them when you
pick symbols and footprints. A deviation is allowed when the circuit needs it (power
rating, voltage, availability), but say so to the user and give the reason. The
placement tools compare every footprint with the block below and add a
`preference_deviation` note to their result when it differs.

The tools read only the `preferences` block (one `key: value` per line, lists
comma-separated); everything else in this document is guidance for you. A more
specific layer (project over user over this built-in template) wins per key; an
empty value leaves the key to the less specific layer.

- `passives_package`: chip size for resistors, capacitors, inductors and ferrite
  beads, e.g. `0805`.
- `preferred_libraries`: symbol/footprint library nicknames to prefer.
- `preferred_manufacturers`: manufacturers or series to prefer.
- `assembly`: assembly constraints, e.g. `JLCPCB basic parts only`.

```preferences
passives_package:
preferred_libraries:
preferred_manufacturers:
assembly:
```
