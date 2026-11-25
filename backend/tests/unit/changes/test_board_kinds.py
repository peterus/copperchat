"""Board change-log kinds (BOARD_KINDS): listed like every row, never a schematic item."""

from __future__ import annotations

from copperchat.changes.diff import compact_diff
from copperchat.changes.repo import BOARD_KINDS, FOOTPRINT_KINDS, ChangeEntry


def _entry(kind: str, uuid: str, sheet_path: str = "") -> ChangeEntry:
    return ChangeEntry(
        project_id="p1",
        run_id="r1",
        plan_step_id=None,
        call_seq=1,
        tool="pcb_move",
        op="modify",
        kind=kind,
        uuid=uuid,
        sheet_path=sheet_path,
    )


def test_board_kinds_exact() -> None:
    assert {
        "board_sync_from_schematic",
        "board_set_outline",
        "board_set_rules",
        "board_define_blocks",
        "board_place_block",
        "board_move",
        "board_legalize",
        "board_route",
        "board_route_fill",
        "board_unroute",
        "board_zones",
        "board_fanout",
        "board_stitch",
        "board_tidy_silk",
        "board_approve",
    } == BOARD_KINDS


def test_board_kinds_disjoint_from_footprint_kinds() -> None:
    assert BOARD_KINDS.isdisjoint(FOOTPRINT_KINDS)


def test_board_entries_not_in_sch_diff() -> None:
    """A board row never marks a schematic diff item (same uuid) as the tool's own change."""
    raw = {"changes": [{"id": "/u-1", "kind": "modified", "typeName": "Symbol"}]}
    out = compact_diff(raw, [_entry("board_move", "u-1")])
    flags = {e["uuid"]: e["side_effect"] for es in out["sheets"].values() for e in es}
    assert flags == {"u-1": True}
