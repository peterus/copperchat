"""kicad-cli netlists of saved copies: the independent connectivity cross-check of the
real-KiCad tests (KiCad's own output, read only)."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def cli_netlist(kicad_cli: str, root_sch: Path, out_dir: Path) -> ET.Element:
    """`kicad-cli sch export netlist --format kicadxml` of `root_sch` (whole hierarchy)."""
    out = out_dir / f"{root_sch.stem}-cli.xml"
    subprocess.run(
        [kicad_cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out),
         str(root_sch)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    text = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE" not in text and "<!ENTITY" not in text
    return ET.fromstring(text)


def cli_pin_nets(root: ET.Element) -> dict[tuple[str, str], str]:
    """(ref, pin) -> net name."""
    return {
        (node.get("ref", ""), node.get("pin", "")): net.get("name", "")
        for net in root.iterfind("./nets/net")
        for node in net.iterfind("node")
    }


def cli_values(root: ET.Element) -> dict[str, str]:
    """ref -> value."""
    return {c.get("ref", ""): c.findtext("value", "") for c in root.iterfind("./components/comp")}
