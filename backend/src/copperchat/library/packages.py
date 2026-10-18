"""Footprint records from the KiCad API and chip-package derivation (spike 2B, Q6).

Port of `spikes/2b-libraries/libdata.py` (`footprint_from_proto`, `package_of`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from copperchat.library.index import clip

# imperial code -> metric code (IPC / KiCad naming: "<imperial>_<metric>Metric")
IMPERIAL_TO_METRIC = {
    "01005": "0402",
    "0201": "0603",
    "0402": "1005",
    "0603": "1608",
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
    "1812": "4532",
    "2010": "5025",
    "2512": "6332",
}
_PKG_RE = re.compile(r"(?:^|[_\-:])(\d{4,5})_(\d{4})Metric(?:$|[_\-])")


def package_of(footprint_name: str) -> str | None:
    """Imperial chip-size code from a KiCad footprint *name* (with or without the
    library nickname), or None.

    Only accepted when the imperial code and the metric suffix agree (0805 + 2012), so
    the metric-only trap `C_01005_0402Metric` is not read as 0402 and odd names such as
    `D_2114_3652Metric` stay unknown.
    """
    m = _PKG_RE.search(footprint_name)
    if not m:
        return None
    imperial, metric = m.groups()
    return imperial if IMPERIAL_TO_METRIC.get(imperial) == metric else None


@dataclass(frozen=True)
class FootprintDetail:
    lib_id: str
    description: str
    pad_count: int  # distinct numbered pads
    smd_pads: int  # numbered SMD pads
    package: str | None
    keywords: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lib_id": self.lib_id,
            "description": self.description,
            "pad_count": self.pad_count,
            "smd_pads": self.smd_pads,
            "package": self.package,
        }


def footprint_from_proto(fp: Any) -> FootprintDetail:
    """`FootprintDetail` from a `kiapi.board.types.Footprint` proto (GetItemsFromLibrary)."""
    from kipy.proto.board import board_types_pb2

    numbers: set[str] = set()
    smd = 0
    for item in fp.items:
        if not item.type_url.endswith("board.types.Pad"):
            continue
        pad = board_types_pb2.Pad()
        item.Unpack(pad)
        if not pad.number:
            continue  # mechanical / unnumbered pads do not count
        numbers.add(pad.number)
        if pad.type == board_types_pb2.PadType.PT_SMD:
            smd += 1
    name = fp.id.entry_name
    return FootprintDetail(
        lib_id=f"{fp.id.library_nickname}:{name}",
        description=clip(fp.attributes.description),
        pad_count=len(numbers),
        smd_pads=smd,
        package=package_of(name),
        keywords=clip(fp.attributes.keywords),
    )
