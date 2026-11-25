"""The board session's doc write policy and the board.md v2 template (SP4 P1c Task 3)."""

from __future__ import annotations

import pytest

from copperchat.design_docs.paths import DocError
from copperchat.design_docs.service import ConversationInfo, DesignDocs, policy_for
from copperchat.projects import Project

CONV = ConversationInfo(id="b" * 32, seq=3, title="Session 3", type="board")
SECTIONS = [
    "Summary",
    "Mechanical",
    "Stack-up and rules",
    "Placement",
    "Part classes",
    "Routing",
    "Checks",
    "Open points",
]
KW = {"summary": "s", "run_id": "r", "conversation": CONV}


async def test_patch_any_board_section(docs: DesignDocs, project: Project) -> None:
    board = policy_for("board", CONV.id)
    v = None
    for name in SECTIONS:
        res = await docs.patch_section(
            project, "board.md", name, f"- {name} text", v, policy=board, **KW
        )
        v = res.version
    body = (await docs.read(project, "board.md")).body
    for name in SECTIONS:
        assert f"- {name} text" in body, name


async def test_patch_creates_missing_board_md(docs: DesignDocs, project: Project) -> None:
    board = policy_for("board", CONV.id)
    assert not (await docs.read(project, "board.md")).exists
    await docs.patch_section(
        project, "board.md", "Fab notes", "- JLCPCB", None, create=True, policy=board, **KW
    )
    got = await docs.read(project, "board.md")
    assert got.exists
    assert "## Fab notes" in got.body and "- JLCPCB" in got.body
    assert "## Stack-up and rules" in got.body  # started from the template


async def test_append_board_and_decisions(docs: DesignDocs, project: Project) -> None:
    board = policy_for("board", CONV.id)
    await docs.append(project, "board.md", "- end", policy=board, **KW)
    await docs.append(project, "board.md", "- q", section="Open points", policy=board, **KW)
    await docs.append(
        project, "decisions.md", "Four layers.", title="Layer count", policy=board, **KW
    )
    assert "- q" in (await docs.read(project, "board.md")).body
    assert "Four layers." in (await docs.read(project, "decisions.md")).body


async def test_write_refused(docs: DesignDocs, project: Project) -> None:
    board = policy_for("board", CONV.id)
    for path in ("board.md", "architecture.md"):
        with pytest.raises(DocError) as err:
            await docs.write(project, path, "x", None, author="agent", policy=board, **KW)
        assert err.value.code == "doc_scope_refused", path
    with pytest.raises(DocError) as err:  # decisions.md is append-only for every session
        await docs.write(project, "decisions.md", "x", None, author="agent", policy=board, **KW)
    assert err.value.code in ("doc_scope_refused", "doc_append_only")


async def test_patch_other_doc_refused(docs: DesignDocs, project: Project) -> None:
    board = policy_for("board", CONV.id)
    for path, section in (
        ("requirements.md", "Open points"),
        ("parts.md", "Key parts"),
        ("architecture.md", "Open points"),
    ):
        with pytest.raises(DocError) as err:
            await docs.patch_section(
                project, path, section, "x", None, create=True, policy=board, **KW
            )
        assert err.value.code == "doc_scope_refused", path
    with pytest.raises(DocError) as err:
        await docs.append(project, "requirements.md", "x", policy=board, **KW)
    assert err.value.code == "doc_scope_refused"


async def test_template_v2_sections(docs: DesignDocs, project: Project) -> None:
    template = (await docs.read(project, "board.md")).template
    assert template is not None
    assert template.startswith("# Board\n")
    headings = [line[3:] for line in template.splitlines() if line.startswith("## ")]
    assert headings == SECTIONS
    assert (
        "<!-- Preset, stackup, net classes table: | Class | Width | Clearance | Via | Nets | -->"
        in template
    )
