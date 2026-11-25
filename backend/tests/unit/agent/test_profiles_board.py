"""Board tool profiles behind COPPERCHAT_BOARD_TOOLS (SP4 P1c Task 2, index B-C1)."""

from __future__ import annotations

from typing import Any

from copperchat.agent.profiles import PCB_READ, PCB_WRITES, SCH_WRITES, profile_specs
from copperchat.changes.repo import BOARD_KINDS
from copperchat.tools import ToolResult, ToolSpec, registry

KINDS = ("requirements", "schematic", "board", "free")
# The 17 write tools of spec §7.2.
SPEC_72 = {
    "pcb_sync_from_schematic",
    "pcb_set_outline",
    "pcb_set_rules",
    "pcb_define_blocks",
    "pcb_place_block",
    "pcb_move",
    "pcb_legalize",
    "pcb_route",
    "pcb_route_fill",
    "pcb_unroute",
    "pcb_zones",
    "pcb_fanout",
    "pcb_stitch",
    "pcb_tidy_silk",
    "pcb_approve",
    "pcb_fab_export",
    "pcb_export_3d",
}


def _spec(name: str) -> ToolSpec:
    async def handler(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(text="ok")

    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, handler=handler)


SPECS = [
    _spec(n)
    for n in sorted(
        PCB_READ
        | PCB_WRITES
        | {"pcb_list_footprints", "sch_connect", "plan_propose", "fp_create", "doc_patch_section"}
    )
]


def _names(kind: str, *, board_tools: bool) -> set[str]:
    return {s.name for s in profile_specs(kind, SPECS, board_tools=board_tools)}


def test_flag_off_removes_writes_everywhere() -> None:
    for kind in KINDS:
        assert not _names(kind, board_tools=False) & PCB_WRITES, kind
    # the default is off
    assert not {s.name for s in profile_specs("free", SPECS)} & PCB_WRITES


def test_flag_off_keeps_reads() -> None:
    for kind in KINDS:
        names = _names(kind, board_tools=False)
        assert {"pcb_status", "pcb_render", "pcb_list_footprints"} <= names, kind
        assert names >= PCB_READ, kind


def test_flag_on_board_profile() -> None:
    names = _names("board", board_tools=True)
    assert names >= PCB_WRITES
    assert {"plan_propose", "fp_create", "doc_patch_section"} <= names
    assert "sch_connect" not in names
    assert names >= PCB_READ
    assert _names("free", board_tools=True) >= PCB_WRITES


def test_flag_on_schematic_has_no_pcb_writes() -> None:
    for kind in ("requirements", "schematic"):
        assert not _names(kind, board_tools=True) & PCB_WRITES, kind
    assert "sch_connect" in _names("schematic", board_tools=True)


def test_pcb_writes_is_the_spec_list() -> None:
    assert len(PCB_WRITES) == 17
    assert set(PCB_WRITES) == SPEC_72
    assert all(n.startswith("pcb_") for n in PCB_WRITES)
    assert not PCB_WRITES & PCB_READ
    assert not PCB_WRITES & SCH_WRITES


def test_every_board_write_has_a_kind() -> None:
    for name in PCB_WRITES - {"pcb_fab_export", "pcb_export_3d"}:
        assert "board_" + name.removeprefix("pcb_") in BOARD_KINDS, name
    # every registered two-phase board write records a board kind (vacuous until P2)
    for spec in registry.all_specs():
        if spec.prepare is not None and spec.name in PCB_WRITES:
            assert "board_" + spec.name.removeprefix("pcb_") in BOARD_KINDS, spec.name


def test_every_registered_pcb_write_is_flagged() -> None:
    """A future mutating `pcb_*` tool missing from PCB_WRITES would slip past the flag."""
    pcb = [s for s in registry.all_specs() if s.name.startswith("pcb_")]
    assert {"pcb_list_footprints", "pcb_get_footprint", "pcb_get_net"} <= {s.name for s in pcb}
    for spec in pcb:
        if spec.mutating or spec.changes_documents or spec.prepare is not None:
            assert spec.name in PCB_WRITES, spec.name
