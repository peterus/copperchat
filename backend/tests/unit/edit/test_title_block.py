"""sch_set_title_block: argument validation, partial merge, date shorthand (no KiCad)."""

from __future__ import annotations

from datetime import date

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
