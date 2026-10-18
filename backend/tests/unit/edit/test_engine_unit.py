"""Engine and sch_add_wire helpers without KiCad (fakes)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kipy.errors import ApiError

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.engine import by_id
from copperchat.kicad_tools.edit.specs_core import MAX_POINTS, _parse_points


class FakeSch:
    def __init__(self, present: set[str], error: Exception | None = None) -> None:
        self.present = present
        self.error = error
        self.calls = 0

    def get_items_by_id(self, kiids: list[Any]) -> list[Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        ids = [k.value for k in kiids]
        if any(i not in self.present for i in ids):
            raise ApiError("item not found")
        return [SimpleNamespace(id=SimpleNamespace(value=i)) for i in ids]


@pytest.mark.parametrize("missing", [set(), {"a"}, {"h"}, {"c", "f"}, set("abcdefgh")])
def test_by_id_bisects_missing_ids(missing: set[str]) -> None:
    ids = list("abcdefgh")
    sch = FakeSch(set(ids) - missing)
    assert set(by_id(sch, ids)) == set(ids) - missing
    assert sch.calls <= 1 + 2 * max(1, len(missing)) * 3  # O(missing * log n), not n


def test_by_id_single_and_empty() -> None:
    assert by_id(FakeSch({"a"}), ["a"]).keys() == {"a"}
    assert by_id(FakeSch(set()), ["a"]) == {}
    assert by_id(FakeSch(set()), []) == {}


def test_by_id_reraises_connection_errors() -> None:
    sch = FakeSch({"a"}, error=ConnectionError("gone"))
    with pytest.raises(ConnectionError):
        by_id(sch, ["a", "b"])
    assert sch.calls == 1


def test_parse_points_limits() -> None:
    assert len(_parse_points([[0, 0]] * MAX_POINTS)) == MAX_POINTS
    with pytest.raises(StructuredToolError) as err:
        _parse_points([[0, 0]] * (MAX_POINTS + 1))
    assert err.value.code == "invalid_argument" and len(err.value.why) < 200
    with pytest.raises(StructuredToolError) as err:
        _parse_points([[0, 0], [10**400, 0]])
    assert err.value.code == "invalid_argument"
    for bad in ([[0, 0], [True, 1]], [[0, 0], [float("nan"), 0]], [[0, 0], [1]], "x"):
        with pytest.raises(StructuredToolError):
            _parse_points(bad)
