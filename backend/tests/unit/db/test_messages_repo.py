import asyncio

import pytest
import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.db.repos.messages import MessageRepo

P = "a1b2c3d4e5f6"
OTHER = "ffffffffffff"


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


@pytest_asyncio.fixture
async def repo(db):
    return MessageRepo(db)


@pytest_asyncio.fixture
async def ca(db) -> str:
    return (await ConversationRepo(db).create(P, "free", None)).id


@pytest_asyncio.fixture
async def cb(db) -> str:
    return (await ConversationRepo(db).create(P, "free", None)).id


async def test_insert_user_is_idempotent_on_client_message_id(repo, ca):
    first, created1 = await repo.insert_user(P, ca, "cmid-1", "hello")
    again, created2 = await repo.insert_user(P, ca, "cmid-1", "hello (retransmit)")
    assert created1 is True and created2 is False
    assert again.id == first.id
    assert again.content == "hello"
    assert first.status == "queued" and first.role == "user" and first.kind == "text"
    assert first.conversation_id == ca
    rows, _ = await repo.page(P, ca, limit=50)
    assert [r.id for r in rows] == [first.id]


async def test_concurrent_duplicate_inserts_yield_one_row(repo, ca):
    results = await asyncio.gather(*(repo.insert_user(P, ca, "same", "x") for _ in range(5)))
    assert len({row.id for row, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_same_client_id_in_other_project_is_independent(repo, db, ca):
    other = (await ConversationRepo(db).create(OTHER, "free", None)).id
    a, _ = await repo.insert_user(P, ca, "c", "x")
    b, created = await repo.insert_user(OTHER, other, "c", "x")
    assert created and a.id != b.id


async def test_page_returns_ascending_and_has_more(repo, ca):
    ids = [
        await repo.insert(P, ca, role="assistant", kind="text", content=str(i)) for i in range(5)
    ]
    rows, has_more = await repo.page(P, ca, limit=2)
    assert [r.id for r in rows] == ids[3:]
    assert has_more is True
    older, has_more2 = await repo.page(P, ca, limit=2, before=rows[0].id)
    assert [r.id for r in older] == ids[1:3]
    assert has_more2 is True
    oldest, has_more3 = await repo.page(P, ca, limit=2, before=older[0].id)
    assert [r.id for r in oldest] == ids[:1]
    assert has_more3 is False


async def test_page_and_since_are_scoped_to_the_conversation(repo, ca, cb):
    a1 = await repo.insert(P, ca, role="assistant", kind="text", content="a1")
    b1 = await repo.insert(P, cb, role="assistant", kind="text", content="b1")
    a2 = await repo.insert(P, ca, role="assistant", kind="text", content="a2")
    b2, _ = await repo.insert_user(P, cb, "b2", "b2")
    rows, more = await repo.page(P, ca, limit=10)
    assert [r.id for r in rows] == [a1, a2] and more is False
    rows, _ = await repo.page(P, cb, limit=10)
    assert [r.id for r in rows] == [b1, b2.id]
    assert [r.id for r in await repo.since(P, ca, a1)] == [a2]
    assert [r.id for r in await repo.since(P, cb, 0)] == [b1, b2.id]
    # The project filter stays as defence: a foreign project sees nothing.
    assert await repo.since(OTHER, ca, 0) == []
    assert (await repo.page(OTHER, ca, limit=10))[0] == []


async def test_queued_user_rows_span_the_project_in_id_order(repo, db, ca, cb):
    b1, _ = await repo.insert_user(P, cb, "b1", "b1")
    a1, _ = await repo.insert_user(P, ca, "a1", "a1")
    b2, _ = await repo.insert_user(P, cb, "b2", "b2")
    other = (await ConversationRepo(db).create(OTHER, "free", None)).id
    await repo.insert_user(OTHER, other, "x", "x")
    rows = await repo.queued_user_rows(P)
    assert [(r.id, r.conversation_id) for r in rows] == [(b1.id, cb), (a1.id, ca), (b2.id, cb)]


async def test_insert_touches_the_conversation(repo, db, ca, cb):
    convs = ConversationRepo(db)
    assert (await convs.get(ca)).last_message_at is None  # type: ignore[union-attr]
    await repo.insert(P, ca, role="system", kind="notice", content="n")
    after = await convs.get(ca)
    assert after is not None and after.last_message_at is not None
    assert (await convs.get(cb)).last_message_at is None  # type: ignore[union-attr]
    await repo.insert_user(P, cb, "u", "u")
    assert (await convs.get(cb)).last_message_at is not None  # type: ignore[union-attr]


async def test_insert_needs_an_existing_conversation(repo):
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.IntegrityError (foreign key)
        await repo.insert(P, "0" * 32, role="system", kind="notice", content="n")


async def test_run_repo_stores_the_conversation(db, ca):
    await RunRepo(db).start("r1", P, ca)
    run = await RunRepo(db).get("r1")
    assert run is not None and run["conversation_id"] == ca


async def test_since_and_status_helpers(repo, ca):
    m1, _ = await repo.insert_user(P, ca, "a", "one")
    m2, _ = await repo.insert_user(P, ca, "b", "two")
    assert [r.id for r in await repo.since(P, ca, m1.id)] == [m2.id]
    assert await repo.ids_by_status(P, "queued") == [m1.id, m2.id]
    await repo.assign_run(m1.id, "run-1", "done")
    row = await repo.get(m1.id)
    assert row is not None and row.run_id == "run-1" and row.status == "done"
    assert [r.id for r in await repo.queued_user_rows(P)] == [m2.id]


async def test_metadata_merge_and_wire_shape(repo, ca):
    mid = await repo.insert(P, ca, role="assistant", kind="tool_use", metadata={"name": "x"})
    merged = await repo.merge_metadata(mid, {"input": {"a": 1}})
    assert merged == {"name": "x", "input": {"a": 1}}
    row = await repo.get(mid)
    assert row is not None
    wire = row.to_wire()
    assert set(wire) == {
        "id",
        "projectId",
        "conversationId",
        "clientMessageId",
        "runId",
        "role",
        "kind",
        "content",
        "status",
        "metadata",
        "createdAt",
    }
    assert wire["createdAt"].endswith("Z") and wire["conversationId"] == ca


async def test_finalize_streaming_marks_leftovers_done(repo, ca):
    mid = await repo.insert(
        P, ca, role="assistant", kind="text", status="streaming", content="half"
    )
    await repo.finalize_streaming(P)
    row = await repo.get(mid)
    assert row is not None and row.status == "done" and row.metadata.get("recovered") is True


def test_utcnow_is_exported_with_z_suffix():
    from copperchat.db.repos.messages import utcnow

    assert utcnow().endswith("Z")
    assert len(utcnow()) == len("2026-10-15T12:00:00.000Z")
