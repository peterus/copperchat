"""The whole board flow, turn by turn, through the real tools (SP4 P9a, spec §18.2).

`run_flow` drives a board session the way the agent and the user would: setup, block
placement, the user's placement OK, routing + the Freerouting fill, planes / fanout /
stitching / silk, the full check, the user's board OK and the JLCPCB package. Every call
goes through the two-phase adapter (`agent.sdk_tools`), so the change budget, the plan
gate, the approval guards, the autosave and the History snapshots are the real ones. The
user's answers to the agent's design questions are fixed (`Answers`); the user's
messages are inserted as typed messages (`metadata.origin = "user"`).

Importable without pytest: `scripts/odh-dry-run.py` runs the same flow on a copy of a
real project. Nothing here names a part of a real design: `SYNTH_ANSWERS` is the
`sp4_synth` fixture's answer set.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from copperchat.agent.engine import TurnInfo, TurnOutcome
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.design_docs.service import DesignDocs
from copperchat.editing.service import EditingService
from copperchat.history.hooks import HistoryHooks
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.plans.gate import PlanGate
from copperchat.plans.service import PlanService
from copperchat.projects import Project, agent_dir, get_project
from copperchat.tools import ToolContext
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster

ROUTE_CHUNK = 50  # pcb_route takes at most 50 nets per call
BUDGET = 20  # EditingSettings.change_budget default: the costs of one turn stay below it
PLACEMENT_OK = "The placement looks good, approved."
PLACEMENT_QUOTE = "placement looks good"
BOARD_OK = "Board approved for manufacturing."
BOARD_QUOTE = "approved for manufacturing"


class FlowError(RuntimeError):
    """A tool call of the flow failed: which step, which tool, the error payload."""

    def __init__(self, step: str, tool: str, error: Mapping[str, Any]) -> None:
        self.step, self.tool, self.error = step, tool, dict(error)
        self.result: FlowResult | None = None  # the flow up to the failure (run_flow)
        super().__init__(f"{step}: {tool} failed: {json.dumps(self.error)[:4000]}")


@dataclass(frozen=True)
class Answers:
    """The user's fixed answers to the agent's S0/S2/S3 design questions.

    - `outline`: `pcb_set_outline` arguments; `rules`: `pcb_set_rules` arguments (give
      `layers` explicitly - a preset alone keeps the layer count).
    - `blocks`: `None` = `pcb_analyze`'s proposal placed as is (a block whose anchor has
      a `board.md` edge goes to that edge, every other block anywhere on the board);
      otherwise per block, keyed by its anchor ref (or block name): `region` (a
      `pcb_place_block` region; `frac: {x, y, w, h}` is relative to the outline),
      `order` (lower first), `keep_channels`, `absorb` (anchors whose proposed blocks
      join this one, e.g. a protection part next to its connector).
    - `zones`: `pcb_zones` arguments (`None` = `{action: add}`, the default stack).
    - `stitch`: `pcb_stitch` arguments (`{}` = none: no stitching call).
    - `accept_warnings`: DRC warning type -> the user's reason for accepting it (the
      agent shows the full check, the user accepts these; the flow records them in
      `board.md` → Checks for every part, then checks again). Hard thresholds are not
      touched: M3 counts accepted warnings as accepted, by design (spec §12).
    """

    outline: dict[str, Any]
    rules: dict[str, Any]
    blocks: dict[str, dict[str, Any]] | None
    zones: dict[str, Any] | None
    stitch: dict[str, Any]
    keep_channels: float = 1.5
    fanout: tuple[str, ...] = ("GND",)
    accept_warnings: dict[str, str] = field(default_factory=dict)


SYNTH_ANSWERS = Answers(
    outline={
        "shape": "rect",
        "width": 60,
        "height": 45,
        "corner_radius": 1,
        "holes": {"inset": 3},
    },
    rules={
        "layers": 4,
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
    },
    blocks={
        "J1": {"region": {"edge": "left"}, "order": 0, "absorb": ["U2"]},
        "U1": {"region": {"frac": {"x": 0.5, "y": 0, "w": 0.5, "h": 1}}, "order": 1},
        "U3": {"region": {"frac": {"x": 0, "y": 0, "w": 0.5, "h": 1}}, "order": 2},
    },
    zones=None,
    stitch={"net": "GND"},
    accept_warnings={
        "text_thickness": "KiCad library footprints draw silkscreen lines 0.12 mm wide "
        "(KLC), below the JLC preset's 0.15 mm; accepted for library footprints",
    },
)


@dataclass
class StepLog:
    """One tool call of the flow (or a user message: `tool = "user"`)."""

    turn: int
    run_id: str
    tool: str
    seconds: float
    cost: int | None  # change budget the call used (None: not reported)
    summary: dict[str, Any]


@dataclass
class FlowResult:
    steps: list[StepLog]
    metrics: dict[str, Any]  # the final `pcb_check full` data
    seconds: float
    renders: list[Path]
    fab_files: list[str]
    hashes: dict[str, str]  # placement / board: the approved hashes
    critical_nets: list[str] = field(default_factory=list)
    fill: dict[str, Any] = field(default_factory=dict)
    snapshots: dict[int, str] = field(default_factory=dict)  # turn -> snapshot id
    results: dict[str, Any] = field(default_factory=dict)  # last data per tool


# -- the rig: the run engine's hooks and services, without a model ------------------------


@dataclass
class Rig:
    """A project opened as the run engine opens it for a Board session: HistoryService
    (+ hooks: turn start, first-mutation snapshot, turn end autosave), EditingService with
    the plan gate, a database with the test conversation."""

    sessions: KicadSessionManager
    history: HistoryService
    editing: EditingService
    plans: PlanService
    docs: DesignDocs
    hooks: HistoryHooks
    engine: FakeEngine
    db: Database
    settings: Settings
    snapshotted: set[str] = field(default_factory=set)

    @property
    def session(self) -> KicadSession:
        current = self.sessions.current()
        assert current is not None, "no open session"
        return current

    @property
    def project(self) -> Project:
        return self.session.project

    @staticmethod
    def turn(run_id: str) -> TurnInfo:
        return TurnInfo(run_id, 1, "board session", {}, TEST_CID)

    def ctx(self, run_id: str) -> ToolContext:
        """The run engine's `_make_ctx`: one History snapshot per turn before its first
        mutation, `reload_project` after board creation (re-discover, reopen, refresh the
        hooks), a fresh `library_touched`."""
        info = self.turn(run_id)

        async def on_mutation() -> None:
            if run_id not in self.snapshotted:
                self.snapshotted.add(run_id)
                await self.hooks.on_first_mutation(info)

        async def reload_project() -> tuple[Project, KicadSession, int]:
            root = self.settings.projects_root
            fresh = await asyncio.to_thread(get_project, root, self.project.id)
            new = await self.sessions.reopen(fresh)
            generation = new.generation
            self.hooks.on_project_changed(fresh)
            return fresh, new, generation

        session = self.session
        return ToolContext(
            project=session.project,
            session=session,
            settings=session.settings,
            run_id=run_id,
            conversation_id=TEST_CID,
            conversation_type="board",  # a Board session (doc write scope, profiles)
            conversation_seq=1000,
            conversation_title="Board",
            on_mutation=on_mutation,
            editing=self.editing,
            library_touched=[],
            on_library_changed=self.hooks.on_library_changed,
            reload_project=reload_project,
            docs=self.docs,
        )

    async def start_turn(self, run_id: str) -> None:
        await self.hooks.on_turn_start(self.turn(run_id))

    async def end_turn(self, run_id: str) -> None:
        outcome = TurnOutcome(reason="completed", conversation_id=TEST_CID, mutated=True)
        await self.hooks.on_turn_end(self.turn(run_id), outcome)

    async def snapshot_of(self, run_id: str) -> str | None:
        snap = await self.history.store(self.project).latest_for_run(run_id)
        return None if snap is None else snap.id


@contextlib.asynccontextmanager
async def open_rig(settings: Settings, project: Project, db_path: Path) -> AsyncIterator[Rig]:
    """Open `project` (board tools must be on in `settings`) with the run engine's
    services; closes the server and the database afterwards."""
    sessions = KicadSessionManager(settings)
    db = await Database.open(db_path)
    await ensure_test_conversation(db, project.id)
    await db.execute("UPDATE conversations SET type = 'board' WHERE id = ?", (TEST_CID,))
    engine = FakeEngine()
    history = HistoryService(
        db=db, sessions=sessions, broadcaster=RecordingBroadcaster(), runtime=FakeRuntime(engine)
    )
    history.projects_root = settings.projects_root

    async def no_preview(session: Any, doc: Any) -> None:
        return None

    history.render_preview = no_preview  # type: ignore[assignment]
    editing = EditingService(
        db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db), history=history
    )
    plans = PlanService(
        db=db,
        editing=editing,
        history=history,
        runtime=cast(Any, FakeRuntime(engine)),
        broadcaster=None,
    )
    editing.gate = PlanGate(editing, plans)
    editing.plans = plans
    try:
        await sessions.open(project)
        hooks = HistoryHooks(history, project, cast(Any, engine))
        await hooks.on_engine_start()
        docs = DesignDocs(db, settings, None)
        yield Rig(sessions, history, editing, plans, docs, hooks, engine, db, settings)
    finally:
        await sessions.close_all()
        await db.close()


async def call(rig: Rig, run_id: str, name: str, /, **args: Any) -> tuple[dict[str, Any], bool]:
    """One tool call through the real adapter; returns the JSON payload and the error flag."""
    from copperchat.agent.sdk_tools import build_sdk_tool_list
    from copperchat.tools import registry

    ctx = rig.ctx(run_id)
    spec = next(s for s in registry.all_specs() if s.name == name)
    tool = build_sdk_tool_list([spec], lambda: ctx)[0]
    raw = await tool.handler(args)
    payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
    return payload, bool(raw.get("is_error"))


async def user_says(rig: Rig, run_id: str, text: str) -> None:
    """The message that starts turn `run_id`, typed by the user (as the WebSocket submit
    stores it), then the turn start hooks. Later than every render / check before it
    (the clock has millisecond resolution)."""
    await asyncio.sleep(0.01)
    await MessageRepo(rig.db).insert(
        rig.project.id,
        TEST_CID,
        role="user",
        kind="text",
        content=text,
        run_id=run_id,
        metadata={"origin": "user"},
    )
    await asyncio.sleep(0.01)


# -- the flow -------------------------------------------------------------------------------

_SUMMARY_KEYS = (
    "moved",
    "unplaced",
    "routed",
    "failed",
    "completion",
    "connections",
    "fill_routed",
    "ripped",
    "rerouted",
    "open",
    "hard_failures",
    "reviews",
    "verdict",
    "zones",
    "fanned_out",
    "kept",
    "via_pour",
    "unconnected",
    "moved_refs",
    "hidden",
    "seconds",
    "placement_hash",
    "board_hash",
    "png_url",
    "dir",
)


def _summary(data: Mapping[str, Any]) -> dict[str, Any]:
    """The interesting scalars of a tool result (lists shortened to their length)."""
    out: dict[str, Any] = {}
    for key in _SUMMARY_KEYS:
        if key not in data:
            continue
        v = data[key]
        out[key] = len(v) if isinstance(v, list) and key not in ("failed", "open") else v
        if key in ("failed", "open") and isinstance(v, list):
            out[key] = v[:10]
    nets = data.get("nets")
    if isinstance(nets, list) and nets and isinstance(nets[0], dict) and "status" in nets[0]:
        # pcb_route: per net status, solve seconds and failures (the report's step rows)
        out["nets"] = [
            {k: n.get(k) for k in ("net", "status", "seconds", "failed") if n.get(k)}
            for n in nets[:50]
        ]
    return out


def _frac_region(region: Mapping[str, Any], outline: Mapping[str, Any]) -> dict[str, Any]:
    if "frac" not in region:
        return dict(region)
    f = region["frac"]
    return {
        "x": outline["x"] + f["x"] * outline["w"],
        "y": outline["y"] + f["y"] * outline["h"],
        "w": f["w"] * outline["w"],
        "h": f["h"] * outline["h"],
    }


class _Flow:
    def __init__(
        self, rig: Rig, answers: Answers, on_step: Callable[[StepLog], None] | None
    ) -> None:
        self.rig, self.answers, self.on_step = rig, answers, on_step
        self.steps: list[StepLog] = []
        self.turn_no = 0
        self.run_id = ""
        self.used = 0
        self.renders: list[Path] = []
        self.results: dict[str, Any] = {}
        self.snapshots: dict[int, str] = {}
        self.label = ""
        self.hashes: dict[str, str] = {}
        self.critical: list[str] = []
        self.fill: dict[str, Any] = {}

    def result(self, seconds: float) -> FlowResult:
        fab = self.results.get("pcb_fab_export") or {}
        return FlowResult(
            steps=list(self.steps),
            metrics=dict(self.results.get("pcb_check") or {}),
            seconds=seconds,
            renders=list(self.renders),
            fab_files=[str(x["path"]) for x in fab.get("files", [])],
            hashes=dict(self.hashes),
            critical_nets=list(self.critical),
            fill=dict(self.fill),
            snapshots=dict(self.snapshots),
            results=dict(self.results),
        )

    async def new_turn(self, label: str) -> None:
        if self.run_id:
            await self.rig.end_turn(self.run_id)
            snap = await self.rig.snapshot_of(self.run_id)
            if snap is not None:
                self.snapshots[self.turn_no] = snap
        self.turn_no += 1
        self.label = label
        self.run_id = f"run-flow-{self.turn_no}"
        self.used = 0
        await self.rig.start_turn(self.run_id)

    async def finish(self) -> None:
        await self.new_turn("end")

    def _log(self, tool: str, seconds: float, cost: int | None, summary: dict[str, Any]) -> None:
        entry = StepLog(self.turn_no, self.run_id, tool, round(seconds, 3), cost, summary)
        self.steps.append(entry)
        if self.on_step is not None:
            self.on_step(entry)

    async def tool(self, name: str, /, *, cost: int = 0, **args: Any) -> dict[str, Any]:
        """`try_tool`; an error fails the flow (`FlowError`)."""
        data, err = await self.try_tool(name, cost=cost, **args)
        if err:
            raise FlowError(f"turn {self.turn_no} ({self.label})", name, data)
        return data

    async def try_tool(
        self, name: str, /, *, cost: int = 0, **args: Any
    ) -> tuple[dict[str, Any], bool]:
        """Call `name` in the current turn (logged); a call that would pass the turn's
        change budget starts a new turn first (as the agent would continue after a
        reply). Returns the payload and the error flag."""
        if cost and self.used + cost > BUDGET:
            await self.new_turn(self.label)
        t0 = time.monotonic()
        data, err = await call(self.rig, self.run_id, name, **args)
        seconds = time.monotonic() - t0
        if err:
            self._log(name, seconds, None, {"error": data})
            return data, True
        budget = data.get("budget")
        used = budget.get("used") if isinstance(budget, dict) else None
        spent = (used - self.used) if isinstance(used, int) else None
        if isinstance(used, int):
            self.used = used
        self._log(name, seconds, spent, _summary(data))
        self.results[name] = data
        if name == "pcb_render" and data.get("png_url"):
            png = agent_dir(self.rig.project) / "renders" / str(data["png_url"]).rsplit("/", 1)[1]
            self.renders.append(png)
        return data, False

    async def say(self, text: str) -> None:
        await self.new_turn(self.label)
        await user_says(self.rig, self.run_id, text)
        self._log("user", 0.0, 0, {"text": text})


async def _place(f: _Flow, answers: Answers) -> None:
    """Turn 2: blocks from `pcb_analyze`, one `pcb_place_block` per block, legalise."""
    outline = (await f.tool("pcb_status"))["outline"]
    analysis = await f.tool("pcb_analyze", scope="all")
    classes: dict[str, str] = analysis["parts"]
    blocks: list[dict[str, Any]] = []
    for b in analysis["blocks"]:
        refs = [r for r in b["refs"] if classes.get(r) not in ("mounting_hole", "fiducial")]
        if refs:
            block: dict[str, Any] = {"name": b["name"], "refs": refs}
            if b.get("anchor") in refs:
                block["anchor"] = b["anchor"]
            blocks.append(block)
    plan = answers.blocks or {}

    def key(b: Mapping[str, Any]) -> str:
        return str(b.get("anchor") or b["name"])

    by_key = {key(b): b for b in blocks}
    for k, spec in plan.items():
        host = by_key.get(k)
        for other in spec.get("absorb", ()):
            src = by_key.get(other)
            if host is not None and src is not None and src is not host:
                host["refs"] = host["refs"] + src["refs"]
                blocks.remove(src)
                del by_key[other]
    await f.tool("pcb_define_blocks", cost=1, blocks=blocks, replace=True)
    order = sorted(blocks, key=lambda b: (plan.get(key(b), {}).get("order", 99), b["name"]))
    whole = {"x": outline["x"], "y": outline["y"], "w": outline["w"], "h": outline["h"]}
    for b in order:
        spec = plan.get(key(b), {})
        region = _frac_region(spec["region"], outline) if "region" in spec else whole
        await f.tool(
            "pcb_place_block",
            cost=1,
            block=b["name"],
            region=region,
            keep_channels=spec.get("keep_channels", answers.keep_channels),
        )
    await f.tool("pcb_legalize", cost=1, scope="all")


def _fill_error_nets(error: Mapping[str, Any]) -> list[str]:
    """The nets of the connections a refused fill would have ripped (`fill_not_acceptable`)."""
    err = error.get("error", error)
    rows = (err.get("details") or {}).get("connections") or []
    return sorted({str(r["connection"]).split(":", 1)[0] for r in rows if "connection" in r})


def _nothing_to_fill(error: Mapping[str, Any]) -> bool:
    err = error.get("error", error)
    return "nothing to fill" in f"{err.get('what', '')} {err.get('why', '')}"


async def _route_nets(f: _Flow, nets: list[str]) -> None:
    for i in range(0, len(nets), ROUTE_CHUNK):
        chunk = nets[i : i + ROUTE_CHUNK]
        await f.tool("pcb_route", cost=math.ceil(len(chunk) / 5), nets=chunk, lock=True)


BOARD_MD = "board.md"


async def _accept_warnings(f: _Flow, answers: Answers, full: Mapping[str, Any]) -> bool:
    """The user accepts the DRC warning types of `answers.accept_warnings` that the full
    check lists unaccepted: one `board.md` Checks row per type naming every part (the
    agent writes it with the doc tools). Returns whether anything was written."""
    open_types = {
        str(w["type"])
        for w in full.get("warnings", [])
        if not w.get("accepted") and str(w["type"]) in answers.accept_warnings
    }
    if not open_types:
        return False
    refs = sorted({p for p in (await f.tool("pcb_analyze", scope="parts"))["parts"]})
    rows = [
        f"| {t} | {', '.join(refs)} | {answers.accept_warnings[t]} | user |"
        for t in sorted(open_types)
    ]
    table = "\n".join(
        ["| DRC type | Items/Refs | Reason | Accepted by |", "|---|---|---|---|", *rows]
    )
    doc = await f.tool("doc_read", path=BOARD_MD)
    await f.tool(
        "doc_patch_section",
        path=BOARD_MD,
        heading="Checks",
        body=table + "\n",
        base_version=doc.get("version"),
        summary="accepted DRC warnings (user)",
        mode="replace",
        create=True,
    )
    return True


FILL_ATTEMPTS = 3  # fills tried; between two, ladder rung (c) routes the refused nets


async def _route(f: _Flow, critical: list[str]) -> dict[str, Any]:
    """Turn 4: the critical nets with the own router (locked), then the Freerouting fill
    with the scripted ladder (spec §10.3): a fill refused by the 20 % rule → rung (c)
    `pcb_route` on the nets it would have ripped, then the fill again; open connections
    after the fill → rung (c) on their nets. Every rung is a logged tool call."""
    await _route_nets(f, critical)
    fill: dict[str, Any] = {}
    for _ in range(FILL_ATTEMPTS):
        data, err = await f.try_tool("pcb_route_fill", cost=5)
        if not err:
            fill = data
            break
        if _nothing_to_fill(data):
            return {"completion": 1.0, "nothing_to_fill": True}
        nets = _fill_error_nets(data)
        if not nets:
            raise FlowError(f"turn {f.turn_no} ({f.label})", "pcb_route_fill", data)
        await _route_nets(f, nets)
    else:
        raise FlowError(f"turn {f.turn_no} ({f.label})", "pcb_route_fill", data)
    if fill.get("completion", 1.0) < 1.0:
        await _route_nets(f, sorted({o["net"] for o in fill.get("open", [])}))
        failed = [
            x
            for x in f.results["pcb_route"].get("nets", [])
            if x.get("status") not in ("routed", "already_routed")
        ]
        if f.results["pcb_route"].get("totals", {}).get("failed"):
            # rung (d): only the user can change the placement / layers / size
            raise FlowError(
                f"turn {f.turn_no} ({f.label})",
                "pcb_route",
                {"why": "connections still open after the ladder", "nets": failed[:10]},
            )
    return fill


async def run_flow(
    rig: Rig, answers: Answers, *, on_step: Callable[[StepLog], None] | None = None
) -> FlowResult:
    """The product flow on `rig`'s project (schematic only or a board without parts
    placed), turn by turn: see the module docstring. Raises `FlowError` on the first
    failing tool call; its `result` holds the flow up to there (steps, renders, the last
    data per tool)."""
    f = _Flow(rig, answers, on_step)
    start = time.monotonic()
    try:
        await _turns(f, answers)
    except FlowError as exc:
        exc.result = f.result(time.monotonic() - start)
        raise
    return f.result(time.monotonic() - start)


async def _turns(f: _Flow, answers: Answers) -> None:
    # 1: S0/S1 - the board from the schematic, outline and rules as the user answered
    await f.new_turn("setup")
    await f.tool("pcb_sync_from_schematic", cost=3)
    await f.tool("pcb_set_outline", cost=2, **answers.outline)
    await f.tool("pcb_set_rules", cost=2, **answers.rules)
    # 2: S2/S3 - blocks and placement, shown to the user
    await f.new_turn("placement")
    await _place(f, answers)
    check = await f.tool("pcb_check", level="quick")
    render = await f.tool("pcb_render", view="placement")
    f.hashes["placement"] = str(check["placement_hash"])
    if render["placement_hash"] != f.hashes["placement"]:
        raise FlowError("placement", "pcb_render", {"why": "hash differs from the check"})
    # 3: the user's OK binds the placement
    await f.say(PLACEMENT_OK)
    await f.tool(
        "pcb_approve",
        cost=1,
        stage="placement",
        hash=f.hashes["placement"],
        user_quote=PLACEMENT_QUOTE,
    )
    # 4: S4 - routing
    await f.new_turn("routing")
    f.critical = list((await f.tool("pcb_analyze", scope="nets"))["critical_nets"])
    f.fill = await _route(f, f.critical)
    # 5: S5/S6 - copper, silk, full check, renders
    await f.new_turn("copper")
    zones = answers.zones or {"action": "add"}
    await f.tool("pcb_zones", cost=3, **zones)
    for net in answers.fanout:
        await f.tool("pcb_fanout", cost=2, net=net)
    if answers.stitch:
        await f.tool("pcb_stitch", cost=2, **answers.stitch)
    await f.tool("pcb_tidy_silk", cost=1)
    full = await f.tool("pcb_check", level="full")
    if await _accept_warnings(f, answers, full):
        full = await f.tool("pcb_check", level="full")
    top = await f.tool("pcb_render", view="top")
    await f.tool("pcb_render", view="bottom")
    f.hashes["board"] = str(full["board_hash"])
    if top["board_hash"] != f.hashes["board"]:
        raise FlowError("copper", "pcb_render", {"why": "hash differs from the full check"})
    # 6: the user's OK binds the board; the fab package
    await f.say(BOARD_OK)
    await f.tool(
        "pcb_approve", cost=1, stage="board", hash=f.hashes["board"], user_quote=BOARD_QUOTE
    )
    await f.tool("pcb_fab_export", preset="jlcpcb")
    await f.finish()


def as_jsonable(result: FlowResult) -> dict[str, Any]:
    """`result` for a JSON report (paths as strings)."""
    d = dataclasses.asdict(result)
    d["renders"] = [str(p) for p in result.renders]
    return d
