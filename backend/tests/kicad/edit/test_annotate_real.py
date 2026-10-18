"""Annotation allocator against a real api-server (E2 Task 6, E-C8)."""

from __future__ import annotations

import re

import pytest

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.edit.annotate import Allocator
from copperchat.kicad_tools.edit.scene import sheet_refs

pytestmark = pytest.mark.kicad


async def test_per_sheet_blocks_on_hierarchy(hier_session: KicadSession) -> None:
    def run(h: KicadHandle) -> tuple[str, str, str, str, set[str]]:
        alloc = Allocator.from_design(h, 100)
        amp = [r for r in sheet_refs(h) if r.file.endswith("ampli_ht.kicad_sch")]
        assert len(amp) == 2
        page = amp[1].page
        used = {str(s.reference) for s in h.schematic().get_symbols()}
        return alloc.scheme, page, alloc.next_ref("R", page), alloc.next_ref("R", page), used

    scheme, page, first, second, used = await hier_session.call(run)
    assert scheme == "per_sheet"
    taken = {int(r[1:]) for r in used if re.fullmatch(r"R\d+", r)}
    expected = min(n for n in range(int(page) * 100 + 1, int(page) * 100 + 100) if n not in taken)
    assert first == f"R{expected}"
    assert second != first and second not in used
    assert re.fullmatch(rf"R{page}\d\d", second)


async def test_single_sheet_numbers_from_1(blank_session: KicadSession) -> None:
    def run(h: KicadHandle) -> tuple[str, str, str]:
        alloc = Allocator.from_design(h, 100)
        return alloc.scheme, alloc.next_ref("R", "1"), alloc.next_ref("R", "1")

    assert await blank_session.call(run) == ("flat", "R1", "R2")
