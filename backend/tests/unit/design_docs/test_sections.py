"""Section addressing (C3 Task 2)."""

from __future__ import annotations

import pytest

from copperchat.design_docs import sections as sec
from copperchat.design_docs.paths import DocError

BODY = """# Title

## Power

Intro.

### Rails

- 3V3

## Interfaces

```
## Not a heading
```

### Rails

- USB

## Open points

- one
- two
  - nested

#### Deep
"""


def test_levels_and_fences() -> None:
    texts = sec.heading_texts(BODY)
    assert texts == ["Title", "Power", "Rails", "Interfaces", "Rails", "Open points", "Deep"]
    assert sec.heading_texts(BODY, level=2) == ["Power", "Interfaces", "Open points"]
    assert sec.heading_texts("##### Five\n## Two ##\n") == ["Two"]


def test_duplicates_are_ambiguous_and_resolved_by_path() -> None:
    with pytest.raises(DocError) as err:
        sec.find(BODY, "Rails")
    assert err.value.code == "doc_section_ambiguous"
    assert "Power/Rails" in err.value.suggestion
    h = sec.find(BODY, "Interfaces/Rails")
    assert "- USB" in sec.section_text(BODY, h)
    assert sec.find(BODY, "  Power ").text == "Power"


def test_missing_lists_headings_and_create_appends_level_2() -> None:
    with pytest.raises(DocError) as err:
        sec.find(BODY, "Mechanical")
    assert err.value.code == "doc_section_not_found"
    assert "Interfaces" in err.value.why
    new, title = sec.patch(BODY, "Mechanical", "- 50x50 mm", mode="replace", create=True)
    assert title == "Mechanical"
    assert new.endswith("#### Deep\n\n## Mechanical\n\n- 50x50 mm\n")


def test_replace_section_keeps_neighbours() -> None:
    new, _ = sec.patch(BODY, "Power/Rails", "- 5V\n- 3V3", mode="replace", create=False)
    assert "### Rails\n\n- 5V\n- 3V3\n\n## Interfaces" in new
    assert "- USB" in new
    assert new.startswith("# Title\n\n## Power\n\nIntro.\n\n### Rails\n")
    assert new.replace("- 5V\n", "") == BODY


def test_append_mode_adds_to_end_of_section() -> None:
    new, _ = sec.patch(BODY, "Open points", "- three", mode="append", create=False)
    # the level-4 sub-heading belongs to the section, so the text goes after it
    assert new.endswith("- nested\n\n#### Deep\n- three\n")
    assert sec.count_bullets(new) == 3
    assert new.replace("- three\n", "") == BODY
    last, _ = sec.patch("## A\n\n- x\n", "A", "- y", mode="append", create=False)
    assert last == "## A\n\n- x\n- y\n"


def test_crlf_preserved() -> None:
    body = "## A\r\n\r\nold\r\n\r\n## B\r\nb\r\n"
    new, _ = sec.patch(body, "A", "new\nline", mode="replace", create=False)
    assert new == "## A\r\n\r\nnew\r\nline\r\n\r\n## B\r\nb\r\n"
    assert "\n" not in new.replace("\r\n", "")


def test_heading_in_new_text_refused_at_or_above_target_level() -> None:
    with pytest.raises(DocError) as err:
        sec.patch(BODY, "Interfaces", "x\n## Sneaky\n", mode="replace", create=False)
    assert err.value.code == "doc_heading_refused"
    with pytest.raises(DocError):
        sec.patch(BODY, "Power/Rails", "# Top", mode="append", create=False)
    new, _ = sec.patch(
        BODY, "Interfaces", "#### Sub\nok\n```\n## fenced\n```", mode="replace", create=False
    )
    assert "#### Sub" in new
    with pytest.raises(DocError):
        sec.patch(BODY, "Nope", "## X", mode="replace", create=True)


def test_append_end() -> None:
    assert sec.append_end("A\n", "B") == "A\n\nB\n"
    assert sec.append_end("", "B") == "B\n"
    assert sec.append_end("A\r\n", "B\nC") == "A\r\n\r\nB\r\nC\r\n"


def test_unclosed_fence_and_lone_cr_cannot_hide_headings() -> None:
    with pytest.raises(DocError) as err:
        sec.patch(BODY, "Interfaces", "```\nunclosed", mode="replace", create=False)
    assert err.value.code == "doc_heading_refused"
    with pytest.raises(DocError):
        sec.patch(BODY, "Interfaces", "x\r## Sneaky", mode="append", create=False)


def test_long_lines_parse_fast() -> None:
    import time

    line = "#" + " " * 50_000 + "x" + " " * 50_000 + "\n"
    t = time.monotonic()
    sec.heading_texts(line * 5 + "a" + "\t" * 100_000 + "#\n")
    assert time.monotonic() - t < 1.0
    assert sec.heading_texts("## Title ##\n## C# ##\n## ##\n") == ["Title", "C#", ""]


def test_backtick_info_string_with_backtick_is_not_a_fence() -> None:
    body = "## A\n``` x`y\n## B\n"
    assert sec.heading_texts(body) == ["A", "B"]
    with pytest.raises(DocError):
        sec.patch("## A\n", "A", "``` x`y\n## B", mode="append", create=False)


def test_heading_with_slash_is_addressable_by_path() -> None:
    body = "## MCU\n\n### I/O\n\na\n\n## Power\n\n### I/O\n\nb\n"
    with pytest.raises(DocError) as err:
        sec.find(body, "I/O")
    assert "MCU/I/O" in err.value.suggestion
    assert "a" in sec.section_text(body, sec.find(body, "MCU/I/O"))
    assert "b" in sec.section_text(body, sec.find(body, "Power / I/O"))
