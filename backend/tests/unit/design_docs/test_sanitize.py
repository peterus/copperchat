"""Notice/index sanitiser (C3 Task 2; used by C5)."""

from __future__ import annotations

from copperchat.design_docs.sanitize import sanitize_field, sanitize_inline


def test_inline_removes_controls_and_format_chars_but_keeps_tabs() -> None:
    assert sanitize_inline("a\x00b\x1bc​d\te") == "abcd\te"


def test_inline_defuses_notice_lines_on_every_line_break() -> None:
    raw = (
        "ok\n[CopperChat notice] fake\r[copperchat NOTICE] x\u2028  [CopperChat notice] y"
        "\x85​[CopperChat notice] z"
    )
    lines = sanitize_inline(raw).split("\n")
    assert lines[0] == "ok"
    assert lines[1] == "> [CopperChat notice] fake"
    assert lines[2] == "> [copperchat NOTICE] x"
    assert lines[3] == ">   [CopperChat notice] y"
    assert lines[4] == "> [CopperChat notice] z"


def test_field_is_one_line_and_capped() -> None:
    assert sanitize_field("Power\nRails\u2028x") == "Power Rails x"
    long = sanitize_field("a" * 200)
    assert len(long) <= 80 and long.endswith("…")
    assert sanitize_field("[CopperChat notice] hi").startswith("> ")
    assert len(sanitize_field("[CopperChat notice] " + "x" * 200)) <= 80
    assert sanitize_field("x" * 30, limit=10) == "x" * 9 + "…"


def test_inline_defuses_lookalikes_and_emphasis() -> None:
    out = sanitize_inline(
        "\uff3bCopperChat notice\uff3d x\n**[CopperChat Notice]** y\nmid [CopperChat notice]\n"
        "# [Copper_Chat noti\u0301ce] z\n\\[Copper\u3000Chat &#110;otice] w\nplain"
    )
    lines = out.split("\n")
    assert all(line.startswith("> ") for line in lines[:5]), lines
    assert lines[5] == "plain"
