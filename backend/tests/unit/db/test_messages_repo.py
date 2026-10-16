import asyncio

import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.messages import MessageRepo

P = "a1b2c3d4e5f6"


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = await Database.open(tmp_path / "t.db")
    yield MessageRepo(db)
    await db.close()


async def test_insert_user_is_idempotent_on_client_message_id(repo):
    first, created1 = await repo.insert_user(P, "cmid-1", "hello")
    again, created2 = await repo.insert_user(P, "cmid-1", "hello (retransmit)")
    assert created1 is True and created2 is False
    assert again.id == first.id
    assert again.content == "hello"
    assert first.status == "queued" and first.role == "user" and first.kind == "text"
    rows, _ = await repo.page(P, limit=50)
    assert [r.id for r in rows] == [first.id]


async def test_concurrent_duplicate_inserts_yield_one_row(repo):
    results = await asyncio.gather(*(repo.insert_user(P, "same", "x") for _ in range(5)))
    assert len({row.id for row, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_same_client_id_in_other_project_is_independent(repo):
    a, _ = await repo.insert_user(P, "c", "x")
    b, created = await repo.insert_user("ffffffffffff", "c", "x")
    assert created and a.id != b.id


async def test_page_returns_ascending_and_has_more(repo):
    ids = [await repo.insert(P, role="assistant", kind="text", content=str(i)) for i in range(5)]
    rows, has_more = await repo.page(P, limit=2)
    assert [r.id for r in rows] == ids[3:]
    assert has_more is True
    older, has_more2 = await repo.page(P, limit=2, before=rows[0].id)
    assert [r.id for r in older] == ids[1:3]
    assert has_more2 is True
    oldest, has_more3 = await repo.page(P, limit=2, before=older[0].id)
    assert [r.id for r in oldest] == ids[:1]
    assert has_more3 is False


async def test_since_and_status_helpers(repo):
    m1, _ = await repo.insert_user(P, "a", "one")
    m2, _ = await repo.insert_user(P, "b", "two")
    assert [r.id for r in await repo.since(P, m1.id)] == [m2.id]
    assert await repo.ids_by_status(P, "queued") == [m1.id, m2.id]
    await repo.assign_run(m1.id, "run-1", "done")
    row = await repo.get(m1.id)
    assert row is not None and row.run_id == "run-1" and row.status == "done"
    assert [r.id for r in await repo.queued_user_rows(P)] == [m2.id]


async def test_metadata_merge_and_wire_shape(repo):
    mid = await repo.insert(P, role="assistant", kind="tool_use", metadata={"name": "x"})
    merged = await repo.merge_metadata(mid, {"input": {"a": 1}})
    assert merged == {"name": "x", "input": {"a": 1}}
    row = await repo.get(mid)
    assert row is not None
    wire = row.to_wire()
    assert set(wire) == {
        "id",
        "projectId",
        "clientMessageId",
        "runId",
        "role",
        "kind",
        "content",
        "status",
        "metadata",
        "createdAt",
    }
    assert wire["createdAt"].endswith("Z")


async def test_finalize_streaming_marks_leftovers_done(repo):
    mid = await repo.insert(P, role="assistant", kind="text", status="streaming", content="half")
    await repo.finalize_streaming(P)
    row = await repo.get(mid)
    assert row is not None and row.status == "done" and row.metadata.get("recovered") is True
