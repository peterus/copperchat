"""Hard tests of the part-researcher path/network guard and the main-agent guard."""

import os
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from copperchat.agent.guards import DELEGATION_TOOL, make_main_guard, make_researcher_guard
from copperchat.projects import Project
from copperchat.research.agent import CHECK_NOTES_TOOL, DOWNLOAD_TOOL

from .conftest import make_project, public_resolver


@pytest.fixture
def proj(project: Project) -> Project:
    d = project.dir
    (d / "docs" / "parts" / "TPS62133").mkdir(parents=True)
    (d / "docs" / "parts" / "_uploads").mkdir()
    (d / ".kicad-agent" / "uploads").mkdir(parents=True)
    (d / "docs" / "parts" / "_uploads" / "a.pdf").write_bytes(b"%PDF-1.7\n")
    return project


async def decide(project: Project, tool: str, inp: dict[str, Any]) -> bool:
    guard = make_researcher_guard(project, resolver=public_resolver)
    res = await guard(tool, inp, ToolPermissionContext(tool_use_id="t1"))
    assert isinstance(res, PermissionResultAllow | PermissionResultDeny)
    if isinstance(res, PermissionResultAllow):
        assert res.updated_input == inp
    return isinstance(res, PermissionResultAllow)


async def test_allowed_calls(proj: Project) -> None:
    notes = proj.dir / "docs/parts/TPS62133/notes.md"
    allowed: list[tuple[str, dict[str, Any]]] = [
        ("Write", {"file_path": str(notes), "content": "x"}),
        ("Write", {"file_path": "docs/parts/NEW-PART/notes.md", "content": "x"}),
        ("Write", {"file_path": "docs/parts/LM358_NOPB/errata.txt", "content": "x"}),
        ("Edit", {"file_path": "docs/parts/NEW/notes.md", "old_string": "a", "new_string": "b"}),
        ("Read", {"file_path": str(proj.dir / "docs/parts/_uploads/a.pdf")}),
        ("Read", {"file_path": "docs/parts/TPS62133/datasheet.pdf"}),
        ("Read", {"file_path": str(proj.dir / ".kicad-agent/uploads/x.png")}),
        ("Glob", {"pattern": "**/*", "path": str(proj.dir / "docs/parts")}),
        ("Glob", {"pattern": "*.md", "path": "docs/parts/TPS62133"}),
        ("WebSearch", {"query": "TPS62133 datasheet"}),
        ("WebFetch", {"url": "https://www.ti.com/lit/ds/symlink/tps62133.pdf", "prompt": "p"}),
        (DOWNLOAD_TOOL, {"mpn": "TPS62133", "url": "https://x"}),
        (CHECK_NOTES_TOOL, {"mpn": "TPS62133"}),
    ]
    for tool, inp in allowed:
        assert await decide(proj, tool, inp), (tool, inp)


DENIED: list[tuple[str, dict[str, Any]]] = [
    # traversal and foreign paths
    ("Write", {"file_path": "docs/parts/../../evil.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/../../../evil.md", "content": "x"}),
    ("Write", {"file_path": "/etc/passwd", "content": "x"}),
    ("Write", {"file_path": "/tmp/notes.md", "content": "x"}),
    ("Write", {"file_path": "proj.kicad_sch", "content": "x"}),
    ("Write", {"file_path": "docs/notes.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts", "content": "x"}),
    ("Write", {"file_path": "docs/parts/notes.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/sub/notes.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/datasheet.pdf", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/run.sh", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/.hidden.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/.git/notes.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/_uploads/x.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/_uploads/a.pdf", "content": "x"}),
    ("Write", {"file_path": "docs/parts/bad;name/notes.md", "content": "x"}),
    ("Write", {"file_path": "docs/parts/X/notes.md\x00.txt", "content": "x"}),
    ("Write", {"file_path": "", "content": "x"}),
    ("Write", {"content": "x"}),
    ("Edit", {"file_path": ".kicad-agent/uploads/x.png", "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "../proj/docs/parts/X/notes.md", "old_string": "a", "new_string": "b"}),
    ("Read", {"file_path": "/data/vapid.json"}),
    ("Read", {"file_path": "proj.kicad_pcb"}),
    ("Read", {"file_path": ".kicad-agent/history/x"}),
    ("Read", {"file_path": "docs/parts/../proj.kicad_pro"}),
    ("Read", {"file_path": ""}),
    ("Glob", {"pattern": "**/*"}),
    ("Glob", {"pattern": "../**/*", "path": "docs/parts"}),
    ("Glob", {"pattern": "/etc/*", "path": "docs/parts"}),
    ("Glob", {"pattern": "**/*", "path": "."}),
    # network: only public http(s)
    ("WebFetch", {"url": "http://localhost:8080/api/projects", "prompt": "p"}),
    ("WebFetch", {"url": "http://127.0.0.1/", "prompt": "p"}),
    ("WebFetch", {"url": "http://[::1]/", "prompt": "p"}),
    ("WebFetch", {"url": "http://169.254.169.254/latest/meta-data", "prompt": "p"}),
    ("WebFetch", {"url": "http://192.168.88.5:8765/", "prompt": "p"}),
    ("WebFetch", {"url": "https://nas.internal/x.pdf", "prompt": "p"}),
    ("WebFetch", {"url": "file:///etc/passwd", "prompt": "p"}),
    ("WebFetch", {"url": "https://user:pw@ti.com/x", "prompt": "p"}),
    ("WebFetch", {"prompt": "p"}),
    # tools the researcher does not have
    ("Bash", {"command": "ls"}),
    ("Grep", {"pattern": "x", "path": "docs/parts"}),
    ("NotebookEdit", {"notebook_path": "docs/parts/a.ipynb"}),
    ("Agent", {"prompt": "x"}),
    ("Task", {"prompt": "x"}),
    ("Skill", {"skill": "x"}),
    ("mcp__copperchat__run_erc", {}),
    ("mcp__copperchat__research_part", {"mpn": "X", "query": "q"}),
]


@pytest.mark.parametrize(("tool", "inp"), DENIED)
async def test_denied(proj: Project, tool: str, inp: dict[str, Any]) -> None:
    assert not await decide(proj, tool, inp)
    assert not (proj.dir.parent / "evil.md").exists()


async def test_symlink_escape_denied(proj: Project, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj.dir / "docs/parts/LINK").symlink_to(outside, target_is_directory=True)
    assert not await decide(
        proj, "Write", {"file_path": "docs/parts/LINK/notes.md", "content": "x"}
    )
    assert not await decide(proj, "Read", {"file_path": "docs/parts/LINK/notes.md"})
    assert not await decide(proj, "Glob", {"pattern": "*", "path": "docs/parts/LINK"})


async def test_symlinked_notes_file_pointing_outside_denied(proj: Project, tmp_path: Path) -> None:
    target = tmp_path / "victim.md"
    target.write_text("keep")
    (proj.dir / "docs/parts/TPS62133/notes.md").symlink_to(target)
    assert not await decide(
        proj, "Write", {"file_path": "docs/parts/TPS62133/notes.md", "content": "x"}
    )


async def test_symlink_into_project_files_denied(proj: Project) -> None:
    (proj.dir / "proj.kicad_sch").write_text("(kicad_sch)")
    (proj.dir / "docs/parts/TPS62133/notes.md").symlink_to(proj.dir / "proj.kicad_sch")
    assert not await decide(
        proj, "Write", {"file_path": "docs/parts/TPS62133/notes.md", "content": "x"}
    )
    assert not await decide(proj, "Read", {"file_path": "docs/parts/TPS62133/notes.md"})


async def test_symlinked_uploads_dir_cannot_be_written_through(proj: Project) -> None:
    (proj.dir / "docs/parts/ALIAS").symlink_to(
        proj.dir / "docs/parts/_uploads", target_is_directory=True
    )
    assert not await decide(proj, "Write", {"file_path": "docs/parts/ALIAS/x.md", "content": "x"})


async def test_parts_root_symlinked_outside_denied(tmp_path: Path) -> None:
    project = make_project(tmp_path / "p2", "ba9876543210")
    (project.dir / "docs").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (project.dir / "docs" / "parts").symlink_to(outside, target_is_directory=True)
    assert not await decide(
        project, "Write", {"file_path": "docs/parts/X/notes.md", "content": "x"}
    )
    assert not await decide(project, "Read", {"file_path": "docs/parts/X/notes.md"})


async def test_docs_dir_symlinked_outside_denied(tmp_path: Path) -> None:
    project = make_project(tmp_path / "p3", "ba9876543211")
    outside = tmp_path / "docs-elsewhere"
    (outside / "parts").mkdir(parents=True)
    (project.dir / "docs").symlink_to(outside, target_is_directory=True)
    assert not await decide(
        project, "Write", {"file_path": "docs/parts/X/notes.md", "content": "x"}
    )


async def test_absolute_path_via_other_project_denied(tmp_path: Path, proj: Project) -> None:
    other = make_project(tmp_path / "projects" / "other", "ba9876543212")
    (other.dir / "docs/parts/X").mkdir(parents=True)
    assert not await decide(
        proj, "Write", {"file_path": str(other.dir / "docs/parts/X/notes.md"), "content": "x"}
    )


async def test_symlink_loop_denied(proj: Project) -> None:
    a = proj.dir / "docs/parts/LOOP"
    os.symlink(a, a)
    assert not await decide(
        proj, "Write", {"file_path": "docs/parts/LOOP/notes.md", "content": "x"}
    )


async def test_user_edited_notes_are_protected(proj: Project) -> None:
    notes = proj.dir / "docs/parts/TPS62133/notes.md"
    notes.write_text("---\nuser_edited: true\n---\nMine\n")
    assert not await decide(proj, "Write", {"file_path": str(notes), "content": "x"})
    assert not await decide(
        proj, "Edit", {"file_path": str(notes), "old_string": "Mine", "new_string": "x"}
    )
    assert await decide(proj, "Read", {"file_path": str(notes)})
    notes.write_text("---\nuser_edited: false\n---\nResearcher\n")
    assert await decide(proj, "Write", {"file_path": str(notes), "content": "x"})


# --- main agent -------------------------------------------------------------------------

MAIN = frozenset({"mcp__copperchat__run_erc", DELEGATION_TOOL})


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [
        ("mcp__copperchat__run_erc", True),
        (DELEGATION_TOOL, True),
        ("Skill", True),
        ("mcp__copperchat__unknown", False),
        ("Read", False),
        ("Write", False),
        ("WebFetch", False),
        ("WebSearch", False),
        ("Glob", False),
        ("Bash", False),
        ("Agent", False),
        ("Task", False),
        (DOWNLOAD_TOOL, False),
        (CHECK_NOTES_TOOL, False),
    ],
)
async def test_main_guard(tool: str, allowed: bool) -> None:
    seen: list[tuple[str, str | None]] = []
    guard = make_main_guard(MAIN, on_allowed=lambda t, _i, ctx: seen.append((t, ctx.tool_use_id)))
    res = await guard(tool, {"x": 1}, ToolPermissionContext(tool_use_id="tu1"))
    assert isinstance(res, PermissionResultAllow) is allowed
    assert seen == ([(tool, "tu1")] if allowed else [])
