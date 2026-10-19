import asyncio

import pytest
import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.conversations import ConversationRepo, ConversationRow
from copperchat.errors import (
    ConversationNotFound,
    ConversationStateConflict,
    CopperChatError,
)

PA, PB = "a1b2c3d4e5f6", "ffffffffffff"


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_create_allocates_seq_and_default_title(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    b = await repo.create(PA, "requirements", "Specs")
    c = await repo.create(PA, "schematic", None)
    other = await repo.create(PB, "free", None)
    assert isinstance(a, ConversationRow)
    assert [a.seq, b.seq, c.seq, other.seq] == [1, 2, 3, 1]
    assert (a.title, b.title, c.title) == ("Session 1", "Specs", "Session 3")
    assert (a.type, a.status, a.archived, a.sdk_session_id, a.docs_seen) == (
        "free",
        "open",
        False,
        None,
        {},
    )
    assert len(a.id) == 32 and a.created_at.endswith("Z") and a.last_message_at is None


async def test_concurrent_creates_get_distinct_seq(db) -> None:
    repo = ConversationRepo(db)
    rows = await asyncio.gather(*(repo.create(PA, "free", None) for _ in range(5)))
    assert sorted(r.seq for r in rows) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "title",
    [
        "",
        "a\nb",
        "x" * 121,
        "tab\there\r",
        "   ",
        "a\u2028b",
        "evil\u202etxt",
        "zero\u200bwidth",
        "lone\ud800surrogate",
    ],
)
async def test_title_is_validated(db, title: str) -> None:
    with pytest.raises(CopperChatError) as err:
        await ConversationRepo(db).create(PA, "free", title)
    assert err.value.http_status in (400, 422)


async def test_type_is_validated(db) -> None:
    with pytest.raises(CopperChatError):
        await ConversationRepo(db).create(PA, "chat", None)


async def test_get_for_project_rejects_foreign_project(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    assert (await repo.get_for_project(PA, a.id)).id == a.id
    with pytest.raises(ConversationNotFound) as err:
        await repo.get_for_project(PB, a.id)
    assert err.value.code == "conversation_not_found" and err.value.http_status == 404
    with pytest.raises(ConversationNotFound):
        await repo.get_for_project(PA, "0" * 32)
    assert await repo.get("0" * 32) is None


async def test_list_update_and_archive(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    b = await repo.create(PA, "free", None)
    await asyncio.sleep(0.01)  # millisecond timestamps
    await repo.touch(a.id)
    assert [r.id for r in await repo.list(PA)] == [a.id, b.id]  # newest activity first
    renamed = await repo.update(b.id, title="Renamed")
    assert renamed.title == "Renamed" and renamed.updated_at >= b.updated_at
    await repo.update(a.id, archived=True)
    assert [r.id for r in await repo.list(PA)] == [b.id]
    assert [r.id for r in await repo.list(PA, archived=True)] == [a.id]
    with pytest.raises(CopperChatError):
        await repo.update(a.id, title="bad\nline")


async def test_set_status_transitions(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    closing = await repo.set_status(a.id, "closing", expect="open")
    assert closing.status == "closing" and closing.closed_at is None
    with pytest.raises(ConversationStateConflict) as err:
        await repo.set_status(a.id, "closing", expect="open")
    assert err.value.http_status == 409
    closed = await repo.set_status(a.id, "closed", expect="closing")
    assert closed.status == "closed" and closed.closed_at is not None
    reopened = await repo.set_status(a.id, "open", expect="closed")
    assert reopened.status == "open" and reopened.closed_at is None


async def test_sdk_session_docs_seen_and_touch(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    await repo.set_sdk_session(a.id, "sdk-9")
    await repo.set_docs_seen(a.id, {"docs/design/requirements.md": "v1"})
    await repo.touch(a.id)
    row = await repo.get(a.id)
    assert row is not None
    assert row.sdk_session_id == "sdk-9"
    assert row.docs_seen == {"docs/design/requirements.md": "v1"}
    assert row.last_message_at is not None and row.last_message_at.endswith("Z")


async def test_usage_totals_sums_runs_usage(db) -> None:
    repo, runs = ConversationRepo(db), RunRepo(db)
    a = await repo.create(PA, "free", None)
    b = await repo.create(PA, "free", None)
    await runs.start("r1", PA, a.id)
    await runs.finish("r1", "completed", {"inputTokens": 10, "outputTokens": 3, "costUsd": 0.5})
    await runs.start("r2", PA, a.id)
    await runs.finish("r2", "completed", {"inputTokens": 5, "outputTokens": 1})
    await runs.start("r3", PA, a.id)
    await runs.finish("r3", "interrupted", None)
    totals = await repo.usage_totals(PA)
    assert totals[a.id] == {"inputTokens": 15, "outputTokens": 4, "costUsd": 0.5}
    assert totals.get(b.id, {"inputTokens": 0, "outputTokens": 0, "costUsd": 0}) == {
        "inputTokens": 0,
        "outputTokens": 0,
        "costUsd": 0,
    }


async def test_usage_for_one_conversation(db) -> None:
    repo, runs = ConversationRepo(db), RunRepo(db)
    a = await repo.create(PA, "free", None)
    b = await repo.create(PA, "free", None)
    await runs.start("r1", PA, a.id)
    await runs.finish("r1", "completed", {"inputTokens": 7, "outputTokens": 2, "costUsd": 0.25})
    await runs.start("r2", PA, b.id)
    await runs.finish("r2", "completed", {"inputTokens": 1, "outputTokens": 1})
    assert await repo.usage_for(a.id) == {"inputTokens": 7, "outputTokens": 2, "costUsd": 0.25}
    assert await repo.usage_for("0" * 32) == {"inputTokens": 0, "outputTokens": 0, "costUsd": 0}
    assert (await repo.create(PA, "free", "Ünïcode ✓ title")).title == "Ünïcode ✓ title"


async def test_closing_lists_rows_across_projects(db) -> None:
    repo = ConversationRepo(db)
    a = await repo.create(PA, "free", None)
    b = await repo.create(PB, "free", None)
    await repo.create(PB, "free", None)
    await repo.set_status(a.id, "closing", expect="open")
    await repo.set_status(b.id, "closing", expect="open")
    assert sorted(r.id for r in await repo.closing()) == sorted([a.id, b.id])
