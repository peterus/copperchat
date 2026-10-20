"""Server-owned front matter (C3 Task 2)."""

from __future__ import annotations

from copperchat.design_docs import frontmatter as fm


def test_rebuild_keeps_unknown_keys_and_owns_server_keys() -> None:
    text = (
        "---\ndoc: whatever\nstatus: approved\nowner: Peter\nuser_edited: true\n"
        "approved_at: 2020-01-01\n# a comment\n---\nBody\n"
    )
    parsed = fm.split(text)
    assert parsed.has_block and parsed.body == "Body\n"
    assert parsed.extra_lines == ("owner: Peter\n", "# a comment\n")
    assert parsed.user_edited
    out = fm.build(
        doc_key="requirements",
        approved=False,
        approved_at=None,
        user_edited=False,
        body=parsed.body,
        extra_lines=parsed.extra_lines,
    )
    assert out == (
        "---\ndoc: requirements\nstatus: draft\nuser_edited: false\napproved_at:\n"
        "owner: Peter\n# a comment\n---\nBody\n"
    )
    assert fm.split(out).body == "Body\n"


def test_approved_mirror_and_crlf() -> None:
    out = fm.build(
        doc_key="parts",
        approved=True,
        approved_at="2026-10-19T10:00:00.000Z",
        user_edited=True,
        body="A\r\nB\r\n",
        extra_lines=("x: 1\n",),
        newline="\r\n",
    )
    assert out.startswith("---\r\ndoc: parts\r\nstatus: approved\r\nuser_edited: true\r\n")
    assert "approved_at: 2026-10-19T10:00:00.000Z\r\nx: 1\r\n---\r\nA\r\nB\r\n" in out
    assert fm.split(out).body == "A\r\nB\r\n"


def test_no_block_or_unterminated_block_is_all_body() -> None:
    assert fm.split("Hello\n---\nx\n") == fm.Parsed({}, (), "Hello\n---\nx\n", False)
    assert fm.split("---\nkey: v\nno end\n").body == "---\nkey: v\nno end\n"


def test_later_dashes_stay_in_body() -> None:
    text = "---\ndoc: a\n---\nIntro\n\n---\nstatus: approved\n---\n"
    parsed = fm.split(text)
    assert parsed.body == "Intro\n\n---\nstatus: approved\n---\n"
    assert parsed.meta == {"doc": "a"}


def test_version_is_body_hash() -> None:
    v = fm.body_version("Body\n")
    assert v.startswith("v") and len(v) == 17
    assert v == fm.body_version("Body\n") != fm.body_version("Body")
