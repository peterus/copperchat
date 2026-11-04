"""sch_set_title_block: argument validation, partial merge, date shorthand (no KiCad)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.title_block import (
    FIELDS,
    changed_fields,
    merge,
    nonempty,
    parse_args,
)

EMPTY = dict.fromkeys(FIELDS, "")


def test_fields_are_the_title_block_nodes_of_kicad() -> None:
    assert (
        "title", "date", "rev", "company", *(f"comment{i}" for i in range(1, 10))
    ) == FIELDS  # fmt: skip


def test_parse_collects_only_given_fields() -> None:
    sheet, all_sheets, upd = parse_args(
        {"title": "Amp", "rev": "B", "comments": {"1": "first", "9": "last"}, "sheet": "/a/"}
    )
    assert (sheet, all_sheets) == ("/a/", False)
    assert upd == {"title": "Amp", "rev": "B", "comment1": "first", "comment9": "last"}


def test_empty_string_is_a_value_that_clears() -> None:
    _, _, upd = parse_args({"company": "", "comments": {"3": ""}})
    assert upd == {"company": "", "comment3": ""}


def test_today_becomes_the_iso_date_in_any_case() -> None:
    iso = date.today().isoformat()
    assert parse_args({"date": "today"})[2] == {"date": iso}
    assert parse_args({"date": " Today "})[2] == {"date": iso}
    assert parse_args({"date": "2026-01-02"})[2] == {"date": "2026-01-02"}


def test_nothing_to_set_is_refused() -> None:
    with pytest.raises(StructuredToolError) as e:
        parse_args({"sheet": "/a/"})
    assert e.value.code == "invalid_argument" and "at least one" in e.value.what


@pytest.mark.parametrize(
    "args",
    [
        {"title": 5},
        {"title": None},
        {"comments": ["a"]},
        {"comments": {"0": "x"}},
        {"comments": {"10": "x"}},
        {"comments": {"one": "x"}},
        {"title": "evil" + chr(0x202E) + "txt"},
        {"company": "zero" + chr(0x200B) + "width"},
        {"comments": {"01": "x"}},
        {"comments": {"+1": "x"}},
        {"comments": {"\u00b2": "x"}},
        {"comments": {"1": 5}},
        {"title": "a\nb"},
        {"company": "tab\there"},
        {"rev": "nul\x00"},
        {"comments": {"2": "x" + chr(0x2028) + "y"}},
        {"title": "x" * 257},
        {"rev": "x" * 65},
        {"comments": {"1": "x" * 257}},
        {"title": "ok", "all_sheets": True, "sheet": "/a/"},
        {"title": "ok", "all_sheets": "yes"},
        {"title": "ok", "sheet": 3},
    ],
)
def test_invalid_arguments_are_refused(args: dict[str, object]) -> None:
    with pytest.raises(StructuredToolError) as e:
        parse_args(args)
    assert e.value.code == "invalid_argument"


def test_quotes_and_backslashes_are_plain_text_here() -> None:
    # S-expression escaping is KiCad's job (through the API); the value stays verbatim
    _, _, upd = parse_args({"title": 'He said "hi" \\ bye (x)'})
    assert upd["title"] == 'He said "hi" \\ bye (x)'


def test_merge_changes_only_the_given_fields() -> None:
    old = {**EMPTY, "title": "Old", "rev": "A", "comment2": "keep"}
    new = merge(old, {"title": "New", "rev": ""})
    assert new["title"] == "New" and new["rev"] == "" and new["comment2"] == "keep"
    assert changed_fields(old, new) == ["title", "rev"]
    assert old["title"] == "Old"  # not mutated


def test_unchanged_values_are_not_changes() -> None:
    old = {**EMPTY, "title": "Same"}
    assert changed_fields(old, merge(old, {"title": "Same", "company": ""})) == []


def test_nonempty_drops_empty_fields() -> None:
    assert nonempty({**EMPTY, "title": "T", "comment4": "c"}) == {"title": "T", "comment4": "c"}


class _Sch:
    """Stub schematic: two instances of one file and one of another."""

    def __init__(self, broken: str | None = None) -> None:
        self.reads: list[str] = []
        self.broken = broken

    def get_hierarchy(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name=n, filename=f, page_number="1", path=SimpleNamespace(path=[]))
            for n, f in (("a", "root.kicad_sch"), ("b", "sub.kicad_sch"), ("c", "sub.kicad_sch"))
        ]

    def for_sheet(self, inst: SimpleNamespace) -> _Sch:
        self.reads.append(inst.filename)
        sheet = _Sch(self.broken)
        sheet.get_title_block = lambda: self._block(inst.filename)  # type: ignore[attr-defined]
        return sheet

    def _block(self, filename: str) -> SimpleNamespace:
        from kipy.errors import ApiError

        if filename == self.broken:
            raise ApiError("boom")
        return SimpleNamespace(title=filename, date="", revision="", company="", comments={})


def _info(sch: _Sch) -> list[dict[str, Any]]:
    from copperchat.kicad_tools import access, project

    sch.get_symbols = lambda: []  # type: ignore[attr-defined]
    handle = SimpleNamespace(schematic=lambda: sch)
    with patch.object(access, "sheet_label", lambda s: s.name):
        out: list[dict[str, Any]] = project._schematic_info(handle)["sheets"]  # type: ignore[arg-type]
    return out


def test_project_info_reads_each_sheet_file_once() -> None:
    sch = _Sch()
    sheets = _info(sch)
    assert sch.reads == ["root.kicad_sch", "sub.kicad_sch"]  # not once per instance
    assert [s["title_block"] for s in sheets] == [
        {"title": "root.kicad_sch"},
        {"title": "sub.kicad_sch"},
        {"title": "sub.kicad_sch"},
    ]


def test_project_info_omits_a_title_block_that_cannot_be_read() -> None:
    sheets = _info(_Sch(broken="sub.kicad_sch"))
    assert "title_block" in sheets[0]
    assert "title_block" not in sheets[1] and "title_block" not in sheets[2]
