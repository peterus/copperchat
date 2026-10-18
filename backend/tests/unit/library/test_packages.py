from __future__ import annotations

import pytest
from kipy.proto.board import board_types_pb2

from copperchat.library.packages import footprint_from_proto, package_of


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("R_0805_2012Metric", "0805"),
        ("Resistor_SMD:R_0603_1608Metric", "0603"),
        ("C_01005_0402Metric", "01005"),
        ("C_0402_1005Metric", "0402"),
        ("R_0805_2012Metric_Pad1.20x1.40mm_HandSolder", "0805"),
        ("L_1206_3216Metric", "1206"),
        ("D_2114_3652Metric", None),  # not an IPC chip pair
        ("C_0603_2012Metric", None),  # inconsistent pair
        ("C_0402_0603Metric", None),  # inconsistent pair (metric 0603 is imperial 0201)
        ("R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", None),
        ("SOT-23", None),
        ("", None),
    ],
)
def test_package_of(name: str, expected: str | None) -> None:
    assert package_of(name) == expected


def _pad(number: str, pad_type: int) -> board_types_pb2.Pad:
    pad = board_types_pb2.Pad()
    pad.number = number
    pad.type = pad_type  # type: ignore[assignment]
    return pad


def test_footprint_from_proto_counts_numbered_pads() -> None:
    fp = board_types_pb2.Footprint()
    fp.id.library_nickname = "Resistor_SMD"
    fp.id.entry_name = "R_0805_2012Metric"
    fp.attributes.description = "Resistor SMD 0805"
    for pad in (
        _pad("1", board_types_pb2.PadType.PT_SMD),
        _pad("2", board_types_pb2.PadType.PT_SMD),
        _pad("", board_types_pb2.PadType.PT_NPTH),
    ):
        fp.items.add().Pack(pad)
    d = footprint_from_proto(fp)
    assert (d.lib_id, d.pad_count, d.smd_pads, d.package) == (
        "Resistor_SMD:R_0805_2012Metric",
        2,
        2,
        "0805",
    )
    assert set(d.to_dict()) == {"lib_id", "description", "pad_count", "smd_pads", "package"}
