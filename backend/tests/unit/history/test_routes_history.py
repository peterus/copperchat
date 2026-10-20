import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copperchat.agent.engine import ChainedHooks
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.agent_state import RateLimitRepo, RunRepo
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.editing.hooks import EditingHooks
from copperchat.history.hooks import HistoryHooks
from copperchat.history.store import sha256_file
from copperchat.projects import Project
from copperchat.sessions import SessionHooks
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, copy_project, other_board, replace_with_kicad_file

GOOD = {"username": "admin", "password": "test-password"}


@dataclass
class Ctx:
    client: TestClient
    app: FastAPI
    project: Project
    cid: str = ""

    def snapshot(self, label: str = "turn one", preview: bytes | None = b"\x89PNG") -> Any:
        store = self.app.state.history.store(self.project)
        return self.client.portal.call(store.snapshot, "run-1", label, preview)  # type: ignore[union-attr]


@pytest.fixture
def ctx(make_settings: Callable[..., Settings], tmp_path: Path) -> Iterator[Ctx]:
    server = FakeServer()
    settings = make_settings(dev_fake_agent=True)
    project = copy_project(ECC83, settings.projects_root)
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        with TestClient(app) as client:

            async def write_view(session: Any, doc: str) -> Path:
                return tmp_path / doc

            app.state.history.write_view = write_view
            assert client.post("/api/auth/login", json=GOOD).status_code == 204
            r = client.post(f"/api/projects/{project.id}/conversations", json={"type": "free"})
            yield Ctx(client, app, project, r.json()["id"])
    finally:
        server.release.set()


def test_list_restore_and_preview(ctx: Ctx) -> None:
    c, pid = ctx.client, ctx.project.id
    assert c.post(f"/api/projects/{pid}/open").status_code == 200
    snap = ctx.snapshot()
    no_preview = ctx.snapshot("second", None)
    listed = c.get(f"/api/projects/{pid}/history").json()
    assert [e["id"] for e in listed] == [no_preview.id, snap.id]
    assert listed[1]["label"] == "turn one" and listed[1]["runId"] == "run-1"
    assert listed[1]["previewUrl"] == f"/api/projects/{pid}/history/{snap.id}/preview"
    assert listed[0]["previewUrl"] is None
    prev = c.get(listed[1]["previewUrl"])
    assert prev.status_code == 200 and prev.headers["content-type"] == "image/png"
    assert c.get(f"/api/projects/{pid}/history/{no_preview.id}/preview").status_code == 404
    res = c.post(f"/api/projects/{pid}/history/{snap.id}/restore", json={"conversationId": ctx.cid})
    assert res.status_code == 200 and res.json() == {"id": snap.id, "files": snap.files}


def test_history_names_the_session_of_each_snapshot(ctx: Ctx) -> None:
    c, pid = ctx.client, ctx.project.id
    assert c.post(f"/api/projects/{pid}/open").status_code == 200
    ctx.client.portal.call(RunRepo(ctx.app.state.db).start, "run-1", pid, ctx.cid)  # type: ignore[union-attr]
    snap = ctx.snapshot()
    store = ctx.app.state.history.store(ctx.project)
    orphan = ctx.client.portal.call(store.snapshot, "run-x", "no run row", None)  # type: ignore[union-attr]
    listed = {e["id"]: e for e in c.get(f"/api/projects/{pid}/history").json()}
    assert (listed[snap.id]["conversationId"], listed[snap.id]["conversationTitle"]) == (
        ctx.cid,
        "Session 1",
    )
    assert (listed[orphan.id]["conversationId"], listed[orphan.id]["conversationTitle"]) == (
        None,
        None,
    )


def test_restore_with_a_foreign_session_is_404_and_restores_nothing(ctx: Ctx) -> None:
    c, project = ctx.client, ctx.project
    assert c.post(f"/api/projects/{project.id}/open").status_code == 200
    snap = ctx.snapshot()
    assert project.pcb_file is not None
    replace_with_kicad_file(project.pcb_file, other_board())
    changed = sha256_file(project.pcb_file)
    other = c.post("/api/projects/ffffffffffff/conversations", json={"type": "free"})
    assert other.status_code == 404  # unknown project: create a foreign row directly
    repo = ConversationRepo(ctx.app.state.db)
    foreign = ctx.client.portal.call(repo.create, "ffffffffffff", "free", None)  # type: ignore[union-attr]
    url = f"/api/projects/{project.id}/history/{snap.id}/restore"
    for body in (
        {"conversationId": foreign.id},
        {"conversationId": "0" * 32},
        {"conversationId": "x"},
    ):
        r = c.post(url, json=body)
        assert r.status_code == 404 and r.json()["error"]["code"] == "conversation_not_found"
    assert c.post(url).status_code == 422  # the body is required
    assert c.post(url, json={}).status_code == 422
    assert sha256_file(project.pcb_file) == changed, "nothing was restored"


@pytest.mark.parametrize("bad", ["ZZZ", "0" * 31, "A" * 32, "%2E%2E"])
def test_invalid_ids_are_400(ctx: Ctx, bad: str) -> None:
    pid = ctx.project.id
    r = ctx.client.post(
        f"/api/projects/{pid}/history/{bad}/restore", json={"conversationId": ctx.cid}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_snapshot_id"
    assert ctx.client.get(f"/api/projects/{pid}/history/{bad}/preview").status_code == 400


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "%2Fetc%2Fpasswd", "../../etc", ".."])
def test_traversal_ids_never_reach_the_filesystem(ctx: Ctx, bad: str) -> None:
    pid = ctx.project.id
    assert ctx.client.post(
        f"/api/projects/{pid}/history/{bad}/restore", json={"conversationId": ctx.cid}
    ).status_code in (400, 404)
    assert ctx.client.get(f"/api/projects/{pid}/history/{bad}/preview").status_code in (400, 404)


def test_unknown_id_is_404_and_locked_is_409(ctx: Ctx) -> None:
    project, c = ctx.project, ctx.client
    r = c.post(
        f"/api/projects/{project.id}/history/{'0' * 32}/restore", json={"conversationId": ctx.cid}
    )
    assert r.status_code == 404
    snap = ctx.snapshot()
    assert project.pcb_file is not None
    (project.dir / f"~{project.pcb_file.name}.lck").touch()
    r = c.post(
        f"/api/projects/{project.id}/history/{snap.id}/restore", json={"conversationId": ctx.cid}
    )
    assert r.status_code == 409 and "open in KiCad" in r.json()["error"]["message"]


def test_bad_project_id(ctx: Ctx) -> None:
    assert ctx.client.get("/api/projects/nothex/history").status_code == 400
    assert ctx.client.get(f"/api/projects/{'a' * 12}/history").status_code == 404


def test_requires_login(ctx: Ctx) -> None:
    ctx.client.cookies.clear()
    pid = ctx.project.id
    assert ctx.client.get(f"/api/projects/{pid}/history").status_code == 401
    assert (
        ctx.client.post(
            f"/api/projects/{pid}/history/{'0' * 32}/restore", json={"conversationId": ctx.cid}
        ).status_code
        == 401
    )
    assert ctx.client.get(f"/api/projects/{pid}/git/status").status_code == 401


def test_hooks_are_installed_before_resume_pending(
    make_settings: Callable[..., Settings],
) -> None:
    """Engines created by resume_pending() at startup must get the history hooks, chained
    with the editing hooks (E-C5), and the editing service."""
    server = FakeServer()
    settings = make_settings(dev_fake_agent=True)
    project = copy_project(ECC83, settings.projects_root)

    async def park() -> None:
        db = await Database.open(settings.data_dir / "copperchat.db")
        await RateLimitRepo(db).set(project.id, datetime.now(UTC) + timedelta(hours=1))
        await db.close()

    asyncio.run(park())
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        with TestClient(app):
            engine = app.state.agent_runtime.get(project.id)
            assert engine is not None and isinstance(engine.hooks, ChainedHooks)
            history, editing, sessions = engine.hooks.hooks
            assert isinstance(history, HistoryHooks) and isinstance(editing, EditingHooks)
            assert isinstance(sessions, SessionHooks)  # sessions C5, chained last
            assert engine.editing is app.state.editing is app.state.agent_runtime.editing
            assert engine.docs is app.state.docs is app.state.agent_runtime.docs
    finally:
        server.release.set()


def test_history_route_lists_kind(ctx: Ctx) -> None:
    c, pid = ctx.client, ctx.project.id
    turn_snap = ctx.snapshot()
    store = ctx.app.state.history.store(ctx.project)
    step = ctx.client.portal.call(  # type: ignore[union-attr]
        lambda: store.snapshot("run-1", "Step 1", None, kind="step", plan_step_id="st-1")
    )
    listed = {e["id"]: e for e in c.get(f"/api/projects/{pid}/history").json()}
    assert (listed[turn_snap.id]["kind"], listed[turn_snap.id]["planStepId"]) == ("turn", None)
    assert (listed[step.id]["kind"], listed[step.id]["planStepId"]) == ("step", "st-1")
