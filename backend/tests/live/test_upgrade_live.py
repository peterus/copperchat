"""Opt-in upgrade check with the real model (C6 Task 1, Review Focus 2).

``scripts/live-upgrade-check.sh`` lets a v0.2.0 container chat once in a fresh data dir
(the agent is told a code word), then runs this test in the kicad-base image with the
same ``/projects`` and ``/data`` paths (the CLI's session store is keyed by the project
path). The app migrates the database, the migrated "Session 1" is opened, and one message
asks for the code word: the resumed SDK session must remember it.

    COPPERCHAT_UPGRADE_DATA=/data COPPERCHAT_UPGRADE_PROJECTS=/projects \\
    COPPERCHAT_UPGRADE_WORD=<word> uv run pytest -m live_sdk tests/live/test_upgrade_live.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from copperchat.db.repos.conversations import ConversationRepo

from .conftest import live
from .sessions_app import app_settings, live_app
from .test_editing_eval import write_result

DATA = os.environ.get("COPPERCHAT_UPGRADE_DATA")
PROJECTS = os.environ.get("COPPERCHAT_UPGRADE_PROJECTS")
WORD = os.environ.get("COPPERCHAT_UPGRADE_WORD", "")

pytestmark = [
    *live,
    pytest.mark.skipif(
        not (DATA and PROJECTS and WORD),
        reason="run through scripts/live-upgrade-check.sh (needs a v0.2.0 data dir)",
    ),
]


@pytest.mark.timeout(600)
async def test_migrated_session_resumes_with_its_memory(tmp_path: Path) -> None:
    assert DATA and PROJECTS
    settings = app_settings(
        tmp_path,
        projects_root=Path(PROJECTS),
        data_dir=Path(DATA),
        kicad_cli="kicad-cli-not-installed",
    )
    async with live_app(settings) as app:
        r = await app.client.get(app.url("/conversations"))
        assert r.status_code == 200, r.text
        [conv] = r.json()
        row = await ConversationRepo(app.db).get(conv["id"])
        assert row is not None and row.sdk_session_id, "the old SDK session moved over"
        old = await app.turn(conv["id"])
        await app.send(
            conv["id"], "What was the code word I asked you to remember? Reply with it only."
        )
        new = await app.turn(conv["id"])
        reply = new.texts()[-1] if new.texts() else ""
        result = {
            "conversation": {k: conv[k] for k in ("seq", "title", "type", "status")},
            "rows_before": len(old.rows),
            "rows_after": len(new.rows),
            "resumed_session": row.sdk_session_id,
            "reply": reply[:200],
            "remembered": WORD in reply,
        }
    write_result("sessions_upgrade", result)
    print(result)
    assert (conv["seq"], conv["title"], conv["type"]) == (1, "Session 1", "free")
    assert len(old.rows) >= 2, "the v0.2.0 history is in Session 1"
    assert WORD in reply, reply
