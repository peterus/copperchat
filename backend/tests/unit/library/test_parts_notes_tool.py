from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import KicadSession
from copperchat.library.specs import TOOL_SPECS
from copperchat.projects import Project
from copperchat.tools import StructuredToolError, ToolContext
from copperchat.tools.registry import all_specs
from tests.unit.phase8.conftest import make_project

TOOL = next(s for s in TOOL_SPECS if s.name == "parts_get_notes")


def _ctx(project: Project, make_settings: Any) -> ToolContext:
    settings: Settings = make_settings()
    return ToolContext(project=project, session=cast(KicadSession, None), settings=settings)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path / "projects" / "proj")


async def test_returns_the_notes(project: Project, make_settings: Any) -> None:
    notes = project.dir / "docs" / "parts" / "TMP102AIDRLR" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("---\nmpn: TMP102AIDRLR\n---\n# TMP102\nI2C sensor\n")
    result = await TOOL.handler(_ctx(project, make_settings), {"mpn": "TMP102AIDRLR"})
    assert result.data is not None
    assert result.data["researched"] is True and "I2C sensor" in result.data["notes"]


async def test_unknown_part_not_researched(project: Project, make_settings: Any) -> None:
    result = await TOOL.handler(_ctx(project, make_settings), {"mpn": "LM358"})
    assert result.data == {
        "mpn": "LM358",
        "researched": False,
        "note": "No notes yet: call research_part before relying on this part.",
    }


@pytest.mark.parametrize("mpn", ["../x", "..", "", "a" * 65, ".hidden"])
async def test_invalid_mpn(project: Project, make_settings: Any, mpn: str) -> None:
    with pytest.raises(StructuredToolError) as err:
        await TOOL.handler(_ctx(project, make_settings), {"mpn": mpn})
    assert err.value.code == "invalid_argument"
    assert err.value.to_dict()["code"] == "invalid_argument"


async def test_symlinked_part_folder_refused(
    project: Project, make_settings: Any, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("secret")
    parts = project.dir / "docs" / "parts"
    parts.mkdir(parents=True)
    (parts / "EVIL").symlink_to(outside)
    with pytest.raises(StructuredToolError) as err:
        await TOOL.handler(_ctx(project, make_settings), {"mpn": "EVIL"})
    assert err.value.code == "invalid_argument"


def test_registered_and_flags() -> None:
    specs = {s.name: s for s in all_specs()}
    for name in ("lib_search_symbols", "lib_get_symbol", "lib_search_footprints"):
        assert specs[name].requires_kicad and not specs[name].mutating
    assert specs["parts_get_notes"].requires_kicad is False
    assert not specs["parts_get_notes"].mutating


async def test_large_notes_truncated(project: Project, make_settings: Any) -> None:
    from copperchat.library.specs import MAX_NOTES_RESULT_BYTES

    notes = project.dir / "docs" / "parts" / "BIG" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("x" * (MAX_NOTES_RESULT_BYTES + 100))
    result = await TOOL.handler(_ctx(project, make_settings), {"mpn": "BIG"})
    assert result.data is not None and result.data["truncated"] is True
    assert len(result.data["notes"]) == MAX_NOTES_RESULT_BYTES


@pytest.mark.parametrize("lib_id", [None, 5, "ab", "x" * 257])
async def test_get_symbol_rejects_invalid_lib_id(
    project: Project, make_settings: Any, lib_id: Any
) -> None:
    tool = next(s for s in TOOL_SPECS if s.name == "lib_get_symbol")
    ctx = _ctx(project, make_settings)
    ctx.session = cast(KicadSession, object())  # never reached: the argument is checked first
    from copperchat.library import specs as specs_mod

    class Ready:
        async def wait_ready(self, timeout: float) -> bool:
            return True

    orig = specs_mod.library_for
    specs_mod.library_for = lambda session: Ready()  # type: ignore[assignment,return-value]
    try:
        with pytest.raises(StructuredToolError) as err:
            await tool.handler(ctx, {"lib_id": lib_id})
    finally:
        specs_mod.library_for = orig
    assert err.value.code == "invalid_argument"
