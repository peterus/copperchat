"""`board.md` table parser (B-C6 `BoardDoc`): tables by header set, comment hints ignored,
Summary `sides:` / `Preset:` lines, bad rows skipped with an error, never raises."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from copperchat.board import boardmd
from copperchat.board.boardmd import (
    MAX_BYTES,
    AcceptedWarning,
    BlockRow,
    BoardDoc,
    ClassOverride,
    EdgeRow,
    NetClassRow,
    parse_board_md,
    read_board_doc,
)
from copperchat.design_docs.paths import DocError

TEMPLATE = (
    Path(boardmd.__file__).resolve().parents[1]
    / "skills"
    / "builtin"
    / "design-docs"
    / "templates"
    / "board.md"
)


def test_template_v2_parses_empty() -> None:
    doc = parse_board_md(TEMPLATE.read_text(encoding="utf-8"))
    assert doc == BoardDoc()
    assert doc.allowed_sides == frozenset({"top"})
    assert doc.preset is None


def test_comment_hints_ignored() -> None:
    text = (
        "## Placement\n"
        "<!-- Edge table:\n"
        "| Ref | Edge/position | Reason |\n"
        "|---|---|---|\n"
        "| J9 | left | hidden |\n"
        "-->\n"
        "<!-- | Ref | Class | Reason | -->\n"
    )
    doc = parse_board_md(text)
    assert doc == BoardDoc()


def test_unclosed_comment_hides_rest() -> None:
    text = "## Placement\n<!-- open\n| Ref | Edge/position | Reason |\n| J1 | left | x |\n"
    assert parse_board_md(text).edges == ()


def test_edges_table() -> None:
    text = (
        "## Placement\n"
        "| Ref | Edge/position | Reason |\n"
        "|-----|:-------------:|--------|\n"
        "| J1 | left edge, 10 mm from top | cable |\n"
        "| SW1 | near U1 | user access |\n"
    )
    doc = parse_board_md(text)
    assert doc.edges == (
        EdgeRow("J1", "left", "left edge, 10 mm from top", "cable"),
        EdgeRow("SW1", None, "near U1", "user access"),
    )
    assert doc.errors == ()


def test_blocks_refs_split() -> None:
    text = (
        "## Placement\n"
        "| Block | Parts | Region | Notes |\n"
        "|---|---|---|---|\n"
        "| MCU | U1, C1 C2 | centre | tight |\n"
    )
    doc = parse_board_md(text)
    assert doc.blocks == (BlockRow("MCU", ("U1", "C1", "C2"), "centre", "tight"),)


def test_class_override_unknown_class() -> None:
    text = (
        "## Part classes\n"
        "| Ref | Class | Reason |\n"
        "|---|---|---|\n"
        "| C7 | Bulk cap | not decoupling |\n"
        "| C8 | Decoupling-Cap | it is one |\n"
        "| J2 | module with antenna | radio |\n"
    )
    doc = parse_board_md(text)
    assert doc.class_overrides == (
        ClassOverride("C8", "decoupling_cap", "it is one"),
        ClassOverride("J2", "module_with_antenna", "radio"),
    )
    assert len(doc.errors) == 1
    assert doc.errors[0].startswith("part classes row 1: ")
    assert "bulk_cap" in doc.errors[0]


def test_empty_ref_and_cell_count_errors() -> None:
    text = (
        "## Placement\n"
        "| Ref | Edge/position | Reason |\n"
        "|---|---|---|\n"
        "|  | left | x |\n"
        "| J1 | left |\n"
        "| J2 | right | ok |\n"
    )
    doc = parse_board_md(text)
    assert doc.edges == (EdgeRow("J2", "right", "right", "ok"),)
    assert [e.split(":")[0] for e in doc.errors] == ["placement row 1", "placement row 2"]


def test_net_class_numbers() -> None:
    text = (
        "## Stack-up and rules\n"
        "| Class | Width | Clearance | Via | Nets |\n"
        "|---|---|---|---|---|\n"
        "| Power | 0.25 mm | 0.2mm | 0.6/0.3 | +3V3, VBUS GND |\n"
        "| Default |  | 0.15 | | |\n"
        "| Bad | wide | 0.2 | | |\n"
    )
    doc = parse_board_md(text)
    assert doc.net_classes == (
        NetClassRow("Power", 0.25, 0.2, "0.6/0.3", ("+3V3", "VBUS", "GND")),
        NetClassRow("Default", None, 0.15, "", ()),
    )
    assert len(doc.errors) == 1
    assert doc.errors[0].startswith("stack-up and rules row 3: ")


def test_accepted_warnings() -> None:
    text = (
        "## Checks\n"
        "| DRC type | Items/refs | Reason | Accepted by |\n"
        "|---|---|---|---|\n"
        "| DRCE_SILK_OVERLAP | U1, J1 | cosmetic | user 2026-11-20 |\n"
        "| Courtyards overlap | C1 C2 | intended | user |\n"
    )
    doc = parse_board_md(text)
    assert doc.accepted == (
        AcceptedWarning("silk_overlap", ("U1", "J1"), "cosmetic", "user 2026-11-20"),
        AcceptedWarning("courtyards_overlap", ("C1", "C2"), "intended", "user"),
    )


def test_tables_recognised_in_any_section() -> None:
    text = "## Routing\n| Reason | Ref | Edge/position |\n|---|---|---|\n| usb | J1 | bottom |\n"
    doc = parse_board_md(text)
    assert doc.edges == (EdgeRow("J1", "bottom", "bottom", "usb"),)


def test_unknown_table_ignored() -> None:
    text = "## Placement\n| Ref | Value |\n|---|---|\n| R1 | 10k |\n"
    assert parse_board_md(text) == BoardDoc()


def test_sides_top_bottom() -> None:
    doc = parse_board_md("## Summary\nAssembly: JLC PCBA; sides: top+bottom\n")
    assert doc.allowed_sides == frozenset({"top", "bottom"})
    assert doc.errors == ()


def test_sides_top() -> None:
    doc = parse_board_md("## Summary\n- assembly: hand; SIDES: Top\n")
    assert doc.allowed_sides == frozenset({"top"})
    assert doc.errors == ()


def test_sides_default() -> None:
    doc = parse_board_md("## Summary\nFab: JLCPCB, 2 layers.\nAssembly: hand\n")
    assert doc.allowed_sides == frozenset({"top"})
    assert doc.errors == ()


def test_sides_invalid() -> None:
    doc = parse_board_md("## Summary\nAssembly: hand; sides: left\n")
    assert doc.allowed_sides == frozenset({"top"})
    assert len(doc.errors) == 1
    assert "left" in doc.errors[0]


def test_preset_line() -> None:
    doc = parse_board_md("## Summary\nPreset:  JLCPCB-4L \nAssembly: hand; sides: top\n")
    assert doc.preset == "jlcpcb-4l"


def test_preset_absent_or_outside_summary() -> None:
    assert parse_board_md("## Summary\nFab: x\n").preset is None
    assert parse_board_md("## Mechanical\nPreset: jlcpcb-2l\n").preset is None


def test_heading_case_insensitive() -> None:
    text = (
        "## SUMMARY\n"
        "Assembly: reflow; sides: TOP+BOTTOM\n"
        "Preset: jlcpcb-2l\n"
        "## PART CLASSES\n"
        "| REF | class | Reason |\n"
        "|---|---|---|\n"
        "| C1 | other | bulk |\n"
        "| C2 | nope | x |\n"
    )
    doc = parse_board_md(text)
    assert doc.allowed_sides == frozenset({"top", "bottom"})
    assert doc.preset == "jlcpcb-2l"
    assert doc.class_overrides == (ClassOverride("C1", "other", "bulk"),)
    assert doc.errors[0].startswith("part classes row 2: ")


def test_oversize_truncated() -> None:
    head = "## Placement\n| Ref | Edge/position | Reason |\n|---|---|---|\n| J1 | left | a |\n"
    filler = "x" * (MAX_BYTES + 10) + "\n"
    tail = "| J2 | right | b |\n"
    doc = parse_board_md(head + filler + tail)
    assert [e.ref for e in doc.edges] == ["J1"]
    assert any("256 KiB" in e for e in doc.errors)


def test_errors_capped() -> None:
    rows = "".join(f"| R{i} | bogus | x |\n" for i in range(500))
    doc = parse_board_md("## Part classes\n| Ref | Class | Reason |\n|---|---|---|\n" + rows)
    assert len(doc.errors) <= boardmd.MAX_ERRORS + 1
    assert "more" in doc.errors[-1]


_ALPHABET = ["|", "#", "## ", "-", "<!--", "-->", "\n", " ", "Ref", "Class", "mm", ":", "ä", "😀"]
_HEADERS = [
    "| Ref | Edge/position | Reason |",
    "| Block | Parts | Region | Notes |",
    "| Ref | Class | Reason |",
    "| Class | Width | Clearance | Via | Nets |",
    "| DRC type | Items/refs | Reason | Accepted by |",
    "## Summary\nAssembly: x; sides: ",
    "Preset:",
]


def test_never_raises() -> None:
    rng = random.Random(11)
    for case in range(500):
        parts: list[str] = []
        for _ in range(rng.randint(0, 80)):
            roll = rng.random()
            if roll < 0.1:
                parts.append(rng.choice(_HEADERS) + "\n")
            elif roll < 0.15:
                parts.append(chr(rng.randint(0, 0x10FFFF) & 0xD7FF))
            else:
                parts.append(rng.choice(_ALPHABET))
        text = "".join(parts)
        try:
            doc = parse_board_md(text)
        except Exception as exc:  # pragma: no cover - failure path
            pytest.fail(f"seed 11 case {case} raised {exc!r} on {text!r}")
        assert isinstance(doc, BoardDoc), f"seed 11 case {case}: {text!r}"


class _FakeDocs:
    def __init__(self, body: str | None) -> None:
        self.body = body
        self.calls: list[tuple[Any, str]] = []

    async def read(self, project: Any, path: str) -> SimpleNamespace:
        self.calls.append((project, path))
        if self.body is None:
            raise DocError("doc_not_found", "board.md", "missing", "create it")
        return SimpleNamespace(body=self.body)


async def test_read_board_doc_without_docs() -> None:
    ctx: Any = SimpleNamespace(docs=None, project="p")
    assert await read_board_doc(ctx) == BoardDoc()


async def test_read_board_doc_missing() -> None:
    docs = _FakeDocs(None)
    ctx: Any = SimpleNamespace(docs=docs, project="p")
    assert await read_board_doc(ctx) == BoardDoc()
    assert docs.calls == [("p", "board.md")]


async def test_read_board_doc_present() -> None:
    docs = _FakeDocs("## Summary\nAssembly: x; sides: top+bottom\n")
    ctx: Any = SimpleNamespace(docs=docs, project="p")
    doc = await read_board_doc(ctx)
    assert doc.allowed_sides == frozenset({"top", "bottom"})
