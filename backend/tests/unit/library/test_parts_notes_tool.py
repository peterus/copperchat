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


async def test_symlinked_notes_file_not_read(
    project: Project, make_settings: Any, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    d = project.dir / "docs" / "parts" / "LNK"
    d.mkdir(parents=True)
    (d / "notes.md").symlink_to(secret)
    result = await TOOL.handler(_ctx(project, make_settings), {"mpn": "LNK"})
    assert result.data is not None and result.data["researched"] is False


async def test_multibyte_cut_and_pin_cap(
    project: Project, make_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.library import specs as specs_mod
    from copperchat.library.symbols import LibPin, SymbolDetail

    notes = project.dir / "docs" / "parts" / "UTF" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("ä" * specs_mod.MAX_NOTES_RESULT_BYTES)  # 2 bytes per char
    result = await TOOL.handler(_ctx(project, make_settings), {"mpn": "UTF"})
    assert result.data is not None and result.data["truncated"] is True
    assert set(result.data["notes"]) == {"ä"}

    pins = [
        LibPin(str(i), f"P{i}", "passive", 1 + i % 2, 1, (0.0, 0.0), "left") for i in range(300)
    ]
    detail = SymbolDetail("FPGA:Big", "", "", "", [], "", 2, 1, False, pins, (10.0, 10.0), None)

    class Svc:
        async def wait_ready(self, timeout: float) -> bool:
            return True

        async def get_symbol(self, lib_id: str) -> SymbolDetail:
            return detail

    monkeypatch.setattr(specs_mod, "library_for", lambda session: Svc())
    tool = next(s for s in TOOL_SPECS if s.name == "lib_get_symbol")
    out = (await tool.handler(_ctx(project, make_settings), {"lib_id": "FPGA:Big"})).data
    assert out is not None and len(out["pins"]) == 256 and out["pins_truncated"] is True
    assert out["pin_count"] == 300
    out = (await tool.handler(_ctx(project, make_settings), {"lib_id": "FPGA:Big", "unit": 2})).data
    assert out is not None and len(out["pins"]) == 150 and "pins_truncated" not in out
    assert {p["unit"] for p in out["pins"]} == {2}
