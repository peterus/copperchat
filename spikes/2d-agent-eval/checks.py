"""Value / footprint checks from recorded components — no KiCad imports, so summarize.py can
run them on the host over existing result.json files."""

from __future__ import annotations

import re

PASSIVE_SYMBOLS = re.compile(r"^Device:(R|C|L|C_Polarized|R_Small|C_Small|L_Small|FerriteBead\w*)$")

# Values the prompts ask for (checked from the recorded components; not part of `golden`,
# which is connectivity only). Normalised: '1k' / '1 kΩ' / '1K' all match.
WANTED_VALUES = {
    "s1": {r"^Device:R": ["1k"]},
    "s2": {r"^Device:C": ["10u", "22u"]},
    "s3": {r"^Device:C": ["100n", "100n"], r"^Device:R": ["4.7k", "4.7k"]},
}


def norm_value(v: str) -> str:
    v = v.lower().replace("ω", "").replace("ohm", "").replace(" ", "").replace("µ", "u").replace("f", "")
    return v.replace("4k7", "4.7k")


def value_checks(name: str, components: dict) -> dict:
    """Prompt values + 0805 passives, from the recorded `components` of a score."""
    out = {}
    for pat, want in WANTED_VALUES[name].items():
        got = sorted(norm_value(c["value"]) for c in components.values() if re.search(pat, c["lib"]))
        out[f"values {pat.strip('^')}"] = sorted(want) == got
    out["passives_0805"] = all("0805" in c["footprint"] for c in components.values() if PASSIVE_SYMBOLS.match(c["lib"]))
    return out
