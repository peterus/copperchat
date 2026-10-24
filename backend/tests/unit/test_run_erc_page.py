"""`run_erc` view of a full ERC result: ids, sheet filter, pagination, counts (spec §8.1)."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.changes.erc import violation_id
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import checks


def v(type_: str, n: int, sheet: str = "/", severity: str = "error") -> dict[str, Any]:
    return {
        "type": type_,
        "severity": severity,
        "description": f"{type_} {n}",
        "position_mm": {"x": float(n), "y": 0.0},
        "items": [
            {"uuid": f"{n:08d}-0000-0000-0000-000000000000", "description": "", "position_mm": None}
        ],
        "sheet": sheet,
    }


def test_run_erc_ids_match_step_done_ids() -> None:
    vs = [v("pin_not_connected", 1), v("wire_dangling", 2, severity="warning")]
    out = checks.erc_page(vs, {})
    assert [x["id"] for x in out["violations"]] == [violation_id(x) for x in vs]
    assert out["total"] == 2 and out["offset"] == 0 and out["limit"] == 50


def test_run_erc_sheet_filter() -> None:
    vs = [v("a", 1, "/"), v("a", 2, "/Power/"), v("b", 3, "/Power/Reg/"), v("a", 4, "/Other/")]
    names = lambda args: [x["description"] for x in checks.erc_page(vs, args)["violations"]]  # noqa: E731
    assert names({"sheet": "Power"}) == ["a 2"]
    assert names({"sheet": "/Power/"}) == ["a 2"]
    assert names({"sheet": "Reg"}) == ["b 3"]
    assert names({"sheet": "/Power/Reg"}) == ["b 3"]
    assert names({"sheet": "/"}) == ["a 1"]
    assert names({"sheet": "Nope"}) == []


def test_run_erc_pagination_total_and_counts() -> None:
    vs = [v("pin_not_connected", i, "/A/") for i in range(120)] + [v("wire_dangling", 200, "/B/")]
    first = checks.erc_page(vs, {})
    assert first["count"] == 50 and first["total"] == 121
    assert first["counts"] == {
        "by_type": {"pin_not_connected": 120, "wire_dangling": 1},
        "by_sheet": {"/A/": 120, "/B/": 1},
    }
    last = checks.erc_page(vs, {"offset": 100, "limit": 50})
    assert last["count"] == 21 and last["total"] == 121 and last["offset"] == 100
    beyond = checks.erc_page(vs, {"offset": 500})
    assert beyond["count"] == 0 and beyond["total"] == 121
    # counts describe everything that matches the filters, not the page
    only = checks.erc_page(vs, {"type": "wire*", "limit": 1})
    assert only["total"] == 1 and only["counts"]["by_type"] == {"wire_dangling": 1}


@pytest.mark.parametrize(
    "args",
    [{"limit": 201}, {"limit": 0}, {"offset": -1}, {"limit": True}, {"sheet": ""}, {"sheet": 3}],
)
def test_run_erc_rejects_bad_paging(args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        checks.erc_page([], args)
    assert err.value.code == "invalid_argument"


def test_run_erc_limit_max_200() -> None:
    vs = [v("a", i) for i in range(250)]
    assert checks.erc_page(vs, {"limit": 200})["count"] == 200
