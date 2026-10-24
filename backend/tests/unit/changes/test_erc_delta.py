"""ERC delta matching and the no-double-run rule (E8a Task 2, E-C10)."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.changes import erc
from copperchat.changes.erc import current_erc, erc_delta, violation_id
from copperchat.editing.state import EditTurnState

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


def v(
    type_: str,
    *uuids: str,
    severity: str = "error",
    at: tuple[float, float] = (10.0, 10.0),
    sheet: str = "/",
) -> dict[str, Any]:
    return {
        "type": type_,
        "severity": severity,
        "description": type_,
        "position_mm": {"x": at[0], "y": at[1]},
        "items": [{"uuid": u, "description": "", "position_mm": None} for u in uuids],
        "sheet": sheet,
    }


def test_violation_id_ignores_item_order_and_position() -> None:
    a = v("pin_not_connected", U1, U2, at=(1, 1))
    b = v("pin_not_connected", U2, U1, at=(50, 50))
    assert violation_id(a) == violation_id(b) and len(violation_id(a)) == 12
    assert violation_id(a) != violation_id(v("wire_dangling", U1, U2))


def test_new_error_detected() -> None:
    base = [v("power_pin_not_driven", U1)]
    cur = [*base, v("pin_not_connected", U2)]
    d = erc_delta(base, cur)
    assert [e["type"] for e in d.new_errors] == ["pin_not_connected"]
    assert d.new_errors[0]["id"] == violation_id(cur[1])
    assert d.new_warnings == [] and d.resolved == []


def test_moved_item_not_new() -> None:
    d = erc_delta(
        [v("pin_not_connected", U1, at=(10, 10))], [v("pin_not_connected", U1, at=(80, 3))]
    )
    assert d.new_errors == [] and d.resolved == []


def test_position_breaks_ties() -> None:
    base = [v("pin_not_connected", U1, at=(10, 10))]
    cur = [v("pin_not_connected", U1, at=(40, 40)), v("pin_not_connected", U1, at=(10, 10))]
    d = erc_delta(base, cur)
    assert len(d.new_errors) == 1
    assert d.new_errors[0]["position_mm"] == {"x": 40, "y": 40}


def test_other_sheet_instance_breaks_ties_first() -> None:
    base = [v("pin_not_connected", U1, at=(10, 10), sheet="/a/")]
    cur = [
        v("pin_not_connected", U1, at=(10, 10), sheet="/b/"),
        v("pin_not_connected", U1, at=(30, 10), sheet="/a/"),
    ]
    d = erc_delta(base, cur)
    assert [e["sheet"] for e in d.new_errors] == ["/b/"]


def test_warning_reported_separately() -> None:
    d = erc_delta([], [v("label_dangling", U1, severity="warning"), v("x", U2, severity="info")])
    assert d.new_errors == []
    assert [w["type"] for w in d.new_warnings] == ["label_dangling"]


def test_resolved_listed() -> None:
    d = erc_delta([v("pin_not_connected", U1), v("y", U2, severity="exclusion")], [])
    assert [r["type"] for r in d.resolved] == ["pin_not_connected"]


async def test_last_erc_reused_when_after_last_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def runner(session: Any, doc: str) -> list[dict[str, Any]]:
        calls.append(doc)
        return [v("fresh", U1)]

    monkeypatch.setattr(erc, "check_violations", runner)
    report = [v("pin_not_connected", U1)]
    state = EditTurnState(call_seq=5, last_mutation_seq=3, last_erc_seq=4, last_erc=report)
    assert await current_erc(object(), state) is report  # type: ignore[arg-type]
    assert calls == []
    # A mutation after the report: ERC runs again and the state remembers it.
    state.last_mutation_seq = 5
    got = await current_erc(object(), state)  # type: ignore[arg-type]
    assert calls == ["sch"] and got[0]["type"] == "fresh"
    assert state.last_erc is got and state.last_erc_seq == 5
    # Without a turn state ERC always runs.
    await current_erc(object(), None)  # type: ignore[arg-type]
    assert calls == ["sch", "sch"]


async def test_run_erc_tool_notes_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.kicad_tools import checks

    report = [v("pin_not_connected", U1)]

    async def runner(session: Any, doc: str) -> list[dict[str, Any]]:
        return report

    class Editing:
        def __init__(self) -> None:
            self.state = EditTurnState(call_seq=7)

        def turn(self, project_id: str, run_id: str | None) -> EditTurnState:
            return self.state

    class Project:
        id = "p"

    class Ctx:
        project = Project()
        session = object()
        run_id = "run-1"
        editing = Editing()

    monkeypatch.setattr(erc, "check_violations", runner)
    ctx = Ctx()
    res = await checks.run_erc(ctx, {})  # type: ignore[arg-type]
    assert res.data is not None and res.data["count"] == 1
    assert ctx.editing.state.last_erc is report and ctx.editing.state.last_erc_seq == 7
    # Another page of the same report is a view of it: no second kicad-cli run.
    ctx.editing.state.last_mutation_seq = 3

    async def boom(session: Any, doc: str) -> list[dict[str, Any]]:
        raise AssertionError("ERC ran again")

    monkeypatch.setattr(erc, "check_violations", boom)
    again = await checks.run_erc(ctx, {"offset": 1})  # type: ignore[arg-type]
    assert again.data is not None and again.data["total"] == 1 and again.data["count"] == 0
