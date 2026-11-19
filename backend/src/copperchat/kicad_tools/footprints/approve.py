"""`fp_approve` (footprints spec §7.7, F4b Task 1): record the user's chat approval of a
local footprint, but only with server-side evidence that they saw the current geometry.

`prepare` checks the seven guards (each refusal is `approval_not_allowed` with
`details.guard`):

1. the footprint is local and `needs_user_review` (no unfinished journal row);
2. `geometry_hash` is the current read-back hash;
3. this conversation rendered **and** checked that geometry (`footprint_reviews`);
4. the current run processed a message the user typed (`metadata.origin == "user"`,
   stamped only by the WebSocket submit) created after both;
5. `user_quote` is in that message (whitespace-normalised, on word boundaries, at least
   2 words and 8 non-space characters - or the whole message when that is shorter);
6. no plan of this conversation is `running`;
7. `fp_check` of the current read-back has no error.

`apply` (under the library lock the adapter takes) cleans up pending journal rows,
journals `approve`, re-reads the geometry in the open document (guard 2 again, TOCTOU),
then one commit (`needs_user_review = no`, a new `User review` field) + save, and reads
it back (the hash must not change). The quote goes to the change log only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from copperchat.changes.repo import ChangeEntry
from copperchat.db.repos.footprints import FootprintReviewRepo
from copperchat.db.repos.messages import USER_ORIGIN, MessageRepo, MessageRow
from copperchat.errors import DocumentInconsistent, StructuredToolError, attach_tool_notes
from copperchat.history.library_lock import assert_library_locked
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_session.session import _uncancellable
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import record_applied
from copperchat.kicad_tools.footprints import protos
from copperchat.kicad_tools.footprints.checks import CheckInput, run_checks, to_result
from copperchat.kicad_tools.footprints.engine import (
    FootprintChangeSet,
    FpStage,
    _drop,
    _file_checks,
    _finalise,
    _library_error,
    _recheck_row,
    _report,
    _sha_or_absent,
    cleanable_copy,
    cleanup_pending,
)
from copperchat.kicad_tools.footprints.journal import ABSENT, Journal, footprint_rel, sha_of
from copperchat.kicad_tools.footprints.model import (
    APPROVAL_FIELD,
    REVIEW_FIELD,
    FootprintGeometry,
    geometry_from_proto,
    geometry_hash,
    review_state,
)
from copperchat.kicad_tools.footprints.read import parse_lib_id, pending_lib_ids, require_enabled
from copperchat.library.local import LocalLibrary
from copperchat.library.service import library_for, local_footprint_file_safe
from copperchat.plans.repo import PlanRepo
from copperchat.tools.spec import ToolContext, ToolResult

if TYPE_CHECKING:
    from copperchat.kicad_session.footprint_doc import FootprintDocument

log = logging.getLogger(__name__)

TOOL = "fp_approve"
LOGICAL_CHANGES = 1  # spec §12
APPROVE_KEYS = frozenset({"lib_id", "geometry_hash", "user_quote"})
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
MAX_QUOTE_CHARS = 300
MIN_QUOTE_WORDS = 2  # index author call 15
MIN_QUOTE_CHARS = 8  # non-space characters


@dataclass(frozen=True)
class ApproveArgs:
    lib_id: str
    nickname: str
    name: str
    geometry_hash: str
    user_quote: str


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def parse_approve_args(args: dict[str, Any]) -> ApproveArgs:
    """The schema, again on the server: `lib_id`, a 64-hex `geometry_hash`, a
    `user_quote` of 1..300 characters (the word rules are guard 5)."""
    unknown = sorted(set(args) - APPROVE_KEYS)
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown}", "see the tool schema")
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    digest = args.get("geometry_hash")
    if not isinstance(digest, str) or not HASH_RE.match(digest):
        raise _invalid(
            "Invalid geometry_hash",
            "geometry_hash must be the 64 lower-case hex characters fp_render / fp_check report",
            "Copy geometry_hash from the fp_render result the user saw.",
        )
    quote = args.get("user_quote")
    if not isinstance(quote, str) or not 1 <= len(quote) <= MAX_QUOTE_CHARS:
        raise _invalid(
            "Invalid user_quote",
            f"user_quote must be a string of 1 to {MAX_QUOTE_CHARS} characters",
            "Copy the user's own words of approval from their message.",
        )
    return ApproveArgs(lib_id, nickname, name, digest, quote)


def refused(guard: int, what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(
        what, why, suggestion, code="approval_not_allowed", details={"guard": guard}
    )


# -- guard 5: the quote ----------------------------------------------------------------


def normalise(text: str) -> str:
    """Whitespace runs -> one space, trimmed (case and punctuation kept)."""
    return " ".join(text.split())


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def quote_in_message(quote: str, message: str) -> bool:
    """Guard 5 (spec §7.7, index author call 15): after whitespace normalisation the
    quote equals the whole message, or it holds at least 2 words and 8 non-space
    characters and occurs in the message starting and ending on word boundaries
    (case-sensitive). So "ok" in a longer message and "ok go" inside "look good" do
    not count, while a whole message "ok!" quoted as "ok!" does."""
    q, m = normalise(quote), normalise(message)
    if not q:
        return False
    if q == m:
        return True
    if len(q.split()) < MIN_QUOTE_WORDS or len(q.replace(" ", "")) < MIN_QUOTE_CHARS:
        return False
    start = m.find(q)
    while start >= 0:
        end = start + len(q)
        before_ok = start == 0 or not (_word(m[start - 1]) and _word(q[0]))
        after_ok = end == len(m) or not (_word(m[end]) and _word(q[-1]))
        if before_ok and after_ok:
            return True
        start = m.find(q, start + 1)
    return False


# -- guards 3 to 6: the evidence in the database ---------------------------------------


async def check_evidence(ctx: ToolContext, lib_id: str, digest: str, quote: str) -> None:
    """Guards 3-6 (spec §7.7) from the database; raises `approval_not_allowed`."""
    if ctx.editing is None or ctx.conversation_id is None:
        raise refused(
            3,
            f"{lib_id} cannot be approved here",
            "approvals are only recorded in a chat session, where CopperChat tracks what the "
            "user was shown",
            "Ask the user to approve the footprint in a CopperChat chat session.",
        )
    db = ctx.editing.db
    cid = ctx.conversation_id
    review = await FootprintReviewRepo(db).get(cid, lib_id, digest)
    if review is None or review.rendered_at is None or review.checked_at is None:
        missing = [
            tool
            for tool, at in (
                ("fp_render", None if review is None else review.rendered_at),
                ("fp_check", None if review is None else review.checked_at),
            )
            if at is None
        ]
        raise refused(
            3,
            f"{lib_id} was not shown to the user at this geometry",
            f"this session has no {' and no '.join(missing)} result for geometry "
            f"{digest[:8]} of {lib_id}",
            "Call fp_render and fp_check, show the render, the check and the source to the "
            "user, and wait for their answer in a later message.",
        )
    shown_at = max(review.rendered_at, review.checked_at)
    human = await _human_messages(ctx)
    later = [m for m in human if m.created_at > shown_at]
    if not later:
        why = (
            "this turn was not started by a message the user typed (plan execution, a "
            "session prompt or a notice do not count)"
            if not human
            else "the user's message of this turn was sent before the render and the check "
            "of this geometry, so they cannot have seen it"
        )
        raise refused(
            4,
            f"{lib_id} cannot be approved in this turn",
            why,
            "Show the render and the check to the user and end the turn; approve only after "
            "the user answers with an explicit OK in their next message.",
        )
    if not any(quote_in_message(quote, m.content) for m in later):
        raise refused(
            5,
            f"The quote is not the user's approval of {lib_id}",
            "user_quote must be the user's own words copied from their message of this turn "
            f"(at least {MIN_QUOTE_WORDS} words and {MIN_QUOTE_CHARS} characters on word "
            "boundaries, or their whole message)",
            "Quote the user's approval verbatim; if they did not approve clearly, ask them.",
        )
    if await PlanRepo(db).running_in_conversation(cid):
        raise refused(
            6,
            f"{lib_id} cannot be approved while a plan runs",
            "a plan of this session is running; an approval is a separate turn after the "
            "user's answer",
            "Finish the plan first, then ask the user for the approval.",
        )


async def _human_messages(ctx: ToolContext) -> list[MessageRow]:
    """The messages the user typed that the current run processed (guard 4)."""
    if ctx.run_id is None or ctx.editing is None or ctx.conversation_id is None:
        return []
    rows = await MessageRepo(ctx.editing.db).run_user_messages(ctx.conversation_id, ctx.run_id)
    return [m for m in rows if m.metadata.get("origin") == USER_ORIGIN]


# -- prepare ---------------------------------------------------------------------------


def _not_local(lib_id: str, why: str) -> StructuredToolError:
    return refused(
        1,
        f"{lib_id} cannot be approved",
        why,
        "Only footprints that fp_create made in this repository's local library and that "
        "wait for the user's review can be approved (see fp_list_local).",
    )


async def prepare_approve(ctx: ToolContext, args: dict[str, Any]) -> FootprintChangeSet:
    """Check every guard (spec §7.7); nothing is written. The library comes from
    `local_library(for_write=True)` (`library_conflict` for a symlinked path), the file
    is checked with `local_footprint_file_safe`."""
    parsed = parse_approve_args(args)
    require_enabled(ctx)
    lib_id, name = parsed.lib_id, parsed.name
    svc = library_for(ctx.session)
    info = await svc.local_library(for_write=True)
    assert info is not None  # for_write raises repo_root_unknown instead
    if info.row_conflict:
        raise StructuredToolError(
            "The local footprint library conflicts with the project's library table",
            info.row_conflict,
            "Ask the user to rename or remove the conflicting fp-lib-table row; nothing was "
            "written.",
            code="library_conflict",
        )
    lib = info.lib
    if parsed.nickname != lib.nickname:
        raise _not_local(lib_id, f"{parsed.nickname} is not this repository's local library")
    if not info.row_present:
        raise _not_local(lib_id, f"this project has no library row for {lib.nickname} yet")
    await asyncio.to_thread(local_footprint_file_safe, lib, name)
    if await cleanable_copy(ctx, lib, name):
        raise StructuredToolError(
            f"Footprint {lib_id} not found",
            f"{lib_id} is only an unfinished copy left by an earlier footprint call that "
            "stopped when the KiCad server died; the next footprint write removes it",
            "Create the footprint again with fp_create.",
            code="not_found",
        )
    geometry = await svc.footprint_geometry(lib_id)
    if geometry is None or geometry.scope != "local":
        raise StructuredToolError(
            f"Footprint {lib_id} not found",
            f"{lib_id} is not in the local library",
            "List the local footprints with fp_list_local.",
            code="not_found",
        )
    if lib_id in await pending_lib_ids(ctx):
        raise _not_local(
            lib_id,
            f"an earlier write of {lib_id} did not finish (the KiCad server stopped); it may "
            "be half-built until a later footprint write cleans the journal up",
        )
    state = review_state(geometry, pending=False)
    if state != "needs_user_review":
        why = (
            "it is approved already"
            if state == "approved"
            else "it has no Footprint source or needs_user_review field (not made by fp_create)"
        )
        raise _not_local(lib_id, why)
    digest = geometry_hash(geometry)
    if digest != parsed.geometry_hash:
        raise refused(
            2,
            f"{lib_id} changed since the user saw it",
            f"its current geometry is {digest[:8]}, the approval names {parsed.geometry_hash[:8]}",
            "Render and check the current footprint, show it to the user and ask again.",
        )
    await check_evidence(ctx, lib_id, digest, parsed.user_quote)
    report = await asyncio.to_thread(
        run_checks, CheckInput(geometry=geometry, local=True, pending=False)
    )
    if report.counts["error"]:
        raise StructuredToolError(
            f"{lib_id} fails fp_check",
            f"{report.counts['error']} error(s); an approved footprint must check clean",
            "Fix the footprint with fp_create(replace=true) and ask the user again.",
            code="approval_not_allowed",
            details={"guard": 7, "check": to_result(report)},
        )
    assert ctx.conversation_id is not None  # guard 3
    return FootprintChangeSet(
        tool=TOOL,
        logical=Logical(LOGICAL_CHANGES),
        summary={
            "lib_id": lib_id,
            "geometry_hash": digest,
            "user_quote": parsed.user_quote,
            "conversation_id": ctx.conversation_id,
        },
        fp_stages=[FpStage("approve", lib_id)],
        library=lib,
    )


# -- apply -----------------------------------------------------------------------------


def approval_text(conversation_id: str, digest: str, today: str | None = None) -> str:
    """The `User review` field (spec §7.7)."""
    day = today or datetime.now(UTC).date().isoformat()
    return f"approved {day}, conversation {conversation_id[:8]}, geometry {digest[:8]}"


async def _approve_commit(doc: FootprintDocument, value: str) -> None:
    """In the open commit: `needs_user_review` -> `no`, `User review` set (created as a
    hidden custom field, like the other tool-set fields)."""
    fields = {i.name: i for i in await doc.items() if i.DESCRIPTOR.name == "Field"}
    flag = fields.get(REVIEW_FIELD)
    if flag is None:
        raise RuntimeError(f"the footprint has no {REVIEW_FIELD} field")
    flag.text.text.text = "no"
    updated = [flag]
    review = fields.get(APPROVAL_FIELD)
    if review is not None:
        review.text.text.text = value
        updated.append(review)
    await doc.update(updated)
    if review is None:
        await doc.create([protos.custom_field_proto(flag, APPROVAL_FIELD, value)])


def _doc_geometry(instance: Any, lib_id: str) -> FootprintGeometry:
    return geometry_from_proto(instance, instance.definition.items, lib_id, "local")


async def _settle(
    journal: Journal, row: int | None, lib: LocalLibrary, lib_id: str, pre: str, saving: bool
) -> None:
    """After a failure: the file still holds its earlier bytes (or the call never
    reached its save) -> `compensated`; else the save changed it without a verified
    read-back -> `leftover` and `DocumentInconsistent` (the footprint is the user's: never
    to be deleted, History undo restores it)."""
    file = lib.dir / f"{lib_id.split(':', 1)[1]}.kicad_mod"
    sha = await _sha_or_absent(file)
    if not saving or sha == pre:
        await _finalise(journal, row, "compensated", sha)
        return
    await _finalise(journal, row, "leftover", sha)
    raise DocumentInconsistent(
        f"fp_approve of {lib_id} changed its file but could not verify it; left behind: {lib_id}",
        leftovers=[lib_id],
        replaced=[lib_id],
        footprint_only=True,
    )


async def _write_approval(
    ctx: ToolContext, lib: LocalLibrary, lib_id: str, digest: str, value: str
) -> FootprintGeometry:
    """The single stage (spec §9.3): journal `approve`, re-check the geometry in the open
    document, one commit + save, read back. Mirrors the F4a engine's conventions:
    `KicadUnavailable` (or a restart) skips the settling (the row stays pending)."""
    from kipy.errors import ApiError

    name = lib_id.split(":", 1)[1]
    file = lib.dir / f"{name}.kicad_mod"
    session = ctx.session
    svc = library_for(session)
    journal = Journal(ctx)
    generation = session.generation
    saving = False

    def same_server() -> bool:
        return session.generation == generation and session.server_alive

    def ensure_same_server() -> None:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during fp_approve; the journal keeps the "
                "write pending"
            )

    # FP-C8: path, project table, its row and the file checked again under the
    # library lock (nothing is written before)
    await asyncio.to_thread(_file_checks, lib, name)
    await _recheck_row(ctx, lib)
    pre = await sha_of(file, lib.repo_root)
    if pre == ABSENT:
        raise StructuredToolError(
            f"Footprint {lib_id} not found",
            f"{lib_id} was removed after the call was planned",
            "List the local footprints with fp_list_local; nothing was written.",
            code="not_found",
        )
    row = await journal.pending("approve", lib_id, footprint_rel(lib, name), pre, digest)

    async def settle() -> None:
        try:
            await _uncancellable(_settle(journal, row, lib, lib_id, pre, saving))
        except asyncio.CancelledError as again:
            if isinstance(again.__cause__, Exception):
                _report(ctx, again.__cause__)
            raise

    try:
        try:
            async with session.footprint_document(lib_id) as doc:
                current = _doc_geometry(await doc.footprint(), lib_id)
                if geometry_hash(current) != digest:
                    raise refused(
                        2,
                        f"{lib_id} changed since the user saw it",
                        "its geometry changed after the call was planned (the KiCad GUI or "
                        "another project of this repository edited it)",
                        "Render and check it again, show it to the user and ask again; "
                        "nothing was written.",
                    )
                if review_state(current, pending=False) != "needs_user_review":
                    raise _not_local(lib_id, "its review state changed after the call was planned")
                await doc.begin()
                try:
                    await _approve_commit(doc, value)
                    await doc.push(f"fp_approve {lib_id}")
                except BaseException:
                    if doc.open_commit:
                        await _drop(doc)
                    raise
                saving = True  # from here the file may hold this call's save
                ctx.library_touched.append(file)
                await doc.save()
        except (ApiError, RuntimeError, ValueError) as exc:  # KicadUnavailable passes
            what = f"Could not approve footprint {lib_id}"
            raise _library_error(what, exc, lib.repo_root) from exc
        finally:
            svc.invalidate_footprints(lib.nickname)
        ensure_same_server()
        readback = await svc.footprint_geometry(lib_id)  # the document is closed
        ensure_same_server()
        problem = None
        if readback is None:
            problem = "the footprint cannot be read back"
        elif geometry_hash(readback) != digest:
            problem = "its geometry hash changed on save"
        elif review_state(readback, pending=False) != "approved":
            problem = f"{REVIEW_FIELD} did not change to no"
        elif readback.fields.get(APPROVAL_FIELD) != value:
            problem = f"the {APPROVAL_FIELD} field reads back differently"
        if problem is not None:
            raise StructuredToolError(
                f"fp_approve: {lib_id} differs from the plan after saving",
                problem,
                "Tell the user; do not retry in this turn.",
                code="verification_failed",
            )
        assert readback is not None
        await journal.finalise(row, "done", await sha_of(file, lib.repo_root))
    except asyncio.CancelledError:
        # the turn is being stopped: settle the journal (to completion), keep cancelling
        if same_server():
            try:
                await settle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _report(ctx, exc)
        raise
    except KicadUnavailable:
        raise  # spec §9.3 server death: the journal row stays pending
    except BaseException as exc:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during fp_approve; the journal keeps the "
                "write pending"
            ) from exc
        await settle()
        raise
    svc.invalidate_footprints(lib.nickname)
    return readback


async def apply_approve(ctx: ToolContext, cs: Any) -> ToolResult:
    """`fp_approve.apply` inside `session.applying()`, the adapter holding the library
    lock: `cleanup_pending` first, then the approval stage; the result names the new
    review state; the change log gets `footprint_approved` with the quote."""
    if not isinstance(cs, FootprintChangeSet) or cs.library is None or cs.tool != TOOL:
        raise TypeError("apply_approve needs a planned fp_approve change set")
    lib = cs.library
    assert_library_locked(lib.dir)
    lib_id: str = cs.summary["lib_id"]
    digest: str = cs.summary["geometry_hash"]
    value = approval_text(cs.summary["conversation_id"], digest)
    async with ctx.session.applying():
        cleaned = await cleanup_pending(ctx, lib)
        try:
            readback = await _write_approval(ctx, lib, lib_id, digest, value)
        except BaseException as exc:
            attach_tool_notes(exc, cleaned)  # the cleanup's work stays: say so
            raise
    await record_applied(ctx, TOOL, [_change_entry(ctx, cs, value)])
    data: dict[str, Any] = {
        "approved": lib_id,
        "review": review_state(readback, pending=False),
        "geometry_hash": geometry_hash(readback),
        "user_review": value,
        "logical_changes": cs.logical.changes,
        "budget": None,
        "notes": cleaned,
    }
    # Library-only (spec §9.1): saved and journalled inside apply.
    return ToolResult(data=data, changed_docs=set(), library_changed={lib.nickname})


def _change_entry(ctx: ToolContext, cs: FootprintChangeSet, value: str) -> ChangeEntry:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    lib_id = cs.summary["lib_id"]
    digest = cs.summary["geometry_hash"]
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=TOOL,
        op="modify",
        kind="footprint_approved",
        uuid=lib_id,
        sheet_path="",
        ref=lib_id,
        before={"review": "needs_user_review", "geometry_hash": digest},
        after={
            "review": "approved",
            "geometry_hash": digest,
            "user_review": value,
            "user_quote": cs.summary["user_quote"],
        },
    )
