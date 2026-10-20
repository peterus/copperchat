"""Docs REST (C3 Task 5): every route, auth, path guard, body caps, route order, frames."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copperchat.agent.protocol import DocUpdatedFrame
from copperchat.config import Settings
from copperchat.projects import project_id_for
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer

GOOD = {"username": "admin", "password": "test-password"}
REQ = "docs/design/requirements.md"


@dataclass
class Ctx:
    client: TestClient
    app: FastAPI
    pid: str
    dir: Path

    def url(self, rest: str = "") -> str:
        return f"/api/projects/{self.pid}/docs" + rest

    def frames(self) -> list[Any]:
        return self.client.portal.call(self._drain)  # type: ignore[union-attr]

    async def _drain(self) -> list[Any]:
        out = []
        while True:
            try:
                out.append(await asyncio.wait_for(self.sub.__anext__(), 0.05))
            except TimeoutError:
                return out

    sub: Any = None


@pytest.fixture
def ctx(make_settings: Callable[..., Settings]) -> Iterator[Ctx]:
    server = FakeServer()
    settings = make_settings(dev_fake_agent=True)
    d = settings.projects_root / "demo"
    d.mkdir(parents=True)
    (d / "demo.kicad_pro").write_text("")
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        with TestClient(app) as client:
            assert client.post("/api/auth/login", json=GOOD).status_code == 204
            c = Ctx(client, app, project_id_for("demo/demo.kicad_pro"), d)
            c.sub = app.state.agent_runtime.broadcaster.subscribe(c.pid)
            yield c
    finally:
        server.release.set()


def put(ctx: Ctx, path: str, body: str, base: str | None) -> Any:
    return ctx.client.put(ctx.url(f"/{path}"), json={"body": body, "baseVersion": base})


ROUTES = [
    ("get", ""),
    ("get", "/requirements.md"),
    ("put", "/requirements.md"),
    ("post", "/requirements.md/approve"),
    ("post", "/requirements.md/unapprove"),
    ("post", "/requirements.md/clear-user-edited"),
    ("get", "/requirements.md/revisions"),
    ("get", "/requirements.md/revisions/1"),
    ("post", "/requirements.md/revisions/1/revert"),
]


@pytest.mark.parametrize(("method", "rest"), ROUTES)
def test_every_route_requires_a_session(ctx: Ctx, method: str, rest: str) -> None:
    ctx.client.post("/api/auth/logout")
    ctx.client.cookies.clear()
    res = getattr(ctx.client, method)(ctx.url(rest), **({} if method == "get" else {"json": {}}))
    assert res.status_code == 401


def test_full_flow(ctx: Ctx) -> None:
    c = ctx.client
    res = c.get(ctx.url("/requirements.md"))
    assert res.status_code == 200
    first = res.json()
    assert first["exists"] is False and first["version"] is None and "template" in first
    res = put(ctx, "requirements.md", "## Function\n\n## Open points\n\n- a\n", None)
    assert res.status_code == 200
    v1 = res.json()["version"]
    got = c.get(ctx.url(f"/{REQ}")).json()
    assert got["version"] == v1 and got["body"].startswith("## Function")
    assert got["meta"]["userEdited"] is True and got["meta"]["status"] == "draft"
    assert set(got) == {"path", "exists", "version", "meta", "body"}
    # approve / unapprove / clear-user-edited
    assert c.post(ctx.url("/requirements.md/approve"), json={"version": v1}).json() == {
        "version": v1
    }
    index = c.get(ctx.url()).json()
    entry = index["docs"][0]
    assert entry["approved"] and entry["approvedVersion"] == v1 and entry["openPoints"] == 1
    assert entry["lastAuthor"] == "user" and entry["lastConversationTitle"] is None
    assert entry["changedSinceApproval"] is False and index["ignored"] == []
    stale = c.post(ctx.url("/requirements.md/approve"), json={"version": "v0000000000000000"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "doc_conflict"
    assert c.post(ctx.url("/requirements.md/unapprove"), json={"version": v1}).status_code == 200
    res = c.post(ctx.url("/requirements.md/clear-user-edited"), json={"version": v1})
    assert res.status_code == 200
    assert c.get(ctx.url(f"/{REQ}")).json()["meta"]["userEdited"] is False
    # conflict body
    v2 = put(ctx, REQ, "new\n", v1).json()["version"]
    res = put(ctx, REQ, "mine\n", v1)
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "doc_conflict" and err["details"]["currentVersion"] == v2
    assert err["details"]["author"] == "user"
    # revisions, revision, revert
    revs = c.get(ctx.url("/requirements.md/revisions")).json()
    assert [r["author"] for r in revs] == ["user", "user"]
    assert set(revs[0]) == {
        "id",
        "version",
        "prevVersion",
        "author",
        "conversationId",
        "conversationTitle",
        "runId",
        "summary",
        "createdAt",
        "added",
        "removed",
    }
    rid_old = revs[-1]["id"]
    one = c.get(ctx.url(f"/requirements.md/revisions/{revs[0]['id']}")).json()
    assert "new" in one["content"] and "## Function" in one["prevContent"]
    assert c.get(ctx.url(f"/requirements.md/revisions/{rid_old}")).json()["prevContent"] is None
    res = c.post(ctx.url(f"/requirements.md/revisions/{rid_old}/revert"), json={"baseVersion": v2})
    assert res.status_code == 200 and res.json()["version"] == v1
    assert (
        c.get(ctx.url("/requirements.md/revisions?limit=1"))
        .json()[0]["summary"]
        .startswith("reverted")
    )
    frames = [f for f in ctx.frames() if isinstance(f, DocUpdatedFrame)]
    assert len(frames) >= 6
    assert all(f.author == "user" and f.conversation_id is None for f in frames)


def test_foreign_revision_id_is_404(ctx: Ctx) -> None:
    v = put(ctx, "requirements.md", "a\n", None).json()["version"]
    put(ctx, "parts.md", "p\n", None)
    rid = ctx.client.get(ctx.url("/parts.md/revisions")).json()[0]["id"]
    res = ctx.client.get(ctx.url(f"/requirements.md/revisions/{rid}"))
    assert res.status_code == 404
    res = ctx.client.post(
        ctx.url(f"/requirements.md/revisions/{rid}/revert"), json={"baseVersion": v}
    )
    assert res.status_code == 404
    assert ctx.client.get(ctx.url("/requirements.md/revisions/999999")).status_code == 404


def test_path_guard_through_the_router(ctx: Ctx, tmp_path: Path) -> None:
    design = ctx.dir / "docs" / "design"
    design.mkdir(parents=True)
    (design / "requirements.md").symlink_to("/proc/self/environ")
    for rest in (
        "/..%2F..%2Fdemo.kicad_pro",
        "/requirements.md",
        "/docs%2Fdesign%2F..%2Fx.md",
        "/notes.txt",
        "/A.md",
    ):
        res = ctx.client.get(ctx.url(rest))
        assert res.status_code == 400, rest
        body = res.text
        assert str(ctx.dir) not in body and "environ" not in body
        assert res.json()["error"]["code"] == "doc_path_refused"
    res = put(ctx, "requirements.md", "x", None)
    assert res.status_code == 400
    assert (design / "requirements.md").is_symlink()
    index = ctx.client.get(ctx.url()).json()
    assert {"path": REQ, "reason": "symlink"} in index["ignored"]


def test_route_order_suffix_routes_are_reachable(ctx: Ctx) -> None:
    put(ctx, "docs/design/notes/usb.md", "u\n", None)
    revs = ctx.client.get(ctx.url("/docs/design/notes/usb.md/revisions"))
    assert revs.status_code == 200 and isinstance(revs.json(), list) and revs.json()
    # a path ending in /revisions is never served as a doc
    res = ctx.client.get(ctx.url("/x.md/revisions"))
    assert res.status_code == 200 and res.json() == []
    for suffix in ("approve", "unapprove", "clear-user-edited"):
        res = ctx.client.post(ctx.url(f"/x.md/{suffix}"), json={"version": "v1"})
        assert res.status_code == 409, suffix  # reached the suffix route: conflict (missing doc)
    res = ctx.client.get(ctx.url("/docs/design/notes/usb.md"))
    assert res.json()["body"] == "u\n"


def test_body_cap_streaming_without_content_length(ctx: Ctx) -> None:
    big = b'{"body": "' + b"a" * (1024 * 1024 + 10) + b'", "baseVersion": null}'

    def chunks() -> Iterator[bytes]:
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]

    res = ctx.client.put(
        ctx.url("/requirements.md"),
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413
    res = ctx.client.put(
        ctx.url("/requirements.md"), content=big, headers={"content-type": "application/json"}
    )
    assert res.status_code == 413
    assert not (ctx.dir / "docs" / "design" / "requirements.md").exists()


def test_file_cap_on_bytes(ctx: Ctx) -> None:
    text = "ä" * 140_000  # 140k characters, 280 KB of UTF-8
    res = put(ctx, "requirements.md", text, None)
    assert res.status_code == 413 and res.json()["error"]["code"] == "doc_too_large"


def test_bad_bodies(ctx: Ctx) -> None:
    res = ctx.client.put(
        ctx.url("/requirements.md"),
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 400
    res = ctx.client.post(ctx.url("/requirements.md/approve"), json={"version": ""})
    assert res.status_code == 400
    res = ctx.client.post(ctx.url("/requirements.md/approve"), json={"version": "v", "x": 1})
    assert res.status_code == 400


def test_unknown_project_is_404(ctx: Ctx) -> None:
    assert ctx.client.get("/api/projects/ffffffffffff/docs").status_code == 404


def test_fifo_through_router_does_not_block(ctx: Ctx) -> None:
    design = ctx.dir / "docs" / "design"
    design.mkdir(parents=True)
    os.mkfifo(design / "parts.md")
    res = ctx.client.get(ctx.url("/parts.md"))
    assert res.status_code == 400
    index = ctx.client.get(ctx.url()).json()
    assert {"path": "docs/design/parts.md", "reason": "not a regular file"} in index["ignored"]


def test_doc_under_revisions_dir_and_huge_rid(ctx: Ctx) -> None:
    assert put(ctx, "docs/design/revisions/x.md", "r\n", None).status_code == 200
    res = ctx.client.get(ctx.url("/docs/design/revisions/x.md"))
    assert res.status_code == 200 and res.json()["body"] == "r\n"
    res = ctx.client.get(ctx.url(f"/requirements.md/revisions/{10**30}"))
    assert res.status_code == 404
