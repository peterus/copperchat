"""Annotation allocator core (E-C8), pure data - no KiCad."""

from __future__ import annotations

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.annotate import Allocator

PAGES3 = ["1", "2", "3"]


def test_flat_scheme_detected() -> None:
    symbols = [(f"R{n}", PAGES3[n % 3]) for n in range(1, 41)]
    alloc = Allocator(symbols, PAGES3, 100)
    assert alloc.scheme == "flat"
    assert alloc.next_ref("R", "2") == "R41"
    assert alloc.next_ref("R", "1") == "R42"
    assert alloc.next_ref("C", "1") == "C1"


def test_per_sheet_detected() -> None:
    alloc = Allocator([("R101", "1"), ("R102", "1"), ("R201", "2")], PAGES3, 100)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("R", "2") == "R202"
    assert alloc.next_ref("R", "2") == "R203"
    assert alloc.next_ref("R", "1") == "R103"
    assert alloc.next_ref("R", "3") == "R301"


def test_per_sheet_fills_first_gap() -> None:
    alloc = Allocator([("R101", "1"), ("R103", "1")], PAGES3, 100)
    assert alloc.next_ref("R", "1") == "R102"
    assert alloc.next_ref("R", "1") == "R104"


def test_empty_multi_sheet_is_per_sheet() -> None:
    alloc = Allocator([], PAGES3, 100)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("R", "3") == "R301"


def test_block_full_error() -> None:
    symbols = [(f"R{n}", "1") for n in range(101, 200)]
    alloc = Allocator(symbols, PAGES3, 100)
    assert alloc.scheme == "per_sheet"
    with pytest.raises(StructuredToolError) as err:
        alloc.next_ref("R", "1")
    assert "block full" in err.value.what
    assert err.value.code == "invalid_argument"
    assert "1000" in err.value.suggestion
    assert "renumber" in err.value.suggestion
    assert alloc.next_ref("R", "2") == "R201"


def test_block_1000() -> None:
    alloc = Allocator([], PAGES3, 1000)
    assert alloc.next_ref("R", "1") == "R1001"
    assert alloc.next_ref("R", "2") == "R2001"
    assert alloc.next_ref("R", "2") == "R2002"


def test_block_1000_keeps_existing_100_blocks_per_sheet() -> None:
    alloc = Allocator([(f"R{n}", "1") for n in range(101, 200)], PAGES3, 1000)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("R", "1") == "R1001"


def test_power_refs_unique() -> None:
    alloc = Allocator([("#PWR01", "1"), ("#PWR01", "2"), ("R101", "1")], PAGES3, 100)
    assert alloc.next_power("#PWR") == "#PWR02"
    assert alloc.next_power("#PWR") == "#PWR03"
    assert alloc.next_power("#FLG") == "#FLG01"


def test_power_skips_used_numbers() -> None:
    alloc = Allocator([("#PWR0101", "1"), ("#PWR01", "1"), ("#PWR02", "1")], PAGES3, 100)
    assert alloc.next_power("#PWR") == "#PWR03"


def test_power_refs_do_not_affect_scheme() -> None:
    alloc = Allocator([("#PWR1", "1"), ("#FLG7", "3"), ("R101", "1")], PAGES3, 100)
    assert alloc.scheme == "per_sheet"


def test_multi_unit_shares_ref() -> None:
    alloc = Allocator([("U101", "1")], PAGES3, 100)
    assert alloc.next_ref("U", "1") == "U102"


def test_multi_unit_duplicates_are_one_ref() -> None:
    # Units A and B of U101 both report U101: still per-sheet, still taken.
    alloc = Allocator([("U101", "1"), ("U101", "1")], PAGES3, 100)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("U", "1") == "U102"


def test_single_sheet_numbers_from_1() -> None:
    alloc = Allocator([], ["1"], 100)
    assert alloc.scheme == "flat"
    assert alloc.next_ref("R", "1") == "R1"
    assert alloc.next_ref("R", "1") == "R2"


def test_single_sheet_continues_after_max() -> None:
    alloc = Allocator([("R1", "1"), ("R5", "1"), ("R?", "1")], ["1"], 100)
    assert alloc.next_ref("R", "1") == "R6"


def test_unannotated_refs_ignored() -> None:
    alloc = Allocator([("R?", "1"), ("C?", "2"), ("R101", "1")], PAGES3, 100)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("R", "1") == "R102"


def test_mixed_prefixes_independent() -> None:
    alloc = Allocator([("R101", "1"), ("R102", "1"), ("C101", "1"), ("C201", "2")], PAGES3, 100)
    assert alloc.next_ref("C", "1") == "C102"
    assert alloc.next_ref("R", "1") == "R103"
    assert alloc.next_ref("C", "2") == "C202"
    assert alloc.next_ref("R", "2") == "R201"
    assert alloc.next_ref("RV", "2") == "RV201"


def test_non_numeric_page_falls_back_to_flat() -> None:
    alloc = Allocator([("R101", "1"), ("R201", "ii")], ["1", "ii"], 100)
    assert alloc.scheme == "per_sheet"
    assert alloc.next_ref("R", "ii") == "R202"
    assert alloc.next_ref("R", "1") == "R102"
