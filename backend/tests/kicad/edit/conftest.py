from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.db import Database


@pytest.fixture
async def edit_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.open(tmp_path / "edit.db")
    try:
        yield db
    finally:
        await db.close()
