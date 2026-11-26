"""Board (SP4) tests against a real KiCad api-server."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from copperchat.board.freerouting import FreeroutingRuntime, find_runtime
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import Project, discover_projects, get_project
from copperchat.tools.spec import ToolContext
from tests.support.edit import edit_ctx
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import copy_fixture, real_settings


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ecc83_module_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[KicadSession]:
    """One ecc83 api-server for a whole test module (tests that only read, or whose
    changes the later tests of the module tolerate). Tests using it run on the module
    event loop: `pytest.mark.asyncio(loop_scope="module")`."""
    tmp: Path = tmp_path_factory.mktemp("ecc83-module")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("ecc83", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "ecc83-pp")
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


# -- board_ctx (SP4 P1a Task 7; reused by later phases) -----------------------------------


class _OneSession:
    """The fixture's session as the 'current' one of a HistoryService (no reopen)."""

    def __init__(self, session: KicadSession) -> None:
        self.session = session

    def current(self) -> KicadSession:
        return self.session

    def add_save_listener(self, fn: Any) -> None:
        return None


async def _no_preview(session: Any, doc: Any) -> None:
    return None


def board_ctx(
    session: KicadSession,
    db: Database,
    sessions: KicadSessionManager | None = None,
) -> ToolContext:
    """A board tool context as `RunEngine._make_ctx` builds it: `edit_ctx` (editing
    service, run id) plus a real `HistoryService`; `on_mutation` takes the turn
    snapshot, and - with the `sessions` manager that opened `session` - `reload_project`
    re-discovers the project (`get_project`) and reopens it (`sessions.reopen`),
    returning the new session's generation read at once. Without `sessions` there is no
    reload (board creation then refuses)."""
    svc = HistoryService(
        db=db,
        sessions=cast(Any, sessions if sessions is not None else _OneSession(session)),
        broadcaster=None,
        runtime=None,
    )
    svc.render_preview = _no_preview  # type: ignore[assignment]
    root = session.settings.projects_root
    svc.projects_root = root
    ctx = edit_ctx(session, db)

    async def on_mutation() -> None:
        await svc.store(ctx.project).snapshot(ctx.run_id or "run", "turn")

    ctx.on_mutation = on_mutation
    if sessions is not None:

        async def reload_project() -> tuple[Project, KicadSession, int]:
            fresh = get_project(root, ctx.project.id)
            new = await sessions.reopen(fresh)
            return fresh, new, new.generation

        ctx.reload_project = reload_project
    return ctx


@pytest_asyncio.fixture
async def board_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.open(tmp_path / "board.db")
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def blank_nopcb_sessions(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[KicadSessionManager]:
    """The blank project copied without its .kicad_pcb, opened through a manager that
    `board_ctx(..., sessions=)` can reopen with (the manager's `current()` is the
    session, as `blank_nopcb_session` would give it)."""
    settings = real_settings(make_settings)
    dest = copy_fixture("blank", settings.projects_root)
    for pcb in dest.glob("*.kicad_pcb"):
        pcb.unlink()
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "blank")
    manager = KicadSessionManager(settings)
    try:
        await manager.open(project)
        yield manager
    finally:
        await manager.close_all()


# -- synth_board (SP4 P3a; reused by later phases) -----------------------------------------


def _build_synth_board(projects_root: Path) -> Path:
    """`sp4_synth` copied to `projects_root` with a board made by KiCad (P1b's
    `test_netlist_imports_cleanly`, not dry): kicad-cli netlist → CreateDocument(PCB) +
    save → reopen → `import_netlist(dry_run=False)` → save. API only; footprints stay where
    KiCad puts them. Returns the project directory."""
    import subprocess

    from kipy.proto.common.types import DocumentType

    from tests.kicad.board.test_sp4_synth_build import NAME, standalone_server

    work = copy_fixture(NAME, projects_root)
    netlist = projects_root.parent / f"{NAME}.net"
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(netlist),
         str(work / f"{NAME}.kicad_sch")],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    pcb = work / f"{NAME}.kicad_pcb"
    with standalone_server(KICAD_CLI) as client:
        client.create_document(str(pcb), DocumentType.DOCTYPE_PCB)
        client.get_board().save()
        for doc in client.get_open_documents(DocumentType.DOCTYPE_PCB):
            client.close_document(doc)
        client.open_document(str(pcb), DocumentType.DOCTYPE_PCB)
        board = client.get_board()
        result = board.import_netlist(str(netlist), dry_run=False)
        assert result.error_count == 0, result.report
        board.save()
    return work


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_board(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[KicadSession]:
    """The `sp4_synth` project with its netlist imported into a new board
    (`_build_synth_board`), opened as a normal CopperChat session (project with `pcb_file`)
    for a whole test module. Tests run on the module event loop
    (`pytest.mark.asyncio(loop_scope="module")`) and build their context per test:
    `edit_ctx(synth_board)` (read tools) or `board_ctx(synth_board, db)` (writes)."""
    tmp: Path = tmp_path_factory.mktemp("synth-board")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
    )
    settings.projects_root.mkdir(parents=True)
    _build_synth_board(settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    assert project.pcb_file is not None
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


# -- synth_rig (SP4 P2; sync / outline / rules on sp4_synth) ------------------------------


@dataclass
class SynthRig:
    """`sp4_synth` (schematic only) opened through a manager, with board tools on, a
    database with the test conversation and a HistoryService that can restore and
    reopen (`restore_turn`). `ctx()` builds a fresh board tool context on the current
    session (`board_ctx` with `sessions=`, so board creation reloads)."""

    sessions: KicadSessionManager
    db: Database
    history: HistoryService
    engine: Any
    settings: Settings

    @property
    def session(self) -> KicadSession:
        current = self.sessions.current()
        assert current is not None
        return current

    def ctx(self) -> ToolContext:
        return board_ctx(self.session, self.db, self.sessions)

    async def restore_turn(self, ctx: ToolContext) -> None:
        """History restore of the turn snapshot `ctx.on_mutation` took."""
        from copperchat.history.restore import restore_snapshot
        from tests.support.conversations import TEST_CID

        project = self.session.project
        snap = await self.history.store(project).latest_for_run(ctx.run_id or "run")
        assert snap is not None, "no turn snapshot"
        await restore_snapshot(self.history, project, snap.id, conversation_id=TEST_CID)


@pytest_asyncio.fixture
async def synth_rig(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> AsyncIterator[SynthRig]:
    from tests.support.conversations import ensure_test_conversation
    from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster

    settings = make_settings(
        kicad_cli=KICAD_CLI, kicad_api_timeout_s=30.0, kicad_start_timeout_s=60.0, board_tools=True
    )
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    assert project.pcb_file is None
    manager = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "synth.db")
    await ensure_test_conversation(db)
    engine = FakeEngine()
    svc = HistoryService(
        db=db, sessions=manager, broadcaster=RecordingBroadcaster(), runtime=FakeRuntime(engine)
    )
    svc.projects_root = settings.projects_root
    try:
        await manager.open(project)
        yield SynthRig(manager, db, svc, engine, settings)
    finally:
        await manager.close_all()
        await db.close()


# -- freerouting_rt (SP4 P6a; reused by P6b and P9a) --------------------------------------


@pytest.fixture(scope="session")
def freerouting_rt() -> FreeroutingRuntime:
    """The fetched Freerouting runtime (`scripts/fetch-freerouting.sh`). Missing → the
    test fails in CI (the kicad job fetches it) and skips locally."""
    rt = find_runtime(Settings(_env_file=None))  # type: ignore[call-arg]
    if rt is not None:
        return rt
    if os.environ.get("CI") == "true":
        pytest.fail("Freerouting runtime missing in CI: run scripts/fetch-freerouting.sh")
    home = Settings(_env_file=None).freerouting_home  # type: ignore[call-arg]
    pytest.skip(f"Freerouting runtime missing locally (COPPERCHAT_FREEROUTING_HOME={home})")


# -- synth_setup (SP4 P3b; checks, checkpoints and approvals on a set-up sp4_synth) -------

SETUP_OUTLINE: dict[str, Any] = {
    "shape": "rect",
    "width": 60,
    "height": 45,
    "corner_radius": 1,
    "holes": {"inset": 3},
}
SETUP_RULES: dict[str, Any] = {"layers": 4, "preset": "jlcpcb-4l"}


@dataclass
class SynthSetup:
    """`sp4_synth` with its board set up through the real two-phase adapter
    (`pcb_sync_from_schematic`, `pcb_set_outline` SETUP_OUTLINE, `pcb_set_rules`
    SETUP_RULES) for a whole test module, board tools on. The board persists across the
    module's tests; each test brings its own database (`turn(db, run_id)`)."""

    sessions: KicadSessionManager
    settings: Settings

    @property
    def session(self) -> KicadSession:
        current = self.sessions.current()
        assert current is not None
        return current

    def turn(self, db: Database, run_id: str) -> ToolContext:
        """The run engine's context of turn `run_id` in the test conversation (a fresh
        EditingService: the turn's budget starts at 0)."""
        import dataclasses

        from tests.support.conversations import TEST_CID

        ctx = board_ctx(self.session, db, self.sessions)
        return dataclasses.replace(ctx, run_id=run_id, conversation_id=TEST_CID)


async def setup_call(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    """One tool call through the real adapter (turn snapshot first, as the run engine
    takes it once per turn); fails the test on an error payload."""
    import json

    from tests.support.edit import call_tool

    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def open_setup_db(path: Path, project_id: str) -> Database:
    from tests.support.conversations import ensure_test_conversation

    db = await Database.open(path)
    await ensure_test_conversation(db, project_id)
    return db


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_setup(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[SynthSetup]:
    tmp: Path = tmp_path_factory.mktemp("synth-setup")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
        board_tools=True,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    manager = KicadSessionManager(settings)
    db = await open_setup_db(tmp / "setup.db", project.id)
    try:
        await manager.open(project)
        rig = SynthSetup(manager, settings)
        await setup_call(rig.turn(db, "run-setup-1"), "pcb_sync_from_schematic")
        await setup_call(rig.turn(db, "run-setup-2"), "pcb_set_outline", **SETUP_OUTLINE)
        await setup_call(rig.turn(db, "run-setup-3"), "pcb_set_rules", **SETUP_RULES)
        yield rig
    finally:
        await manager.close_all()
        await db.close()


@pytest_asyncio.fixture(loop_scope="module")
async def setup_db(synth_setup: SynthSetup, tmp_path: Path) -> AsyncIterator[Database]:
    """A fresh database (with the test conversation) for one `synth_setup` test."""
    db = await open_setup_db(tmp_path / "p3b.db", synth_setup.session.project.id)
    try:
        yield db
    finally:
        await db.close()


async def user_says(db: Database, project_id: str, run_id: str, text: str) -> None:
    """The message that starts turn `run_id`, typed by the user (stored with
    `metadata.origin = "user"`, as the WebSocket submit stores it; later than every
    render and check before it: the clock has millisecond resolution)."""
    import asyncio

    from copperchat.db.repos.messages import MessageRepo
    from tests.support.conversations import TEST_CID

    await asyncio.sleep(0.01)
    await MessageRepo(db).insert(
        project_id,
        TEST_CID,
        role="user",
        kind="text",
        content=text,
        run_id=run_id,
        metadata={"origin": "user"},
    )
    await asyncio.sleep(0.01)


# -- block helpers (SP4 P4; reused by place_synth and P5b's rigs) --------------------------

NO_BLOCK_CLASSES = frozenset({"mounting_hole", "fiducial"})


async def analyzed_blocks(ctx: ToolContext) -> list[dict[str, Any]]:
    """`pcb_analyze`'s block proposals as `pcb_define_blocks` blocks: mounting holes and
    fiducials dropped (they cannot be in a block), blocks left empty dropped."""
    out = await setup_call(ctx, "pcb_analyze", scope="all")
    classes: dict[str, str] = out["parts"]
    blocks = []
    for b in out["blocks"]:
        refs = [r for r in b["refs"] if classes.get(r) not in NO_BLOCK_CLASSES]
        if not refs:
            continue
        block: dict[str, Any] = {"name": b["name"], "refs": refs}
        if b["anchor"] in refs:
            block["anchor"] = b["anchor"]
        blocks.append(block)
    return blocks


# -- place_synth (SP4 P4; P5b's placed_synth / routed_synth build on it) -----------------


SYNTH_CHANNELS_MM = 1.5  # routing room between the blocks (the board has space for it)


async def place_synth(rig: SynthSetup, db: Database, run: str = "run-place") -> dict[str, Any]:
    """`sp4_synth` placed block by block through the real tools (needs `synth_setup`'s
    outline + rules): `pcb_analyze` proposals → `pcb_define_blocks` (the ESD part's own
    block joins the USB connector's block, as the board-layout skill tells the agent:
    protection next to its connector) → `pcb_place_block` per block with a fixed
    floorplan: the connector block on `edge: left`, the MCU block in the right half, the
    power block in the left half, any other block anywhere. Returns
    `{blocks, results: {block: tool data}, seconds}`."""
    import time

    o = (await setup_call(rig.turn(db, f"{run}-0"), "pcb_status"))["outline"]
    blocks = await analyzed_blocks(rig.turn(db, f"{run}-1"))
    by_anchor = {b.get("anchor"): b for b in blocks}
    esd, usb = by_anchor.get("U2"), by_anchor.get("J1")
    if esd is not None and usb is not None:
        usb["refs"] = usb["refs"] + esd["refs"]
        blocks.remove(esd)
    await setup_call(rig.turn(db, f"{run}-2"), "pcb_define_blocks", blocks=blocks, replace=True)
    half = o["w"] / 2

    def region(b: dict[str, Any]) -> dict[str, Any]:
        anchor = b.get("anchor")
        if anchor == "J1":
            return {"edge": "left"}
        if anchor == "U1":
            return {"x": o["x"] + half, "y": o["y"], "w": half, "h": o["h"]}
        if anchor == "U3":
            return {"x": o["x"], "y": o["y"], "w": half, "h": o["h"]}
        return {"x": o["x"], "y": o["y"], "w": o["w"], "h": o["h"]}

    order = {"J1": 0, "U1": 1, "U3": 2}
    results: dict[str, Any] = {}
    start = time.monotonic()
    for i, b in enumerate(sorted(blocks, key=lambda b: (order.get(b.get("anchor"), 9), b["name"]))):
        results[b["name"]] = await setup_call(
            rig.turn(db, f"{run}-p{i}"),
            "pcb_place_block",
            block=b["name"],
            region=region(b),
            keep_channels=SYNTH_CHANNELS_MM,
        )
    return {"blocks": blocks, "results": results, "seconds": time.monotonic() - start}


# -- placed_synth / routed_synth (SP4 P5b; P6b, P7, P8 and P9a build on them) -------------

PLACED_RULES: dict[str, Any] = {
    "layers": 4,  # explicit: a preset alone never changes the layer count (P2)
    "preset": "jlcpcb-4l",
    "net_classes": [
        {
            "name": "Power",
            "track_width": 0.4,
            "clearance": 0.2,
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "patterns": ["*VBUS", "*+3V3", "*SW"],
        }
    ],
}
PLACEMENT_OK = "placement looks good, go ahead"
PLACEMENT_QUOTE = "placement looks good"
ROUTE_CHUNK = 50  # pcb_route takes at most 50 nets per call


@dataclass
class PlacedSynth:
    """`sp4_synth` set up, placed and placement-approved (`placed_synth`) on its own
    api-server for a whole test module; `db` holds the approval (later tools check it),
    `info` is `placed_synth`'s result. `turn(run_id)` = a fresh turn context on `db`."""

    rig: SynthSetup
    db: Database
    info: dict[str, Any]

    @property
    def session(self) -> KicadSession:
        return self.rig.session

    def turn(self, run_id: str) -> ToolContext:
        return self.rig.turn(self.db, run_id)


async def placed_synth(rig: SynthSetup, db: Database, run: str = "run-placed") -> dict[str, Any]:
    """The one setup sequence of the later board phases, through the real two-phase
    adapter: (1) `pcb_sync_from_schematic`; (2) `pcb_set_outline` SETUP_OUTLINE; (3)
    `pcb_set_rules` PLACED_RULES (4 layers, `jlcpcb-4l`, the Power class); (4)
    `place_synth`; (5) `pcb_legalize {scope: all}`; (6) turn 1: `pcb_render view=placement`
    + `pcb_check quick` (M1 must pass); (7) turn 2: the user's OK + `pcb_approve
    {stage: placement}` — the placement checkpoint exists before any routing. Returns
    `{placement_hash, critical_nets}`."""
    await setup_call(rig.turn(db, f"{run}-sync"), "pcb_sync_from_schematic")
    await setup_call(rig.turn(db, f"{run}-outline"), "pcb_set_outline", **SETUP_OUTLINE)
    await setup_call(rig.turn(db, f"{run}-rules"), "pcb_set_rules", **PLACED_RULES)
    await place_synth(rig, db, f"{run}-place")
    await setup_call(rig.turn(db, f"{run}-legalize"), "pcb_legalize", scope="all")
    show = rig.turn(db, f"{run}-show")
    render = await setup_call(show, "pcb_render", view="placement")
    check = await setup_call(show, "pcb_check", level="quick")
    assert render["placement_hash"] == check["placement_hash"]
    m1 = next(m for m in check["metrics"] if m["id"] == "M1")
    assert m1["verdict"] == "pass", m1
    digest: str = check["placement_hash"]
    await user_says(db, rig.session.project.id, f"{run}-ok", PLACEMENT_OK)
    await setup_call(
        rig.turn(db, f"{run}-ok"),
        "pcb_approve",
        stage="placement",
        hash=digest,
        user_quote=PLACEMENT_QUOTE,
    )
    analysis = await setup_call(rig.turn(db, f"{run}-analyze"), "pcb_analyze", scope="nets")
    return {"placement_hash": digest, "critical_nets": list(analysis["critical_nets"])}


async def routed_synth(rig: SynthSetup, db: Database, run: str = "run-routed") -> dict[str, Any]:
    """`placed_synth` + (8) `pcb_route {nets: critical_nets}` in chunks of at most 50
    (`lock: true`). Returns `placed_synth`'s dict + `{routed: [pcb_route data per chunk],
    board_hash}`."""
    out = await placed_synth(rig, db, run)
    nets = out["critical_nets"]
    routed = []
    for i in range(0, len(nets), ROUTE_CHUNK):
        chunk = nets[i : i + ROUTE_CHUNK]
        routed.append(
            await setup_call(rig.turn(db, f"{run}-route-{i}"), "pcb_route", nets=chunk, lock=True)
        )
    status = await setup_call(rig.turn(db, f"{run}-status"), "pcb_status")
    return out | {"routed": routed, "board_hash": status["board_hash"]}


@contextlib.asynccontextmanager
async def _synth_rig(tmp: Path) -> AsyncIterator[tuple[SynthSetup, Database]]:
    """A fresh `sp4_synth` (schematic only) on its own api-server with board tools on, and a
    database with the test conversation."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
        board_tools=True,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    manager = KicadSessionManager(settings)
    db = await open_setup_db(tmp / "placed.db", project.id)
    try:
        await manager.open(project)
        yield SynthSetup(manager, settings), db
    finally:
        await manager.close_all()
        await db.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_placed(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[PlacedSynth]:
    """`placed_synth` on a fresh `sp4_synth` server for a whole test module (tests run on
    the module loop: `pytest.mark.asyncio(loop_scope="module")`)."""
    async with _synth_rig(tmp_path_factory.mktemp("synth-placed")) as (rig, db):
        yield PlacedSynth(rig, db, await placed_synth(rig, db))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_routed(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[PlacedSynth]:
    """`routed_synth` on a fresh `sp4_synth` server for a whole test module."""
    async with _synth_rig(tmp_path_factory.mktemp("synth-routed")) as (rig, db):
        yield PlacedSynth(rig, db, await routed_synth(rig, db))


# -- fill_rig (SP4 P6b; the Freerouting fill on P5b's routed rig) --------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fill_rig(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[PlacedSynth]:
    """`routed_synth` (P5b's one setup sequence: placed, placement approved, critical nets
    routed and locked) on a fresh `sp4_synth` server for a whole fill test module."""
    async with _synth_rig(tmp_path_factory.mktemp("synth-fill")) as (rig, db):
        yield PlacedSynth(rig, db, await routed_synth(rig, db))
