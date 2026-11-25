"""`pcb_approve` guards and apply (P3b Task 3, spec §8.3, B-C10) against a real temp
database and a fake board: `current_hashes` returns fixed hashes; the evidence rows,
messages, change entries and plans are written with explicit timestamps. No KiCad."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.changes.repo import ChangeEntry, ChangeRepo
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.editing.gate import BudgetGate
from copperchat.editing.service import EditingService
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import approve as approve_mod
from copperchat.kicad_tools.board.approve import apply_approve, prepare_approve
from copperchat.kicad_tools.board.engine import BoardChangeSet
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.plans.gate import PlanGate
from copperchat.projects import Project
from copperchat.tools import ToolContext
from tests.support.conversations import TEST_CID, ensure_test_conversation

PID = "a1b2c3d4e5f6"
BOARD = "main.kicad_pcb"
RUN1, RUN2 = "run-1", "run-2"
PA, PB = "a" * 64, "b" * 64  # placement hashes
BA, BB = "c" * 64, "d" * 64  # board hashes
OTHER_CID = "e" * 32
T0 = "2026-11-25T10:00:00.000Z"  # before the evidence
T_RENDER = "2026-11-25T10:01:00.000Z"
T_MID = "2026-11-25T10:01:30.000Z"
T_CHECK = "2026-11-25T10:02:00.000Z"
T_BOTTOM = "2026-11-25T10:02:30.000Z"
T_LATER = "2026-11-25T10:05:00.000Z"
T_AFTER_MSG = "2026-11-25T10:06:00.000Z"
OK_TEXT = "Thanks, that looks good to me - approve the placement please."
OK_QUOTE = "looks good to me - approve the placement"


@dataclasses.dataclass
class Rig:
    db: Database
    editing: EditingService
    project: Project
    settings: Settings
    hashes: dict[str, str]

    def ctx(self, run_id: str = RUN2, cid: str | None = TEST_CID) -> ToolContext:
        return ToolContext(
            project=self.project,
            session=cast(Any, SimpleNamespace(read_only=False)),
            settings=self.settings,
            run_id=run_id,
            editing=self.editing,
            conversation_id=cid,
        )

    async def _stamp(self, row_id: int, at: str) -> None:
        await self.db.execute("UPDATE board_reviews SET created_at = ? WHERE id = ?", (at, row_id))

    async def render(self, view: str, at: str, *, p: str = PA, b: str = BA,
                     cid: str = TEST_CID) -> None:  # fmt: skip
        rid = await BoardReviewRepo(self.db).record_render(
            PID, BOARD, cid, RUN1, view=view, placement_hash=p, board_hash=b
        )
        await self._stamp(rid, at)

    async def check(self, level: str, at: str, *, m1: bool = True, hard: int = 0,
                    p: str = PA, b: str = BA) -> None:  # fmt: skip
        rid = await BoardReviewRepo(self.db).record_check(
            PID, BOARD, TEST_CID, RUN1, level=level, hard_failures=hard, m1_pass=m1,
            placement_hash=p, board_hash=b, stage="placement" if level == "quick" else "board",
        )  # fmt: skip
        await self._stamp(rid, at)

    async def placement_shown(self) -> None:
        await self.render("placement", T_RENDER)
        await self.check("quick", T_CHECK)

    async def board_shown(self) -> None:
        await self.render("top", T_RENDER)
        await self.check("full", T_CHECK)
        await self.render("bottom", T_BOTTOM)

    async def said(self, text: str = OK_TEXT, *, at: str = T_LATER, run_id: str = RUN2,
                   metadata: dict[str, Any] | None = None) -> None:  # fmt: skip
        meta = {"origin": "user"} if metadata is None else metadata
        mid = await MessageRepo(self.db).insert(
            PID, TEST_CID, role="user", kind="text", content=text, run_id=run_id, metadata=meta
        )
        await self.db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (at, mid))

    async def wrote(self, at: str, kind: str = "board_move") -> None:
        await ChangeRepo(self.db).add_many(
            [ChangeEntry(PID, RUN2, None, 0, "pcb_move", "modify", kind, "", "", created_at=at)]
        )

    async def plan(self, status: str) -> None:
        await self.db.execute(
            "INSERT INTO plans (id, project_id, title, summary, status, revision, "
            "created_run_id, created_at, updated_at, conversation_id) "
            "VALUES (?, ?, 't', 's', ?, 1, ?, ?, ?, ?)",
            ("p" * 32, PID, status, RUN1, T0, T0, TEST_CID),
        )

    async def counts(self) -> tuple[int, int]:
        cp = await self.db.fetchone("SELECT COUNT(*) FROM board_checkpoints")
        ch = await self.db.fetchone("SELECT COUNT(*) FROM changes WHERE kind = 'board_approve'")
        assert cp is not None and ch is not None
        return int(cp[0]), int(ch[0])


@pytest.fixture
async def rig(
    tmp_path: Path, make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Rig]:
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db, PID)
    editing = EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    project = Project(
        id=PID, name="main", rel_path="main", dir=tmp_path, pro_file=tmp_path / "main.kicad_pro",
        sch_file=tmp_path / "main.kicad_sch", pcb_file=tmp_path / BOARD,
    )  # fmt: skip
    r = Rig(db, editing, project, make_settings(board_tools=True), {"placement": PA, "board": BA})

    async def hashes(ctx: ToolContext) -> dict[str, str]:
        return dict(r.hashes)

    monkeypatch.setattr(approve_mod, "current_hashes", hashes)
    try:
        yield r
    finally:
        await db.close()


def _args(stage: str = "placement", digest: str = PA, quote: str = OK_QUOTE) -> dict[str, Any]:
    return {"stage": stage, "hash": digest, "user_quote": quote}


async def _refused(rig: Rig, ctx: ToolContext | None = None, **kw: Any) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as err:
        await prepare_approve(ctx or rig.ctx(), _args(**kw))
    assert err.value.code == "approval_not_allowed", err.value
    return err.value


def _guard(err: StructuredToolError) -> int:
    assert err.details is not None
    return int(err.details["guard"])


# -- guard 5 / 6 ----------------------------------------------------------------------------


async def test_same_turn_approval_refused(rig: Rig) -> None:
    await rig.said(at=T0)  # the run's user message, then render + check in the same run
    await rig.placement_shown()
    assert _guard(await _refused(rig)) == 5


@pytest.mark.parametrize("origin", ["plan", "close-session", None])
async def test_server_generated_message_does_not_count(rig: Rig, origin: str | None) -> None:
    await rig.placement_shown()
    await rig.said(metadata={} if origin is None else {"origin": origin})
    assert _guard(await _refused(rig)) == 5


async def test_message_of_another_run_does_not_count(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said(run_id="run-other")
    assert _guard(await _refused(rig)) == 5


async def test_quote_not_substring(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    assert _guard(await _refused(rig, quote="please approve everything now")) == 6


async def test_trivial_quote(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    assert _guard(await _refused(rig, quote="ok")) == 6


# -- guard 1 --------------------------------------------------------------------------------


async def test_placement_with_board_hash_refused(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    err = await _refused(rig, digest=BA)
    assert _guard(err) == 1 and err.details is not None and err.details["other_stage"] is True
    err = await _refused(rig, stage="board", digest=PA)
    assert _guard(err) == 1 and err.details is not None and err.details["other_stage"] is True


async def test_wrong_hash(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    err = await _refused(rig, digest=PB)
    assert _guard(err) == 1 and err.details is not None and "other_stage" not in err.details


# -- guard 7 --------------------------------------------------------------------------------


async def test_plan_running_refused(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    await rig.plan("running")
    assert _guard(await _refused(rig)) == 7


async def test_paused_plan_does_not_block(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    await rig.plan("paused")
    cs = await prepare_approve(rig.ctx(), _args())
    assert cs.tool == "pcb_approve"


# -- guard 4 (ABA) --------------------------------------------------------------------------


async def test_aba_in_approving_turn_refused(rig: Rig) -> None:
    """Evidence for hash A, the user's message, then a move away and back (two board_move
    entries newer than the evidence): the hash is A again, but the user never saw it."""
    await rig.placement_shown()
    await rig.said()
    await rig.wrote("2026-11-25T10:05:10.000Z")
    await rig.wrote("2026-11-25T10:05:20.000Z")
    assert _guard(await _refused(rig)) == 4


async def test_aba_in_earlier_turn_refused(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.wrote("2026-11-25T10:03:00.000Z")  # after the evidence, before the message
    await rig.said()
    assert _guard(await _refused(rig)) == 4


async def test_write_before_evidence_ok(rig: Rig) -> None:
    await rig.wrote(T0)
    await rig.placement_shown()
    await rig.said()
    cs = await prepare_approve(rig.ctx(), _args())
    assert cs.summary["evidence_since"] == T_RENDER


async def test_write_at_evidence_time_refused(rig: Rig) -> None:
    """A tie between the write and the oldest evidence row fails closed."""
    await rig.wrote(T_RENDER)
    await rig.placement_shown()
    await rig.said()
    assert _guard(await _refused(rig)) == 4


@pytest.mark.parametrize("stage", ["placement", "board"])
async def test_write_between_render_and_check_refused(rig: Rig, stage: str) -> None:
    """The write is newer than the (first) render, older than the check: the render
    showed a state before the write."""
    if stage == "placement":
        await rig.render("placement", T_RENDER)
        await rig.wrote(T_MID)
        await rig.check("quick", T_CHECK)
    else:
        await rig.render("top", T_RENDER)
        await rig.wrote(T_MID)
        await rig.check("full", T_CHECK)
        await rig.render("bottom", T_BOTTOM)
    await rig.said()
    digest = PA if stage == "placement" else BA
    assert _guard(await _refused(rig, stage=stage, digest=digest)) == 4


async def test_approve_entry_does_not_count(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.wrote("2026-11-25T10:03:00.000Z", kind="board_approve")
    await rig.said()
    cs = await prepare_approve(rig.ctx(), _args())
    assert cs.kind == "board_approve"


async def test_schematic_write_does_not_count(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.wrote("2026-11-25T10:03:00.000Z", kind="symbol")
    await rig.said()
    await prepare_approve(rig.ctx(), _args())


async def test_other_project_write_does_not_count(rig: Rig) -> None:
    await rig.placement_shown()
    await ChangeRepo(rig.db).add_many(
        [
            ChangeEntry(
                "f" * 12,
                RUN2,
                None,
                0,
                "pcb_move",
                "modify",
                "board_move",
                "",
                "",
                created_at="2026-11-25T10:03:00.000Z",
            )
        ]
    )
    await rig.said()
    await prepare_approve(rig.ctx(), _args())


# -- guards 2 / 3 ---------------------------------------------------------------------------


async def test_no_placement_render(rig: Rig) -> None:
    await rig.check("quick", T_CHECK)
    await rig.said()
    err = await _refused(rig)
    assert _guard(err) == 2 and err.details is not None
    assert err.details["missing"] == ["pcb_render view=placement"]


async def test_no_check(rig: Rig) -> None:
    await rig.render("placement", T_RENDER)
    await rig.said()
    assert _guard(await _refused(rig)) == 2


async def test_evidence_other_conversation(rig: Rig) -> None:
    await rig.render("placement", T_RENDER, cid=OTHER_CID)
    await rig.check("quick", T_CHECK)
    await rig.said()
    assert _guard(await _refused(rig)) == 2


async def test_evidence_other_hash(rig: Rig) -> None:
    await rig.render("placement", T_RENDER, p=PB)
    await rig.check("quick", T_CHECK)
    await rig.said()
    assert _guard(await _refused(rig)) == 2


async def test_no_conversation(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    assert _guard(await _refused(rig, rig.ctx(cid=None))) == 2


async def test_m1_failing(rig: Rig) -> None:
    await rig.render("placement", T_RENDER)
    await rig.check("quick", T_CHECK, m1=False)
    await rig.said()
    assert _guard(await _refused(rig)) == 3


async def test_newest_check_counts(rig: Rig) -> None:
    """A passing check followed by a failing one for the same hash: the newest fails."""
    await rig.render("placement", T_RENDER)
    await rig.check("quick", T_CHECK)
    await rig.check("full", T_BOTTOM, m1=False)
    await rig.said()
    assert _guard(await _refused(rig)) == 3


async def test_full_check_with_hard_failures(rig: Rig) -> None:
    await rig.render("top", T_RENDER)
    await rig.check("full", T_CHECK, hard=2)
    await rig.render("bottom", T_BOTTOM)
    await rig.said()
    assert _guard(await _refused(rig, stage="board", digest=BA)) == 3


async def test_missing_bottom_render(rig: Rig) -> None:
    await rig.render("top", T_RENDER)
    await rig.check("full", T_CHECK)
    await rig.said()
    err = await _refused(rig, stage="board", digest=BA)
    assert _guard(err) == 2 and err.details is not None
    assert err.details["missing"] == ["pcb_render view=bottom"]


async def test_board_needs_full_not_quick(rig: Rig) -> None:
    await rig.render("top", T_RENDER)
    await rig.check("quick", T_CHECK)
    await rig.render("bottom", T_BOTTOM)
    await rig.said()
    assert _guard(await _refused(rig, stage="board", digest=BA)) == 2


# -- happy paths and apply ------------------------------------------------------------------


async def _approve(rig: Rig, stage: str, digest: str) -> dict[str, Any]:
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args(stage, digest))
    assert cs.logical == Logical(1) and cs.kind == "board_approve" and cs.board_stages == []
    result = await apply_approve(ctx, cs)
    assert result.changed_docs == set()
    assert result.data is not None
    return result.data


async def test_happy_placement(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    seen: list[tuple[str, str, str | None, int]] = []

    async def listener(pid: str, run: str, step: str | None, count: int) -> None:
        seen.append((pid, run, step, count))

    rig.editing.on_changes_recorded.append(listener)
    data = await _approve(rig, "placement", PA)
    assert data["stage"] == "placement" and data["hash"] == PA and data["approved_at"]
    assert "route" in data["next"]
    row = await BoardCheckpointRepo(rig.db).latest(PID, BOARD, "placement")
    assert row is not None and row.hash == PA and row.user_quote == OK_QUOTE
    assert row.run_id == RUN2 and row.conversation_id == TEST_CID
    entries = await ChangeRepo(rig.db).for_run(PID, RUN2)
    assert [(e.kind, e.op, e.uuid, e.sheet_path) for e in entries] == [
        ("board_approve", "add", f"board-checkpoint-{row.id}", "")
    ]
    assert entries[0].after == {"stage": "placement", "hash": PA, "user_quote": OK_QUOTE}
    assert seen == [(PID, RUN2, None, 1)]


async def test_happy_board(rig: Rig) -> None:
    await rig.board_shown()
    await rig.said("Board looks great, approve the board for manufacturing.")
    ctx = rig.ctx()
    args = _args("board", BA, "approve the board for manufacturing")
    cs = await prepare_approve(ctx, args)
    data = (await apply_approve(ctx, cs)).data
    assert data is not None and data["stage"] == "board" and "pcb_fab_export" in data["next"]
    assert await rig.counts() == (1, 1)


async def test_hash_changed_between_prepare_and_apply(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args())
    rig.hashes["placement"] = PB
    with pytest.raises(StructuredToolError) as err:
        await apply_approve(ctx, cs)
    assert _guard(err.value) == 1
    assert await rig.counts() == (0, 0)


async def test_write_between_prepare_and_apply(rig: Rig) -> None:
    await rig.placement_shown()
    await rig.said()
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args())
    await rig.wrote(T_AFTER_MSG)
    with pytest.raises(StructuredToolError) as err:
        await apply_approve(ctx, cs)
    assert _guard(err.value) == 4
    assert await rig.counts() == (0, 0)


@pytest.mark.parametrize("guard", [1, 4])
async def test_apply_guard_refusal_leaves_no_rows(rig: Rig, guard: int) -> None:
    await rig.placement_shown()
    await rig.said()
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args())
    changes_before = await rig.db.fetchone("SELECT COUNT(*) FROM changes")
    if guard == 1:
        rig.hashes["placement"] = PB
    else:
        await rig.wrote(T_AFTER_MSG)
    changes_mid = await rig.db.fetchone("SELECT COUNT(*) FROM changes")
    with pytest.raises(StructuredToolError):
        await apply_approve(ctx, cs)
    changes_after = await rig.db.fetchone("SELECT COUNT(*) FROM changes")
    assert changes_before is not None and changes_mid is not None and changes_after is not None
    assert changes_after[0] == changes_mid[0]
    assert (await rig.counts())[0] == 0


async def test_apply_real_path_commits_both(rig: Rig) -> None:
    """No monkeypatch of the change repo: one transaction, no nested-transaction error."""
    await rig.placement_shown()
    await rig.said()
    await _approve(rig, "placement", PA)
    assert await rig.counts() == (1, 1)


async def test_apply_change_entry_failure_rolls_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.placement_shown()
    await rig.said()
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args())
    real = ChangeRepo.add_many_in_transaction

    async def insert_then_fail(self: ChangeRepo, entries: Any) -> int:
        await real(self, entries)
        raise RuntimeError("injected after the change insert")

    monkeypatch.setattr(ChangeRepo, "add_many_in_transaction", insert_then_fail)
    with pytest.raises(RuntimeError, match="injected"):
        await apply_approve(ctx, cs)
    assert await rig.counts() == (0, 0)


async def test_apply_refuses_foreign_change_set(rig: Rig) -> None:
    cs = BoardChangeSet(tool="pcb_move", kind="board_move", board_stages=[], logical=Logical(1))
    with pytest.raises(TypeError):
        await apply_approve(rig.ctx(), cs)


async def test_board_tools_off_refused(rig: Rig) -> None:
    rig.settings = rig.settings.model_copy(update={"board_tools": False})
    with pytest.raises(StructuredToolError) as err:
        await prepare_approve(rig.ctx(), _args())
    assert err.value.code == "board_tools_disabled"


@pytest.mark.parametrize(
    "args",
    [
        {"stage": "routing", "hash": PA, "user_quote": OK_QUOTE},
        {"stage": "placement", "hash": PA.upper(), "user_quote": OK_QUOTE},
        {"stage": "placement", "hash": PA, "user_quote": ""},
        {"stage": "placement", "hash": PA, "user_quote": "x" * 301},
        {"stage": "placement", "hash": PA, "user_quote": OK_QUOTE, "force": True},
        {"stage": "placement", "user_quote": OK_QUOTE},
    ],
)
async def test_invalid_arguments(rig: Rig, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        await prepare_approve(rig.ctx(), args)
    assert err.value.code == "invalid_argument"


# -- the plan gate --------------------------------------------------------------------------


async def test_plan_policy_always_allowed(rig: Rig) -> None:
    """`plan_policy = always` and no active step: pcb_approve passes both gates."""
    await EditingSettingsRepo(rig.db).put(PID, plan_policy="always")
    ctx = rig.ctx()
    await BudgetGate(rig.editing).before_prepare(ctx, "pcb_approve")
    plans = SimpleNamespace(repo=SimpleNamespace(open_status=_none), clear_step=lambda state: None)
    await PlanGate(rig.editing, cast(Any, plans)).before_prepare(ctx, "pcb_approve")
    with pytest.raises(StructuredToolError) as err:
        await BudgetGate(rig.editing).before_prepare(ctx, "pcb_move")
    assert err.value.code == "plan_step_required"


async def _none(project_id: str) -> None:
    return None
