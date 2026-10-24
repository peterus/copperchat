"""sch_delete keeps a child hierarchical label that names a bus (buses are not followed)."""

from __future__ import annotations

import pytest

from copperchat.kicad_tools.edit.delete import _BUS_NAME


@pytest.mark.parametrize(
    ("name", "bus"),
    [
        ("D[0..7]", True),
        ("ADDR[10..15]", True),
        ("{SDA SCL}", True),
        ("I2C{SDA SCL}", True),
        ("VIN", False),
        ("D[0]", False),
        ("NET[1..x]", False),
        ("A.B", False),
    ],
)
def test_bus_names(name: str, bus: bool) -> None:
    assert bool(_BUS_NAME.search(name)) is bus
