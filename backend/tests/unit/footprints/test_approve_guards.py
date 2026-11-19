"""`fp_approve` guards (F4b Task 1, spec §7.7, index author calls 13 and 15) against a
real database and a fake geometry: the footprint read-back is `build_target` of the
L201 input (it passes `fp_check`), the evidence rows (`footprint_reviews`, messages,
plans) are written with explicit timestamps. Nothing here talks to KiCad."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.footprints import FootprintReviewRepo, FootprintWriteRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.editing.gate import BudgetGate
from copperchat.editing.service import EditingService
from copperchat.errors import StructuredToolError
from copperchat.history.library_lock import library_lock
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.footprints import create as create_mod
from copperchat.kicad_tools.footprints.approve import (
    apply_approve,
    prepare_approve,
    quote_in_message,
)
from copperchat.kicad_tools.footprints.build import build_target, parse_create_args
from copperchat.kicad_tools.footprints.create import apply_footprint_create
from copperchat.kicad_tools.footprints.engine import FootprintApplied, FootprintChangeSet
from copperchat.kicad_tools.footprints.model import REVIEW_FIELD, FootprintGeometry, geometry_hash
from copperchat.kicad_tools.footprints.read import fp_check
from copperchat.library import service as service_mod
from copperchat.library.local import LocalLibrary
from copperchat.plans.gate import PlanGate
from copperchat.plans.repo import PlanRepo
from copperchat.tools import ToolContext
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.unit.footprints.test_build import l201_args
from tests.unit.footprints.test_create_prepare import NAME, CreateSession
from tests.unit.library.test_service import LOCAL_URI, REPO, _policy

PID = "a1b2c3d4e5f6"
RUN1, RUN2 = "run-1", "run-2"
LIB_ID = f"{REPO}:{NAME}"
T_RENDER = "2026-11-19T10:00:00.000Z"
T_CHECK = "2026-11-19T10:00:01.000Z"
T_EARLY = "2026-11-19T09:59:00.000Z"
T_LATER = "2026-11-19T10:05:00.000Z"
OK_TEXT = "The render looks right, approve it please."
OK_QUOTE = "looks right, approve it"


class ApproveSession(CreateSession):
    """CreateSession with a project id and the apply surface of a KicadSession."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.project.id = PID
        self.server_alive = True
        self.rows = {REPO: LOCAL_URI}
        self.names = [NAME]

    @contextlib.asynccontextmanager
    async def applying(self) -> AsyncIterator[None]:
        yield


@dataclasses.dataclass
class Rig:
    session: ApproveSession
    db: Database
    editing: EditingService
    geometry: FootprintGeometry

    @property
    def digest(self) -> str:
        return geometry_hash(self.geometry)

    def ctx(self, run_id: str = RUN2) -> ToolContext:
        return ToolContext(
            project=cast(Any, self.session.project),
            session=cast(KicadSession, self.session),
            settings=cast(Any, self.session.settings),
            run_id=run_id,
            editing=self.editing,
            conversation_id=TEST_CID,
        )

    async def shown(
        self,
        *,
        rendered: str | None = T_RENDER,
        checked: str | None = T_CHECK,
        digest: str | None = None,
        errors: int = 0,
    ) -> None:
        """fp_render / fp_check of the geometry at the given times (None: not done)."""
        repo = FootprintReviewRepo(self.db)
        h = digest or self.digest
        await repo.record(
            TEST_CID,
            LIB_ID,
            h,
            RUN1,
            rendered=rendered is not None,
            checked=checked is not None,
            check_errors=errors if checked is not None else None,
        )
        await self.db.execute(
            "UPDATE footprint_reviews SET rendered_at = ?, checked_at = ? "
            "WHERE conversation_id = ? AND lib_id = ? AND geometry_hash = ?",
            (rendered, checked, TEST_CID, LIB_ID, h),
        )

    async def said(
        self,
        text: str = OK_TEXT,
        *,
        at: str = T_LATER,
        run_id: str = RUN2,
        metadata: dict[str, Any] | None = None,
        role: str = "user",
        kind: str = "text",
    ) -> None:
        """A message of the conversation, processed by run `run_id`, created at `at`."""
        meta = {"origin": "user"} if metadata is None else metadata
        mid = await MessageRepo(self.db).insert(
            PID, TEST_CID, role=role, kind=kind, content=text, run_id=run_id, metadata=meta
        )
        await self.db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (at, mid))

    async def plan(self, status: str) -> None:
        await self.db.execute(
            "INSERT INTO plans (id, project_id, title, summary, status, revision, "
            "created_run_id, created_at, updated_at, conversation_id) "
            "VALUES (?, ?, 't', 's', ?, 1, ?, ?, ?, ?)",
            ("p" * 32, PID, status, RUN1, T_EARLY, T_EARLY, TEST_CID),
        )

    def lib(self) -> LocalLibrary:
        root = self.session.root
        return LocalLibrary(
            nickname=REPO,
            dir=root / "footprints" / f"{REPO}.pretty",
            uri=LOCAL_URI,
            repo_root=root,
            table=self.session.project.dir / "fp-lib-table",
        )


def _target(**kw: Any) -> FootprintGeometry:
    from tests.unit.footprints.test_build import LIB

    return build_target(parse_create_args(l201_args(**kw)), LIB)


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    session = ApproveSession(tmp_path / REPO)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(session.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db, PID)
    editing = EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    rig = Rig(session, db, editing, _target())

    async def geometry(
        self: Any, lib_id: str, notes: list[dict[str, Any]] | None = None
    ) -> FootprintGeometry | None:
        return rig.geometry if lib_id == LIB_ID else None

    monkeypatch.setattr(service_mod.LibraryService, "footprint_geometry", geometry)
    try:
        yield rig
    finally:
        await db.close()


def _args(rig: Rig, **kw: Any) -> dict[str, Any]:
    args = {"lib_id": LIB_ID, "geometry_hash": rig.digest, "user_quote": OK_QUOTE}
    args.update(kw)
    return args


async def _refused(rig: Rig, ctx: ToolContext | None = None, **kw: Any) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as err:
        await prepare_approve(ctx or rig.ctx(), _args(rig, **kw))
    return err.value


async def _guard(rig: Rig, **kw: Any) -> int:
    err = await _refused(rig, **kw)
    assert err.code == "approval_not_allowed", err.to_dict()
    assert err.details is not None
    guard: int = err.details["guard"]
    return guard


# -- the happy path ----------------------------------------------------------------------


async def test_all_guards_pass(rig: Rig) -> None:
    await rig.shown()
    await rig.said()
    cs = await prepare_approve(rig.ctx(), _args(rig))
    assert isinstance(cs, FootprintChangeSet)
    assert cs.tool == "fp_approve" and cs.logical == Logical(1)
    assert cs.library is not None and cs.library.nickname == REPO
    assert cs.summary["lib_id"] == LIB_ID and cs.summary["geometry_hash"] == rig.digest
    assert cs.summary["user_quote"] == OK_QUOTE and cs.summary["conversation_id"] == TEST_CID
    assert [(s.kind, s.lib_id) for s in cs.fp_stages] == [("approve", LIB_ID)]


# -- guard 1 ----------------------------------------------------------------------------


async def test_not_local(rig: Rig) -> None:
    await rig.shown()
    await rig.said()
    assert await _guard(rig, lib_id="Inductor_SMD:L_Bourns-SRN4018") == 1


async def test_already_approved(rig: Rig) -> None:
    fields = {**rig.geometry.fields, REVIEW_FIELD: "no"}
    rig.geometry = dataclasses.replace(rig.geometry, fields=fields)
    await rig.shown()
    await rig.said()
    assert await _guard(rig) == 1


async def test_pending_journal_row_refused(rig: Rig) -> None:
    await FootprintWriteRepo(rig.db).insert_pending(
        PID, RUN1, LIB_ID, f"footprints/{REPO}.pretty/{NAME}.kicad_mod", "replace", "x"
    )
    await rig.shown()
    await rig.said()
    assert await _guard(rig) == 1


# -- guard 2 ----------------------------------------------------------------------------


async def test_wrong_hash(rig: Rig) -> None:
    await rig.shown(digest="0" * 64)
    await rig.said()
    assert await _guard(rig, geometry_hash="0" * 64) == 2


async def test_bad_hash_is_invalid_argument(rig: Rig) -> None:
    err = await _refused(rig, geometry_hash=rig.digest.upper())
    assert err.code == "invalid_argument"


# -- guard 3 ----------------------------------------------------------------------------


async def test_no_render(rig: Rig) -> None:
    await rig.shown(rendered=None)
    await rig.said()
    assert await _guard(rig) == 3


async def test_no_check(rig: Rig) -> None:
    await rig.shown(checked=None)
    await rig.said()
    assert await _guard(rig) == 3


async def test_render_of_other_conversation_does_not_count(rig: Rig) -> None:
    await rig.shown()
    await rig.said()
    ctx = dataclasses.replace(rig.ctx(), conversation_id="d" * 32)
    err = await _refused(rig, ctx)
    assert err.details == {"guard": 3}


async def test_create_counts_as_check(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """fp_create's read-back check is recorded as the check of that geometry: with
    fp_render and no fp_check, guard 3 passes."""
    lib = rig.lib()

    async def engine(ctx: ToolContext, cs: Any) -> FootprintApplied:
        return FootprintApplied(
            lib_ids=[LIB_ID],
            files=[f"footprints/{REPO}.pretty/{NAME}.kicad_mod"],
            readback=rig.geometry,
        )

    monkeypatch.setattr(create_mod, "apply_footprint_change_set", engine)
    cs = FootprintChangeSet(
        tool="fp_create", logical=Logical(5), library=lib, target=rig.geometry, summary={}
    )
    await apply_footprint_create(rig.ctx(RUN1), cs)
    review = await FootprintReviewRepo(rig.db).get(TEST_CID, LIB_ID, rig.digest)
    assert review is not None and review.checked_at and review.rendered_at is None
    assert review.check_errors == 0 and review.run_id == RUN1
    await FootprintReviewRepo(rig.db).record(TEST_CID, LIB_ID, rig.digest, RUN1, rendered=True)
    await rig.said(at="2999-01-01T00:00:00.000Z")  # after both stamps of "now"
    cs2 = await prepare_approve(rig.ctx(), _args(rig))
    assert cs2.summary["lib_id"] == LIB_ID


async def test_fp_check_records_check(rig: Rig) -> None:
    out = await fp_check(rig.ctx(RUN1), {"lib_id": LIB_ID})
    assert out.data is not None and out.data["geometry_hash"] == rig.digest
    review = await FootprintReviewRepo(rig.db).get(TEST_CID, LIB_ID, rig.digest)
    assert review is not None and review.checked_at and review.rendered_at is None
    assert review.check_errors == 0
    # without a conversation (plain tool contexts) nothing is recorded
    await rig.db.execute("DELETE FROM footprint_reviews")
    await fp_check(dataclasses.replace(rig.ctx(RUN1), conversation_id=None), {"lib_id": LIB_ID})
    assert await FootprintReviewRepo(rig.db).get(TEST_CID, LIB_ID, rig.digest) is None


async def test_record_keeps_first_stamp(rig: Rig) -> None:
    repo = FootprintReviewRepo(rig.db)
    await rig.shown()
    await repo.record(TEST_CID, LIB_ID, rig.digest, RUN2, rendered=True, checked=True)
    got = await repo.get(TEST_CID, LIB_ID, rig.digest)
    assert got is not None
    assert (got.rendered_at, got.checked_at, got.run_id) == (T_RENDER, T_CHECK, RUN2)


# -- guards 4 and 6 -----------------------------------------------------------------------


async def test_same_turn_refused(rig: Rig) -> None:
    """Render, check and approve in one run: the run's message predates the render."""
    await rig.said(at=T_EARLY, run_id=RUN1)
    await rig.shown()
    assert await _guard(rig, ctx=rig.ctx(RUN1)) == 4


async def test_equal_timestamp_refused(rig: Rig) -> None:
    await rig.shown()
    await rig.said(at=T_CHECK)
    assert await _guard(rig) == 4


async def _written(
    rig: Rig, op: str, state: str, created: str, finalised: str | None, project: str = PID
) -> None:
    """A journalled write of the footprint with explicit timestamps."""
    repo = FootprintWriteRepo(rig.db)
    wid = await repo.insert_pending(
        project, RUN2, LIB_ID, f"footprints/{REPO}.pretty/{NAME}.kicad_mod", op, "x"
    )
    if state != "pending":
        await repo.finalise(wid, state, "y")
    await rig.db.execute(
        "UPDATE footprint_writes SET created_at = ?, finalised_at = ? WHERE id = ?",
        (created, finalised, wid),
    )


async def test_rewritten_after_message_refused(rig: Rig) -> None:
    """The attack: the user saw G earlier (first stamps kept), said something about G',
    and in the approval turn the agent writes G back and approves it with that quote.
    The write after the user's message refuses with guard 4 - in any project, also
    when only its finalisation is later, and on a tie."""
    await rig.shown()
    await rig.said()  # T_LATER
    T_AFTER = "2026-11-19T10:06:00.000Z"
    await _written(rig, "replace", "done", T_AFTER, T_AFTER, project="f" * 12)
    err = await _refused(rig)
    assert err.details == {"guard": 4} and "written" in err.why
    await rig.db.execute("DELETE FROM footprint_writes")
    await _written(rig, "create", "leftover", T_EARLY, T_AFTER)
    assert await _guard(rig) == 4
    await rig.db.execute("DELETE FROM footprint_writes")
    await _written(rig, "replace", "done", T_CHECK, T_LATER)  # tie
    assert await _guard(rig) == 4


async def test_write_before_message_or_compensated_ok(rig: Rig) -> None:
    """Writes before the user's message, a compensated write (the file kept its bytes),
    and approve / delete / other footprints' rows do not refuse."""
    await rig.shown()
    await rig.said()
    T_AFTER = "2026-11-19T10:06:00.000Z"
    await _written(rig, "create", "done", T_EARLY, T_EARLY)
    await _written(rig, "replace", "compensated", T_AFTER, T_AFTER)
    await _written(rig, "approve", "done", T_AFTER, T_AFTER)
    cs = await prepare_approve(rig.ctx(), _args(rig))
    assert cs.summary["lib_id"] == LIB_ID


async def test_message_of_another_run_does_not_count(rig: Rig) -> None:
    await rig.shown()
    await rig.said(run_id="run-other")
    assert await _guard(rig) == 4


@pytest.mark.parametrize(
    "metadata",
    [
        {"planId": "p" * 32, "event": "approve"},  # plans/service.py plan-execute message
        {"origin": "close-session"},  # sessions close prompt
        {},  # any other server-written user row
    ],
    ids=["plan-execute", "close-session", "no-origin"],
)
async def test_server_message_does_not_count(rig: Rig, metadata: dict[str, Any]) -> None:
    await rig.shown()
    await rig.said(metadata=metadata)
    assert await _guard(rig) == 4


async def test_notice_does_not_count(rig: Rig) -> None:
    await rig.shown()
    await rig.said(role="system", kind="notice", metadata={"origin": "user"})
    assert await _guard(rig) == 4


async def test_plan_running(rig: Rig) -> None:
    await rig.shown()
    await rig.said()
    await rig.plan("running")
    assert await _guard(rig) == 6


# -- guard 5 ------------------------------------------------------------------------------


async def test_quote_not_in_message(rig: Rig) -> None:
    await rig.shown()
    await rig.said()
    assert await _guard(rig, user_quote="ship it as it is") == 5


async def test_whitespace_normalised_quote_ok(rig: Rig) -> None:
    await rig.shown()
    await rig.said("The render\n looks   right,\tapprove it please.")
    cs = await prepare_approve(rig.ctx(), _args(rig, user_quote="  looks right,\n approve it "))
    assert cs.summary["lib_id"] == LIB_ID


@pytest.mark.parametrize("quote", ["e", "ok"])
async def test_trivial_quote_refused(rig: Rig, quote: str) -> None:
    await rig.shown()
    await rig.said("ok, the render is fine, please approve the footprint")
    assert await _guard(rig, user_quote=quote) == 5


async def test_quote_word_boundary(rig: Rig) -> None:
    assert not quote_in_message("ok go", "look good")
    assert not quote_in_message("ok good to go", "look good to go")
    assert quote_in_message("look good to go", "yes, look good to go!")
    assert not quote_in_message("Looks right", "looks right, approve")  # case-sensitive
    await rig.shown()
    await rig.said("look good")
    assert await _guard(rig, user_quote="ok go") == 5


async def test_short_whole_message_ok(rig: Rig) -> None:
    await rig.shown()
    await rig.said("ok!")
    cs = await prepare_approve(rig.ctx(), _args(rig, user_quote="ok!"))
    assert cs.summary["user_quote"] == "ok!"


# -- guard 7 ------------------------------------------------------------------------------


async def test_check_errors(rig: Rig) -> None:
    """Pad 1 without solder mask: `mask_missing` is an fp_check error."""
    pads = [
        {"number": "1", "type": "smd", "shape": "rect", "at": [-1.1, 0], "size": [1.3, 3.5],
         "layers": ["F.Cu", "F.Paste"]},
        {"number": "2", "type": "smd", "shape": "rect", "at": [1.1, 0], "size": [1.3, 3.5]},
    ]  # fmt: skip
    rig.geometry = _target(pads=pads)
    await rig.shown()
    await rig.said()
    err = await _refused(rig)
    assert err.code == "approval_not_allowed" and err.details is not None
    assert err.details["guard"] == 7 and err.details["check"]["passed"] is False


# -- the plan gates (index author call 13) --------------------------------------------------


def _plan_gate(rig: Rig) -> PlanGate:
    async def foreign_owner(ctx: ToolContext) -> None:
        return None

    plans = SimpleNamespace(
        repo=PlanRepo(rig.db), clear_step=lambda state: None, foreign_owner=foreign_owner
    )
    return PlanGate(rig.editing, cast(Any, plans))


async def test_approve_with_policy_always(rig: Rig) -> None:
    await EditingSettingsRepo(rig.db).put(PID, plan_policy="always")
    ctx = rig.ctx()
    for gate in (BudgetGate(rig.editing), _plan_gate(rig)):
        with pytest.raises(StructuredToolError) as err:
            await gate.before_prepare(ctx, "fp_create")  # an edit is refused
        assert err.value.code == "plan_step_required"
        await gate.before_prepare(ctx, "fp_approve")  # the approval is not
    await rig.shown()
    await rig.said()
    await prepare_approve(ctx, _args(rig))


async def _approve_with_open_plan(rig: Rig, status: str) -> None:
    await rig.plan(status)
    ctx = rig.ctx()
    gate = _plan_gate(rig)
    with pytest.raises(StructuredToolError) as err:
        await gate.before_prepare(ctx, "fp_create")
    assert err.value.code == "plan_open"
    await gate.before_prepare(ctx, "fp_approve")
    await rig.shown()
    await rig.said()
    await prepare_approve(ctx, _args(rig))


async def test_approve_with_paused_plan(rig: Rig) -> None:
    await _approve_with_open_plan(rig, "paused")


async def test_approve_with_proposed_plan(rig: Rig) -> None:
    await _approve_with_open_plan(rig, "proposed")


# -- library safety -----------------------------------------------------------------------


async def test_approve_symlinked_dir_refused(rig: Rig, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (rig.session.root / "footprints").symlink_to(elsewhere, target_is_directory=True)
    await rig.shown()
    await rig.said()
    err = await _refused(rig)
    assert err.code == "library_conflict"


async def test_approve_symlinked_file_refused(rig: Rig, tmp_path: Path) -> None:
    pretty = rig.lib().dir
    pretty.mkdir(parents=True)
    (tmp_path / "other.kicad_mod").write_bytes(b"opaque")  # never parsed
    (pretty / f"{NAME}.kicad_mod").symlink_to(tmp_path / "other.kicad_mod")
    await rig.shown()
    await rig.said()
    err = await _refused(rig)
    assert err.code == "library_conflict"


# -- apply: TOCTOU ----------------------------------------------------------------------


async def test_geometry_changed_between_prepare_and_apply(rig: Rig) -> None:
    """Prepare saw the approved geometry; the document apply opens holds another one (the
    seed instance of the fake): guard 2, nothing saved, the journal row compensated."""
    await rig.shown()
    await rig.said()
    ctx = rig.ctx()
    cs = await prepare_approve(ctx, _args(rig))
    pretty = rig.lib().dir
    pretty.mkdir(parents=True)
    file = pretty / f"{NAME}.kicad_mod"
    file.write_bytes(b"opaque")  # never parsed: the fake document serves the geometry
    rig.session.documents_ok.add(LIB_ID)
    assert cs.library is not None
    with pytest.raises(StructuredToolError) as err:
        async with library_lock(cs.library.dir):
            await apply_approve(ctx, cs)
    assert err.value.code == "approval_not_allowed"
    assert err.value.details == {"guard": 2}
    assert file.read_bytes() == b"opaque"
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state, r.geometry_hash) for r in rows] == [
        ("approve", "compensated", rig.digest)
    ]
    assert rows[0].pre_sha256 == rows[0].post_sha256
