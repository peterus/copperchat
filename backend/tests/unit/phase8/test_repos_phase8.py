from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.db import Database
from copperchat.db.repos import skill_prefs, uploads


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_skill_prefs_default_enabled_and_toggle(db: Database) -> None:
    assert await skill_prefs.disabled_skills(db, "p1") == set()
    await skill_prefs.set_skill_enabled(db, "p1", "copperchat-tools", False)
    assert await skill_prefs.disabled_skills(db, "p1") == {"copperchat-tools"}
    assert await skill_prefs.disabled_skills(db, "p2") == set()
    await skill_prefs.set_skill_enabled(db, "p1", "copperchat-tools", True)
    assert await skill_prefs.disabled_skills(db, "p1") == set()


async def test_upload_roundtrip_is_project_scoped(db: Database) -> None:
    row = uploads.UploadRow(
        id="a" * 32,
        project_id="p1",
        kind="image",
        name="board.jpg",
        rel_path=".kicad-agent/uploads/" + "a" * 32 + ".jpg",
        mime="image/jpeg",
        size=123,
        created_at="2026-10-15T00:00:00Z",
    )
    await uploads.insert_upload(db, row)
    assert await uploads.get_upload(db, "p1", "a" * 32) == row
    assert await uploads.get_upload(db, "p2", "a" * 32) is None
