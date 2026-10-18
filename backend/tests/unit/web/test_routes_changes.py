"""Change log, diff and ERC result REST (E-C12, E8a Task 4) + the changes-updated frame."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copperchat.agent.protocol import ChangesUpdatedFrame, dump_frame
from copperchat.changes import diff as diff_mod
from copperchat.changes.erc import ErcDelta
from copperchat.changes.repo import ChangeEntry
from copperchat.config import Settings
from copperchat.editing import _publisher
from copperchat.projects import Project
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, RecordingBroadcaster, copy_project

GOOD = {"username": "admin", "password": "test-password"}
ROOT = "/aaaaaaaa-0000-0000-0000-000000000001"
U_WIRE = "bbbbbbbb-0000-0000-0000-000000000002"
U_MERGED = "cccccccc-0000-0000-0000-000000000003"


@dataclass
class Ctx:
    client: TestClient
    app: FastAPI
    project: Project

    @property
    def pid(self) -> str:
        return self.project.id

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return self.client.portal.call(fn, *args)  # type: ignore[union-attr]

    def entry(self, run_id: str, uuid: str, op: str = "add", **kw: Any) -> ChangeEntry:
        return ChangeEntry(
            project_id=self.pid,
            run_id=run_id,
            plan_step_id=kw.pop("step", None),
            call_seq=kw.pop("seq", 1),
            tool="sch_add_wire",
            op=op,
            kind=kw.pop("kind", "wire"),
            uuid=uuid,
            sheet_path=ROOT,
            **kw,
        )


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
            assert client.post("/api/auth/login", json=GOOD).status_code == 204
            yield Ctx(client, app, project)
    finally:
        server.release.set()


def test_runs_entries_and_erc_result(ctx: Ctx) -> None:
    c, editing = ctx.client, ctx.app.state.editing
    store = ctx.app.state.history.store(ctx.project)
    snap = ctx.call(store.snapshot, "run-1", "turn one")
    ctx.call(
        editing.changes.add_many,
        [
            ctx.entry("run-1", U_WIRE, after={"start": [0, 0]}),
            ctx.entry("run-1", U_MERGED, op="delete", side_effect=True),
            ctx.entry("run-2", U_WIRE, op="modify", step="st-1", ref=None, net="VCC"),
        ],
    )
    delta = ErcDelta(new_errors=[{"id": "abc", "type": "pin_not_connected"}])
    rid = ctx.call(editing.erc_results.add, ctx.pid, "run-1", None, snap.id, delta)

    runs = c.get(f"/api/projects/{ctx.pid}/changes/runs").json()
    assert [r["runId"] for r in runs] == ["run-2", "run-1"]
    run1 = runs[1]
    assert run1 == {
        "runId": "run-1",
        "stepIds": [],
        "count": 2,
        "sideEffects": 1,
        "erc": {"errors": 1, "warnings": 0, "resultId": rid},
        "snapshotId": snap.id,
        "createdAt": run1["createdAt"],
    }
    assert runs[0]["erc"] is None and runs[0]["snapshotId"] is None
    assert runs[0]["stepIds"] == ["st-1"]
    assert len(c.get(f"/api/projects/{ctx.pid}/changes/runs?limit=1").json()) == 1

    rows = c.get(f"/api/projects/{ctx.pid}/changes", params={"runId": "run-1"}).json()
    assert [r["uuid"] for r in rows] == [U_WIRE, U_MERGED]
    assert set(rows[0]) == {
        "id", "runId", "planStepId", "callSeq", "tool", "op", "kind", "uuid", "sheetPath",
        "ref", "net", "before", "after", "sideEffect", "createdAt",
    }  # fmt: skip
    assert rows[0]["after"] == {"start": [0, 0]} and rows[1]["sideEffect"] is True
    step = c.get(f"/api/projects/{ctx.pid}/changes", params={"runId": "run-2", "stepId": "st-1"})
    assert [r["net"] for r in step.json()] == ["VCC"]

    res = c.get(f"/api/projects/{ctx.pid}/erc-results/{rid}").json()
    assert res["newErrors"] == delta.new_errors and res["resolved"] == []
    assert res["justifications"] == {} and res["baselineSnapshotId"] == snap.id


def test_diff_route_shape_and_side_effects(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    c, editing = ctx.client, ctx.app.state.editing
    assert c.post(f"/api/projects/{ctx.pid}/open").status_code == 200
    store = ctx.app.state.history.store(ctx.project)
    snap = ctx.call(store.snapshot, "run-1", "turn one")
    ctx.call(editing.changes.add_many, [ctx.entry("run-1", U_WIRE)])
    raw = {
        "changes": [
            {
                "id": f"{ROOT}/{U_WIRE}",
                "kind": "added",
                "typeName": "SCH_LINE",
                "bbox": [10_000, 20_000, 30_000, 0],
                "properties": [{"name": "Start X", "after": {"type": "int", "v": 1}}],
            },
            {
                "id": f"{ROOT}/{U_MERGED}",
                "kind": "modified",
                "typeName": "SCH_LINE",
                "bbox": [0, 0, 1, 1],
                "properties": [
                    {"name": "End X", "before": {"v": 1}, "after": {"v": 2}},
                ],
            },
        ]
    }
    seen: list[str] = []

    async def fake_diff(svc: Any, project: Any, snapshot: Any) -> dict[str, Any]:
        seen.append(snapshot.id)
        return raw

    monkeypatch.setattr(diff_mod, "run_sch_diff", fake_diff)
    body = c.get(f"/api/projects/{ctx.pid}/changes/diff", params={"snapshotId": snap.id}).json()
    assert seen == [snap.id]
    assert body["snapshotId"] == snap.id and body["count"] == 2
    added, modified = body["sheets"][ROOT]
    assert added == {
        "id": f"{ROOT}/{U_WIRE}",
        "kind": "added",
        "type": "SCH_LINE",
        "uuid": U_WIRE,
        "ref": None,
        "boxMm": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 0.0},
        "properties": [{"name": "Start X"}],  # added/removed: names only
        "sideEffect": False,
    }
    assert modified["sideEffect"] is True  # no tool logged it: KiCad's cleanup
    assert modified["properties"] == [{"name": "End X", "before": {"v": 1}, "after": {"v": 2}}]
    # Unchanged design: the cached diff is reused (no second kicad-cli run) ...
    url = f"/api/projects/{ctx.pid}/changes/diff"
    assert c.get(url, params={"snapshotId": snap.id}).json() == body
    assert seen == [snap.id]
    # ... until an edit writes a change row.
    ctx.call(editing.changes.add_many, [ctx.entry("run-1", U_MERGED, op="modify")])
    again = c.get(url, params={"snapshotId": snap.id}).json()
    assert seen == [snap.id, snap.id]
    assert again["sheets"][ROOT][1]["sideEffect"] is False  # now logged


def test_diff_needs_the_open_project(ctx: Ctx) -> None:
    store = ctx.app.state.history.store(ctx.project)
    snap = ctx.call(store.snapshot, "run-1", "turn one")
    r = ctx.client.get(f"/api/projects/{ctx.pid}/changes/diff", params={"snapshotId": snap.id})
    assert r.status_code == 503


def test_unknown_snapshot_and_result_are_404(ctx: Ctx) -> None:
    c, pid = ctx.client, ctx.pid
    r = c.get(f"/api/projects/{pid}/changes/diff", params={"snapshotId": "0" * 32})
    assert r.status_code == 404
    assert c.get(f"/api/projects/{pid}/erc-results/{'0' * 32}").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/changes/diff?snapshotId=..%2Fetc",
        "/changes/diff?snapshotId=ZZZ",
        "/erc-results/not-hex",
        "/erc-results/" + "A" * 32,
        "/changes?runId=..%2F..",
        "/changes?runId=" + "x" * 101,
        "/changes?runId=run-1&stepId=a%20b",
        "/changes/runs?limit=0",
        "/changes/runs?limit=1000",
    ],
)
def test_bad_ids_are_400(ctx: Ctx, url: str) -> None:
    r = ctx.client.get(f"/api/projects/{ctx.pid}{url}")
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        assert r.json()["error"]["code"] in ("invalid_snapshot_id", "invalid_argument")


def test_missing_run_id_is_rejected(ctx: Ctx) -> None:
    assert ctx.client.get(f"/api/projects/{ctx.pid}/changes").status_code in (400, 422)


@pytest.mark.parametrize(
    "url", ["/changes/runs", "/changes?runId=r", "/changes/diff?snapshotId=" + "0" * 32,
            "/erc-results/" + "0" * 32]
)  # fmt: skip
def test_requires_login(ctx: Ctx, url: str) -> None:
    ctx.client.post("/api/auth/logout")
    assert ctx.client.get(f"/api/projects/{ctx.pid}{url}").status_code == 401


def test_editing_service_publishes_changes_updated(ctx: Ctx) -> None:
    assert len(ctx.app.state.editing.on_changes_recorded) == 1


async def test_publisher_frame() -> None:
    b = RecordingBroadcaster()
    await _publisher(b)("a1b2c3d4e5f6", "run-1", None, 3)
    [frame] = b.frames
    assert isinstance(frame, ChangesUpdatedFrame)
    assert dump_frame(frame) == (
        '{"type":"changes-updated","projectId":"a1b2c3d4e5f6","runId":"run-1",'
        '"stepId":null,"count":3}'
    )
