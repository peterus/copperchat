"""Reference allocator for new symbols (E-C8, spec §5.5).

Per-sheet blocks: a symbol on page n gets the lowest free number in R{n}01..R{n}99
(block 100) or R{n}001..R{n}999 (block 1000). Single-sheet projects number flat from 1
(R1, R2, ...). Existing references never change and the existing scheme wins: if any
annotated non-power reference does not sit in its page's block (e.g. R1..R40 spread over
three pages), the scheme is "flat" and the next reference is max+1 per prefix.

Power symbols (`#PWR`, `#FLG`) keep their own unique numbering (`#PWR01`, `#PWR02`, ...).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import sheet_refs

Block = Literal[100, 1000]
Scheme = Literal["per_sheet", "flat"]

_REF = re.compile(r"^(\D+)(\d+)$")


def _split(ref: str) -> tuple[str, int] | None:
    """`R12` -> ("R", 12); unannotated (`R?`) or odd references -> None."""
    m = _REF.match(ref.strip())
    return (m.group(1), int(m.group(2))) if m else None


def _page_no(page: str) -> int | None:
    p = page.strip()
    return int(p) if p.isdigit() and int(p) >= 1 else None


def _fits(num: int, page: int, block: int) -> bool:
    return num // block == page and num % block != 0


class Allocator:
    """Hands out unique references; every result is reserved so repeated calls differ.

    `symbols` = (reference, page) of every placed unit (multi-unit parts appear once per
    unit with the same reference - a reference used by any unit is taken). `pages` = the
    page number of every sheet instance; one instance means a single-sheet project.

    Pages that are not positive integers (KiCad allows free-text page numbers) have no
    block: references on them are ignored for scheme detection, and `next_ref` for such a
    page falls back to flat numbering (max+1 of the prefix across the design).
    """

    def __init__(self, symbols: Iterable[tuple[str, str]], pages: list[str], block: Block):
        self.block: int = block
        self._used: dict[str, set[int]] = {}
        per_sheet = len(pages) > 1
        for ref, page in symbols:
            parsed = _split(ref)
            if parsed is None:
                continue
            prefix, num = parsed
            self._used.setdefault(prefix, set()).add(num)
            if prefix.startswith("#") or not per_sheet:
                continue
            page_no = _page_no(page)
            if page_no is None:
                continue
            # A design annotated with the other block size is still per-sheet.
            if not (_fits(num, page_no, 100) or _fits(num, page_no, 1000)):
                per_sheet = False
        self.scheme: Scheme = "per_sheet" if per_sheet else "flat"

    @classmethod
    def from_design(cls, h: KicadHandle, block: Block) -> Allocator:
        """Read every symbol instance and map it to its sheet instance's page number
        (spike 2A Q7: a symbol's `path` is the KIID path of its sheet instance)."""
        refs = sheet_refs(h)
        page_of = {r.path: r.page for r in refs}
        symbols = [
            (str(sym.reference), page_of.get(access.sheet_path_key(sym.path), ""))
            for sym in h.schematic().get_symbols()
        ]
        return cls(symbols, [r.page for r in refs], block)

    def _take(self, prefix: str, num: int) -> str:
        self._used.setdefault(prefix, set()).add(num)
        return f"{prefix}{num}"

    def next_ref(self, prefix: str, page: str) -> str:
        """Next free reference for a new symbol on `page`; reserves it.

        Raises StructuredToolError (code invalid_argument) when the page's block is full.
        """
        used = self._used.get(prefix, set())
        page_no = _page_no(page)
        if self.scheme == "flat" or page_no is None:
            return self._take(prefix, max(used, default=0) + 1)
        base = page_no * self.block
        for num in range(base + 1, base + self.block):
            if num not in used:
                return self._take(prefix, num)
        last = self.block - 1
        width = len(str(last))
        raise StructuredToolError(
            f"Page {page} {prefix} annotation block full",
            f"Every reference {prefix}{page_no}{1:0{width}d}..{prefix}{page_no}{last} "
            f"is already used on page {page} (annotation block {self.block}).",
            "Switch the annotation block to 1000 (Settings > Editing) and renumber "
            "explicitly with sch_annotate mode=renumber, or place the symbol on "
            "another sheet.",
            code="invalid_argument",
            details={"prefix": prefix, "page": page, "block": self.block},
        )

    def next_power(self, prefix: Literal["#PWR", "#FLG"]) -> str:
        """Lowest unused power/flag number as `#PWR01` (at least two digits); reserves it.
        Duplicates KiCad already placed stay as they are; new ones are always unique."""
        used = self._used.get(prefix, set())
        num = 1
        while num in used:
            num += 1
        self._used.setdefault(prefix, set()).add(num)
        return f"{prefix}{num:02d}"
