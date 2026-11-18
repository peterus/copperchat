"""Footprint write path (FP-C8, spec §9): the change-set types the footprint tools plan in
`prepare`, the staged write engine (S1 `ensure_library`, S2 `copy_seed`, S3 `rebuild`,
read-back, compensation, journal) and the history wiring of the local library (FP-C6:
library locator and listener).

`FootprintChangeSet` is a `ChangeSet` so the tool adapter, the gates and the change log
treat it like any two-phase edit; the schematic engine (`apply_change_set`) refuses it.

Locks: the adapter holds the library lock (`library_lock(cs.library.dir)`) around
`apply`; the engine runs every stage inside `session.applying()` and never takes either
again. Footprint documents take `_fp_lock` only (never `_commit_lock`), and the read-back
(`footprint_geometry`) runs after the document is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from copperchat.config import Settings
from copperchat.db.repos.footprints import FootprintWrite, FootprintWriteRepo
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.library_lock import assert_library_locked
from copperchat.history.store import LibraryLocation, LibraryLocator
from copperchat.kicad_session import (
    SEED_LIB_ID,
    KicadHandle,
    KicadSession,
    KicadSessionManager,
    KicadUnavailable,
)
from copperchat.kicad_session.launcher import kicad_template_dir
from copperchat.kicad_session.session import _finish, _uncancellable
from copperchat.kicad_tools.edit.changeset import ChangeSet, NetIntent, SheetRef, Stage
from copperchat.kicad_tools.footprints.journal import (
    ABSENT,
    Journal,
    PendingOutcome,
    classify_pending,
    file_sha256,
    footprint_rel,
    sha_of,
    table_rel,
)
from copperchat.kicad_tools.footprints.model import geometry_equal
from copperchat.library.local import LIBRARY_DESCRIPTION, LocalLibrary, derive_local_library
from copperchat.library.service import (
    library_for,
    local_footprint_file_safe,
    local_library_dir_safe,
)
from copperchat.projects import Project
from copperchat.repo_context.paths import repo_policy
from copperchat.tools.spec import ToolContext

if TYPE_CHECKING:
    from copperchat.kicad_session.footprint_doc import FootprintDocument
    from copperchat.kicad_tools.footprints.model import FootprintGeometry

log = logging.getLogger(__name__)

# Footprint change sets have no sheet; the base field stays a `SheetRef` (author call 8).
NO_SHEET = SheetRef(path="", page="", file="", name="<footprint>")

FpStageKind = Literal["ensure_library", "copy_seed", "rebuild", "approve", "delete"]


@dataclass(frozen=True)
class LibraryRowStage:
    """Add the project's row for the local library (S1 / the `library_row` pre-stage);
    nickname, URI and directory come from `library`."""

    library: LocalLibrary
    create_library: bool


@dataclass(frozen=True)
class FpStage:
    """One footprint write stage; `lib_id` is `<nick>:<name>` (empty for
    `ensure_library`)."""

    kind: FpStageKind
    lib_id: str = ""


@dataclass(frozen=True)
class FootprintPreImage:
    """What `replace` overwrites, read through the API in prepare (spec §9.5):
    serialized items and definition, plus the file's sha256 and the geometry hash that
    the engine re-checks under the library lock before it writes."""

    items: list[bytes]
    definition: bytes
    file_sha256: str
    geometry_hash: str


@dataclass(kw_only=True)
class FootprintChangeSet(ChangeSet):
    """A footprint write (spec §9.1). The schematic fields stay empty; `logical` carries
    the budget (`fp_create` 5). The three hooks at the end are set by tests only."""

    sheet: SheetRef = NO_SHEET
    stages: list[Stage] = field(default_factory=list)
    pre_images: dict[str, Any] = field(default_factory=dict)
    intent: NetIntent = field(default_factory=NetIntent)
    fp_stages: list[FpStage] = field(default_factory=list)
    target: FootprintGeometry | None = None
    pre_image: FootprintPreImage | None = None  # replace only
    library: LocalLibrary | None = None
    fp_applied: FootprintApplied | None = None  # set by the footprint engine
    fail_after: str | None = None  # test hook: raise InjectedFailure after a stage
    kill_after: str | None = None  # test hook: kill the api-server after a stage
    fail_compensation: bool = False  # test hook: DeleteLibraryItem raises
    # the symbol pins prepare checked against (fp_check on the read-back uses them too)
    check_pins: list[str] | None = None
    polarized: bool = False


@dataclass
class FootprintApplied:
    """What a footprint change set wrote: the lib ids, the repo-relative files, whether
    this call added the project's library row / created the library directory, the
    read-back geometry and notes."""

    lib_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    row_added: bool = False
    dir_created: bool = False
    readback: FootprintGeometry | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)


# -- the staged write engine (FP-C8, spec §9.3) --------------------------------------------

ROW_LOAD_TIMEOUT_S = 30.0  # wait for the new project row's library to load (S1 read-back)
VERIFY_SHIFT_MM = 0.002  # `fail_after="verify"`: pad 1 of the read-back moved by 2 um
_DIFFS_SHOWN = 20


class InjectedFailure(StructuredToolError):
    """A failure injected by a test hook (`FootprintChangeSet.fail_after`); never raised
    outside tests."""

    def __init__(self, stage: str) -> None:
        super().__init__(
            f"Injected failure after {stage}",
            "a test hook asked the footprint engine to fail here",
            "Tests only.",
            code="injected",
        )


def _library_error(what: str, exc: BaseException) -> StructuredToolError:
    reason = str(exc) or type(exc).__name__
    return StructuredToolError(
        what,
        f"KiCad refused it: {reason[:300]}",
        "Nothing is left behind (the call was rolled back); check fp_list_local and retry "
        "once, then tell the user.",
        code="library_error",
    )


def _norm_uri(uri: str) -> str:
    return uri.strip().replace("\\", "/").rstrip("/")


def _project_rows(h: KicadHandle) -> dict[str, str]:
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    table = h.kicad.get_library_table(LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_PROJECT)
    return {str(r.nickname): str(r.uri) for r in table.rows}


def _add_row(lib: LocalLibrary, create_library: bool, h: KicadHandle) -> bool:
    """`AddLibraryTableEntry` of the local row; False when a row of that nickname with
    the same URI is there already (`LCS_ALREADY_EXISTS`, nothing written)."""
    from kipy.errors import LibraryCommandError
    from kipy.proto.common.types import LibraryTableScope, LibraryType
    from kipy.proto.common.types.library_types_pb2 import LibraryCommandStatus

    try:
        h.kicad.add_library_table_entry(
            LibraryType.LT_FOOTPRINT,
            LibraryTableScope.LTS_PROJECT,
            lib.nickname,
            lib.uri,
            description=LIBRARY_DESCRIPTION,
            create_library=create_library,
        )
    except LibraryCommandError as exc:
        if exc.code == LibraryCommandStatus.Code.LCS_ALREADY_EXISTS:
            uri = _project_rows(h).get(lib.nickname)
            if uri is not None and _norm_uri(uri) == _norm_uri(lib.uri):
                return False
        raise
    return True


def _row_problem(lib: LocalLibrary, h: KicadHandle) -> str | None:
    """S1 read-back (spec §9.2): the project table has the row with the local URI and
    KiCad reports the library `LLS_LOADED`; else what is wrong."""
    from kipy.proto.common.types import LibraryTableScope, LibraryType
    from kipy.proto.common.types.library_types_pb2 import LibraryLoadStatus

    uri = _project_rows(h).get(lib.nickname)
    if uri is None or _norm_uri(uri) != _norm_uri(lib.uri):
        return f"the project table has no row {lib.nickname!r} -> {lib.uri!r} afterwards"

    def loaded() -> Any:
        statuses = h.kicad.wait_for_libraries(
            LibraryType.LT_FOOTPRINT,
            interval_ms=100,
            timeout_s=ROW_LOAD_TIMEOUT_S,
            scope=LibraryTableScope.LTS_PROJECT,
        )
        return next((s for s in statuses if s.nickname == lib.nickname), None)

    try:
        status = loaded()
        if status is not None and status.status == LibraryLoadStatus.LLS_INVALID:
            # A new row is not loaded yet (LLS_INVALID, verified F4a Task 3 for a row
            # over an existing directory); GetLibraryItems loads it on demand.
            h.kicad.get_library_footprints([lib.nickname])
            status = loaded()
    except TimeoutError:
        return f"library {lib.nickname!r} did not finish loading in {ROW_LOAD_TIMEOUT_S:g} s"
    if status is None or status.status != LibraryLoadStatus.LLS_LOADED:
        state = "missing" if status is None else LibraryLoadStatus.Name(status.status)
        detail = "" if status is None else f" ({status.error_message or ''})"
        return f"library {lib.nickname!r} is {state} after adding the row{detail}"
    return None


def _delete_row(nickname: str, h: KicadHandle) -> None:
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    h.kicad.delete_library_table_entry(
        LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_PROJECT, nickname
    )


def _delete_item(nickname: str, name: str, h: KicadHandle) -> None:
    from kipy.proto.common.types import LibraryType

    h.kicad.delete_library_item(LibraryType.LT_FOOTPRINT, nickname, name)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _rmdir_if_empty(path: Path) -> bool:
    """`rmdir` (never a recursive delete): only an empty real directory goes."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    try:
        path.rmdir()
    except OSError:  # not empty (or vanished): kept
        return False
    return True


@dataclass
class _RowProgress:
    """S1 bookkeeping, filled while the stage runs (compensation reads it)."""

    journal_id: int | None = None
    dir_existed: bool = True
    attempted: bool = False
    added: bool = False  # this call wrote the row
    final: bool = False  # journal row finalised after the read-back


async def _ensure_row(ctx: ToolContext, stage: LibraryRowStage, progress: _RowProgress) -> None:
    lib = stage.library
    assert_library_locked(lib.dir)
    await asyncio.to_thread(local_library_dir_safe, lib)
    session = ctx.session
    from kipy.errors import ApiError

    ctx.library_touched.append(lib.table)
    progress.attempted = True
    try:
        progress.added = await session.call(partial(_add_row, lib, stage.create_library))
        problem = await session.call(
            partial(_row_problem, lib),
            timeout=ROW_LOAD_TIMEOUT_S + session.settings.kicad_api_timeout_s,
        )
    except ApiError as exc:
        raise _library_error(
            f"Could not add the local footprint library {lib.nickname} to the project", exc
        ) from exc
    finally:
        library_for(session).invalidate_footprints(lib.nickname)
    if problem is not None:
        raise StructuredToolError(
            f"The local footprint library {lib.nickname} is not usable after adding it",
            problem,
            "Nothing is left behind (the call was rolled back); tell the user.",
            code="verification_failed",
        )


async def run_library_row(
    ctx: ToolContext, stage: LibraryRowStage, *, verify: str | None = None
) -> bool:
    """Add the project's row for the local library (S1 / the `library_row` pre-stage):
    `AddLibraryTableEntry(create_library=stage.create_library)`, then the read-back
    (`GetLibraryTable` has the row, `GetLibraryStatuses` says `LLS_LOADED`). True when
    this call added the row, False when a row with the same nickname and URI was
    there already. With `verify` (a lib id), the footprint must exist afterwards
    (`not_found` otherwise). Lock-free: the caller holds the library lock (asserted);
    `local_library_dir_safe` runs first. Not journalled here; the caller compensates."""
    progress = _RowProgress()
    await _ensure_row(ctx, stage, progress)
    if verify is not None and not await library_for(ctx.session).has_footprint(verify):
        raise StructuredToolError(
            f"Footprint {verify} not found",
            f"the library row {stage.library.nickname} is there, but KiCad does not list {verify}",
            "Check the name with fp_list_local.",
            code="not_found",
        )
    return progress.added


def _file_checks(lib: LocalLibrary, name: str) -> None:
    """FP-C1 under the library lock: the library path, the project table and the target
    file (no symlink, regular file) - `library_conflict` otherwise."""
    local_library_dir_safe(lib)
    local_footprint_file_safe(lib, name)


async def _recheck_name(ctx: ToolContext, lib: LocalLibrary, name: str) -> None:
    """Before S2 (spec §9.3, author call): the name must still be free - neither in the
    API listing (the row exists now) nor as `<dir>/<name>.kicad_mod` on disk
    (`SaveCopyOfDocument` would overwrite it silently)."""
    await asyncio.to_thread(_file_checks, lib, name)
    names = await library_for(ctx.session).footprint_names(lib.nickname)
    on_disk = await asyncio.to_thread(_lexists, lib.dir / f"{name}.kicad_mod")
    if name in names or on_disk:
        where = "in KiCad's listing of" if name in names else "on disk in"
        raise StructuredToolError(
            f"Footprint {lib.nickname}:{name} already exists",
            f"a footprint named {name!r} appeared {where} the local library after the "
            "call was planned (the KiCad GUI or another project of this repository may "
            "have created it)",
            "Pick another name, or pass replace=true to rebuild it (needs the user's OK). "
            "Nothing was written.",
            code="already_exists",
        )


async def _copy_seed(ctx: ToolContext, lib_id: str) -> None:
    """S2: `SaveCopyOfDocument(seed -> lib_id)`."""
    from kipy.errors import ApiError

    try:
        async with ctx.session.footprint_document(SEED_LIB_ID) as doc:
            await doc.save_copy(lib_id)
    except (ApiError, RuntimeError) as exc:
        raise _library_error(f"Could not copy the seed footprint to {lib_id}", exc) from exc


async def _rebuild_commit(doc: FootprintDocument, target: FootprintGeometry) -> None:
    """The S3 commit: delete every child but the footprint's own fields, create the
    target's pads, shapes, user texts and custom fields, set Reference / Value /
    Datasheet, then the footprint-level properties and the 3D model link."""
    from copperchat.kicad_tools.footprints import protos

    items = await doc.items()
    kind = [i.DESCRIPTOR.name for i in items]
    template_pad = next((i for i, k in zip(items, kind, strict=True) if k == "Pad"), None)
    template_text = next((i for i, k in zip(items, kind, strict=True) if k == "BoardText"), None)
    fields = {i.name: i for i, k in zip(items, kind, strict=True) if k == "Field"}
    if template_pad is None or not {"Reference", "Value", "Datasheet"} <= fields.keys():
        raise RuntimeError("the seed copy has no pad or misses a mandatory field")
    stale = [i.id.value for i, k in zip(items, kind, strict=True) if k != "Field"]
    stale += [f.text.id.value for n, f in fields.items() if n not in protos.MANDATORY_FIELDS]
    if stale:
        await doc.delete(stale)

    texts = {t.kind: t for t in target.texts if t.kind != "user"}
    new: list[Any] = [protos.pad_proto(template_pad, p) for p in target.pads]
    new += [protos.shape_proto(s) for s in target.shapes]
    new += [protos.user_text_proto(template_text, t) for t in target.texts if t.kind == "user"]
    template_field = fields["Datasheet"]
    new += [
        protos.custom_field_proto(template_field, n, v)
        for n, v in target.fields.items()
        if n not in protos.MANDATORY_FIELDS
    ]
    await doc.create(new)

    reference, value, datasheet = fields["Reference"], fields["Value"], fields["Datasheet"]
    protos.field_text(reference, texts.get("reference"), target.fields.get("Reference", "REF**"))
    protos.field_text(value, texts.get("value"), target.fields.get("Value", ""))
    protos.field_text(datasheet, None, target.fields.get("Datasheet", ""))
    await doc.update([reference, value, datasheet])

    instance = await doc.footprint()
    definition = instance.definition
    definition.attributes.description = target.description
    definition.attributes.keywords = target.keywords
    attrs = instance.attributes
    attrs.mounting_style = protos.MOUNTING[target.mounting]
    attrs.exclude_from_bill_of_materials = target.exclude_from_bom
    attrs.exclude_from_position_files = target.exclude_from_pos
    attrs.do_not_populate = target.dnp
    kept = [a for a in definition.items if not a.type_url.endswith("Footprint3DModel")]
    del definition.items[:]
    definition.items.extend(kept)
    for model in target.models:
        packed = definition.items.add()
        packed.Pack(protos.model_proto(model))
    await doc.update_footprint(instance)


async def _drop(doc: FootprintDocument) -> None:
    """Drop the open S3 commit to completion (`_finish`, as `KicadSession.commit` does):
    a further cancel waits for the drop and then wins; a failed drop only logs (the
    document's close drops again)."""
    try:
        _, cancelled = await _finish(doc.drop())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("dropping the footprint commit of %s failed: %s", doc.lib_id, exc)
        return
    if cancelled:
        raise asyncio.CancelledError


async def _rebuild(
    ctx: ToolContext, lib_id: str, target: FootprintGeometry, progress: _CreateProgress
) -> None:
    """S3: open the copy, one commit (dropped on any failure before the push), save.
    The document is closed before this returns."""
    from kipy.errors import ApiError

    try:
        async with ctx.session.footprint_document(lib_id) as doc:
            await doc.begin()
            try:
                await _rebuild_commit(doc, target)
                await doc.push(f"fp_create {lib_id}")
            except BaseException:
                if doc.open_commit:
                    await _drop(doc)
                raise
            progress.s3_saving = True  # from here the file may hold S3's save
            await doc.save()
    except (ApiError, RuntimeError, ValueError) as exc:
        raise _library_error(f"Could not build footprint {lib_id}", exc) from exc


def _shift_pad_one(g: FootprintGeometry) -> FootprintGeometry:
    """`fail_after="verify"`: the read-back's pad 1 moved by `VERIFY_SHIFT_MM` in x."""
    pads = list(g.pads)
    for i, pad in enumerate(pads):
        if pad.number == "1":
            pads[i] = dataclasses.replace(pad, at=(pad.at[0] + VERIFY_SHIFT_MM, pad.at[1]))
            break
    return dataclasses.replace(g, pads=tuple(pads))


async def _kill_server(session: KicadSession) -> None:
    """`kill_after`: SIGKILL the api-server (a crash), and reap it."""
    proc = session.process
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(proc.pid, signal.SIGKILL)
    await proc.wait()


@dataclass
class _CreateProgress:
    row: _RowProgress = field(default_factory=_RowProgress)
    journal_id: int | None = None  # the create row
    attempted: bool = False  # S2 started (the footprint file may exist)
    post_s2: str | None = None  # sha of this call's seed copy
    s3_saving: bool = False  # S3 reached its save
    saved: str | None = None  # sha after S3's save
    final: bool = False

    def ours(self, sha: str) -> bool:
        """`sha` is a state of the footprint file this call wrote (S2 or S3)."""
        return sha != ABSENT and sha in (self.post_s2, self.saved)


async def apply_footprint_change_set(ctx: ToolContext, cs: FootprintChangeSet) -> FootprintApplied:
    """Write a footprint change set (spec §9.3) inside `session.applying()`; the caller
    (the tool adapter) holds the library lock of `cs.library`.

    First the cleanup of this project's `pending` rows (`cleanup_pending`, spec §9.3),
    then, for a new name: S1 `ensure_library` (only when planned) -> API name re-check -> S2
    `copy_seed` -> S3 `rebuild` -> read-back (`geometry_equal(target, read-back) == []`).
    Every failure compensates in reverse, shielded; `KicadUnavailable` or a server
    restart skips the compensation (journal rows stay `pending`). A failed compensation
    raises `DocumentInconsistent(leftovers=..., footprint_only=True)`."""
    if not isinstance(cs, FootprintChangeSet) or cs.library is None or cs.target is None:
        raise TypeError("apply_footprint_change_set needs a planned FootprintChangeSet")
    lib = cs.library
    assert_library_locked(lib.dir)
    if cs.pre_image is not None or cs.summary.get("replace"):
        raise StructuredToolError(
            f"{cs.tool} replace=true is not available yet",
            "rebuilding an existing local footprint is not implemented in this version",
            "Create the footprint under a new name instead; nothing was written.",
            code="invalid_argument",
        )
    async with ctx.session.applying():
        cleaned = await cleanup_pending(ctx, lib)  # first, before the tool's own stages
        applied = await _apply_create(ctx, cs, lib)
    applied.notes[:0] = cleaned
    cs.fp_applied = applied
    return applied


async def _apply_create(
    ctx: ToolContext, cs: FootprintChangeSet, lib: LocalLibrary
) -> FootprintApplied:
    assert cs.target is not None
    target = cs.target
    lib_id = target.lib_id
    name = lib_id.split(":", 1)[1]
    file = lib.dir / f"{name}.kicad_mod"
    session = ctx.session
    svc = library_for(session)
    journal = Journal(ctx)
    progress = _CreateProgress()
    generation = session.generation
    need_row = any(s.kind == "ensure_library" for s in cs.fp_stages)

    def same_server() -> bool:
        return session.generation == generation and session.server_alive

    def ensure_same_server() -> None:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during the footprint write; the journal "
                "keeps the write pending"
            )

    async def after(stage: str) -> None:
        if cs.kill_after == stage:
            await _kill_server(session)
        ensure_same_server()
        if cs.fail_after == stage:
            raise InjectedFailure(stage)

    async def rollback() -> None:
        """Compensate to completion; a Stop meanwhile wins (chained, never swallowed)
        and a compensation failure under it still reaches the turn."""
        try:
            await _uncancellable(_compensate(ctx, cs, lib, name, progress, journal))
        except asyncio.CancelledError as again:
            if isinstance(again.__cause__, Exception):
                _report(ctx, again.__cause__)
            raise

    try:
        # FP-C8: the library path, the project table and the target file are checked
        # under the library lock before the first write
        await asyncio.to_thread(_file_checks, lib, name)
        if need_row:
            row = progress.row
            row.dir_existed = await asyncio.to_thread(_lexists, lib.dir)
            row.journal_id = await journal.pending(
                "row_add", lib.nickname, table_rel(lib), await sha_of(lib.table)
            )
            stage = LibraryRowStage(library=lib, create_library=not row.dir_existed)
            await _ensure_row(ctx, stage, row)
            # a row that was there already (same URI) wrote nothing: nothing to undo
            state = "done" if row.added else "compensated"
            await journal.finalise(row.journal_id, state, await sha_of(lib.table))
            row.final = True
            await after("ensure_library")
        await _recheck_name(ctx, lib, name)
        progress.journal_id = await journal.pending(
            "create", lib_id, footprint_rel(lib, name), await sha_of(file)
        )
        progress.attempted = True
        ctx.library_touched.append(file)

        async def record_s2() -> None:
            # Needs no server: the seed copy's hash goes into the journal also when
            # KiCad died right after (or while) saving it, so the next write's cleanup
            # can recognise it (spec §9.3 point 1). The re-check proved the name
            # absent under the library lock, so a file there now is this call's copy.
            sha = await _sha_or_absent(file)
            if sha != ABSENT:
                progress.post_s2 = sha
                await journal.post_s2(progress.journal_id, sha)

        try:
            await _copy_seed(ctx, lib_id)
        except BaseException:
            try:
                await record_s2()
            except Exception as exc:  # the stage's own error wins
                log.warning("recording the seed copy of %s failed: %s", lib_id, exc)
            raise
        await record_s2()
        svc.invalidate_footprints(lib.nickname)
        if progress.post_s2 is None:
            ensure_same_server()
            raise StructuredToolError(
                f"Could not copy the seed footprint to {lib_id}",
                f"KiCad reported success, but {footprint_rel(lib, name)} is not on disk",
                "Nothing is left behind; tell the user.",
                code="verification_failed",
            )
        await after("copy_seed")
        try:
            await _rebuild(ctx, lib_id, target, progress)
        finally:
            if progress.s3_saving:
                progress.saved = await _sha_or_absent(file)
        svc.invalidate_footprints(lib.nickname)
        await after("rebuild_saved")
        readback = await svc.footprint_geometry(lib_id)  # the document is closed
        ensure_same_server()
        if readback is not None and cs.fail_after == "verify":
            readback = _shift_pad_one(readback)
        diffs = (
            ["the footprint cannot be read back"]
            if readback is None
            else geometry_equal(target, readback)
        )
        if diffs:
            raise StructuredToolError(
                f"{cs.tool}: the footprint KiCad saved differs from the plan; it was removed",
                "; ".join(diffs[:6]),
                "Nothing was written. Check the input (pads, shapes, fields) and retry "
                "once; if it fails again, tell the user.",
                code="verification_failed",
                details={"diffs": diffs[:_DIFFS_SHOWN], "diff_count": len(diffs)},
            )
        assert readback is not None
        await journal.finalise(progress.journal_id, "done", await sha_of(file))
        progress.final = True
    except asyncio.CancelledError:
        # The turn is being stopped: roll back (to completion), then keep cancelling.
        if same_server():
            try:
                await rollback()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _report(ctx, exc)
        raise
    except KicadUnavailable:
        raise  # spec §9.3 server death: no compensation, the journal rows stay pending
    except BaseException as exc:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during the footprint write; the journal "
                "keeps the write pending"
            ) from exc
        await rollback()
        raise
    svc.invalidate_footprints(lib.nickname)
    return FootprintApplied(
        lib_ids=[lib_id],
        files=[footprint_rel(lib, name)],
        row_added=progress.row.added,
        dir_created=progress.row.added and not progress.row.dir_existed,
        readback=readback,
    )


def _report(ctx: ToolContext, exc: BaseException) -> None:
    """A compensation that failed while the call was being cancelled (the adapter only
    sees the CancelledError, so the turn is told here)."""
    if isinstance(exc, DocumentInconsistent) and ctx.on_document_inconsistent is not None:
        ctx.on_document_inconsistent(exc)
    elif isinstance(exc, KicadUnavailable) and ctx.on_kicad_unavailable is not None:
        ctx.on_kicad_unavailable(exc)
    else:
        log.error("rolling back a footprint write after a cancellation failed: %s", exc)


async def _compensate(
    ctx: ToolContext,
    cs: FootprintChangeSet,
    lib: LocalLibrary,
    name: str,
    progress: _CreateProgress,
    journal: Journal,
) -> None:
    """Undo S3/S2/S1 in reverse (spec §9.3). An open S3 commit was dropped already (the
    document is closed). S2: `DeleteLibraryItem`, only of a file this call wrote (its
    hash is the seed copy's or S3's save; the GUI is not under the library lock). S1:
    `DeleteLibraryTableEntry` and `rmdir` of the library dir if this call's row
    created it and it is empty. Rows become `compensated`; when a step fails (or the
    file is not this call's), it and every earlier stage stay as they are
    (`leftover`), and `DocumentInconsistent` names them - also when the journal itself
    cannot be updated. `KicadUnavailable` passes through (rows stay `pending`)."""
    session = ctx.session
    svc = library_for(session)
    lib_id = f"{lib.nickname}:{name}"
    file = lib.dir / f"{name}.kicad_mod"
    leftovers: list[str] = []
    failure: BaseException | None = None

    if progress.attempted and not progress.final:
        ctx.library_touched.append(file)
        try:
            if await asyncio.to_thread(_lexists, file):
                if not progress.ours(await _sha_or_absent(file)):
                    raise RuntimeError(
                        f"{footprint_rel(lib, name)} is not the file this call wrote "
                        "(changed meanwhile); it is not deleted"
                    )
                if cs.fail_compensation:
                    raise RuntimeError("injected compensation failure (DeleteLibraryItem)")
                await session.call(partial(_delete_item, lib.nickname, name))
            svc.invalidate_footprints(lib.nickname)
            if await asyncio.to_thread(_lexists, file):
                raise RuntimeError(f"{footprint_rel(lib, name)} is still on disk")
        except KicadUnavailable:
            raise
        except Exception as exc:
            log.warning("compensating S2 of %s failed: %s", lib_id, exc)
            failure = exc
            leftovers.append(lib_id)
            await _finalise(journal, progress.journal_id, "leftover", await _sha_or_absent(file))
        else:
            await _finalise(journal, progress.journal_id, "compensated", ABSENT)

    row = progress.row
    if row.attempted:
        if failure is not None and row.added:
            leftovers.append(f"row {lib.nickname}")
            await _finalise(journal, row.journal_id, "leftover", await _sha_or_absent(lib.table))
        else:
            ctx.library_touched.append(lib.table)
            try:
                if row.added:
                    await session.call(partial(_delete_row, lib.nickname))
                # only a directory this call's own row created
                if row.added and not row.dir_existed:
                    await asyncio.to_thread(_rmdir_if_empty, lib.dir)
                svc.invalidate_footprints(lib.nickname)
            except KicadUnavailable:
                raise
            except Exception as exc:
                log.warning("compensating S1 of %s failed: %s", lib.nickname, exc)
                failure = exc
                leftovers.append(f"row {lib.nickname}")
                await _finalise(
                    journal, row.journal_id, "leftover", await _sha_or_absent(lib.table)
                )
            else:
                await _finalise(
                    journal, row.journal_id, "compensated", await _sha_or_absent(lib.table)
                )
    if failure is not None:
        raise DocumentInconsistent(
            f"undoing the partial {cs.tool} of {lib_id} failed: {failure}; left behind: "
            + ", ".join(leftovers),
            leftovers=leftovers,
            footprint_only=True,
        ) from failure


async def _finalise(journal: Journal, row: int | None, state: str, sha: str) -> None:
    """`journal.finalise` during compensation: a database failure is logged, never
    raised, so the compensation's outcome (and its leftovers) is still reported."""
    try:
        await journal.finalise(row, state, sha)
    except Exception:
        log.exception("journalling footprint write %s as %s failed", row, state)


async def _sha_or_absent(path: Path) -> str:
    """The file's hash for a journal row; a file that is not a regular file is
    recorded as absent (the row only needs to tell it changed)."""
    try:
        return await sha_of(path)
    except StructuredToolError:
        return ABSENT


# -- pending cleanup after a server death (spec §9.3, FP-C8) ------------------------------


def _own_copy(lib: LocalLibrary, row: FootprintWrite) -> str | None:
    """The footprint name of a `create` / `replace` row of `lib` (its lib id and file
    are `<nick>:<name>` and `<dir>/<name>.kicad_mod`), else None."""
    nick, sep, name = row.lib_id.partition(":")
    if not sep or nick != lib.nickname or not name or "/" in name:
        return None
    return name if row.file_rel == footprint_rel(lib, name) else None


async def _pending_outcomes(ctx: ToolContext, lib: LocalLibrary) -> list[PendingOutcome]:
    """`classify_pending` over this project's `pending` rows (read-only). A `delete` is
    kept only for a copy in `lib` that no other project journalled a write to
    afterwards (cross-project guard, spec §10); otherwise it becomes `leftover`."""
    if ctx.editing is None:
        return []
    repo = FootprintWriteRepo(ctx.editing.db)
    rows = await repo.pending(ctx.project.id)
    if not rows:
        return []

    def current(file_rel: str) -> str:
        try:
            return file_sha256(lib.repo_root / file_rel)
        except StructuredToolError:  # not a regular file: never equal to a copy's hash
            return ABSENT

    outcomes = await asyncio.to_thread(classify_pending, rows, current)
    out: list[PendingOutcome] = []
    for o in outcomes:
        if o.action == "delete" and (
            _own_copy(lib, o.row) is None
            or await repo.later_foreign(o.row.file_rel, ctx.project.id, o.row.id)
        ):
            o = dataclasses.replace(o, action="leftover")
        out.append(o)
    return out


async def cleanable_copy(ctx: ToolContext, lib: LocalLibrary, name: str) -> bool:
    """`prepare`'s view of the cleanup (read-only, spec §9.3): True when the next write's
    cleanup would delete `<nick>:<name>` (an untouched seed copy of a call the server
    died in). `fp_create` counts it as free; `replace` (and F4b's approve / delete) as
    `not_found`."""
    lib_id = f"{lib.nickname}:{name}"
    return any(
        o.action == "delete" and o.row.lib_id == lib_id for o in await _pending_outcomes(ctx, lib)
    )


def _cleaned_note(lib_id: str) -> dict[str, Any]:
    return {
        "kind": "pending_cleaned",
        "lib_id": lib_id,
        "message": f"Removed {lib_id}: an unfinished copy left by an earlier footprint call "
        "that stopped when the KiCad server died. Nothing else was changed.",
    }


def _leftover_note(row: FootprintWrite) -> dict[str, Any]:
    if row.op == "row_add":
        name = f"row {row.lib_id}"
        what = (
            f"The library row {row.lib_id} from an earlier call that stopped when the "
            "KiCad server died"
        )
    else:
        name = row.lib_id
        what = (
            f"{row.lib_id}, written by an earlier {row.op} that stopped when the KiCad "
            "server died (it changed since, or cannot be matched),"
        )
    return {
        "kind": "pending_leftover",
        "lib_id": name,
        "op": row.op,
        "message": f"{what} is kept as it is. It may be half-built: check it with fp_get / "
        "fp_check and tell the user; History undo of that turn or fp_delete removes it.",
    }


async def cleanup_pending(ctx: ToolContext, lib: LocalLibrary) -> list[dict[str, Any]]:
    """Process this project's `pending` journal rows (spec §9.3 point 2) at the start of
    a writing footprint `apply`: under the library lock (asserted) and inside
    `session.applying()`. An untouched seed copy (`classify_pending` -> `delete`) is
    removed with `DeleteLibraryItem` and its row becomes `compensated`; everything
    else - and a copy whose delete fails - becomes `leftover` with its current hash and
    is never deleted. Deleted files go to `ctx.library_touched`. Returns the notes
    (`pending_cleaned` / `pending_leftover`). `KicadUnavailable` passes through (the
    remaining rows stay `pending`)."""
    assert_library_locked(lib.dir)
    outcomes = await _pending_outcomes(ctx, lib)
    if not outcomes:
        return []
    await asyncio.to_thread(local_library_dir_safe, lib)
    session = ctx.session
    svc = library_for(session)
    journal = Journal(ctx)
    notes: list[dict[str, Any]] = []
    for o in outcomes:
        row = o.row
        sha = o.current_sha256
        if o.action == "delete":
            name = _own_copy(lib, row)
            assert name is not None
            file = lib.dir / f"{name}.kicad_mod"
            try:
                await asyncio.to_thread(local_footprint_file_safe, lib, name)
                if await _sha_or_absent(file) != row.post_s2_sha256:
                    raise RuntimeError("the copy changed after it was classified")
                ctx.library_touched.append(file)
                try:
                    await session.call(partial(_delete_item, lib.nickname, name))
                finally:
                    svc.invalidate_footprints(lib.nickname)
                if await asyncio.to_thread(_lexists, file):
                    raise RuntimeError(f"{row.file_rel} is still on disk")
            except KicadUnavailable:
                raise
            except Exception as exc:
                log.warning("cleaning up the pending copy %s failed: %s", row.lib_id, exc)
                sha = await _sha_or_absent(file)
            else:
                await _finalise(journal, row.id, "compensated", ABSENT)
                notes.append(_cleaned_note(row.lib_id))
                continue
        await _finalise(journal, row.id, "leftover", sha)
        notes.append(_leftover_note(row))
    return notes


# -- history wiring: library locator (FP-C6) ---------------------------------------------

_FOOTPRINT_DIR_ENV = re.compile(r"^KICAD(\d+)_FOOTPRINT_DIR$")
RELOAD_WAIT_S = 20.0  # wait_for_libraries after ReloadLibrary (external change)


def stock_footprint_dir(settings: Settings) -> Path | None:
    """KiCad's stock footprint directory: `KICAD<n>_FOOTPRINT_DIR` of the api-server's
    environment (the highest n), else `share/kicad/footprints` of the install
    `settings.kicad_cli` belongs to; None when neither is known."""
    found: list[tuple[int, str]] = []
    for key, value in os.environ.items():
        m = _FOOTPRINT_DIR_ENV.match(key)
        if m and value:
            found.append((int(m.group(1)), value))
    if found:
        return Path(max(found)[1])
    template = kicad_template_dir(settings.kicad_cli)
    return None if template is None else template.parent / "footprints"


def stock_footprint_nicknames(settings: Settings) -> frozenset[str]:
    """The global nicknames without an api-server (index author call 9): the names of
    the `*.pretty` directories in the stock footprint directory (no file is read),
    which equals the image's global footprint table."""
    directory = stock_footprint_dir(settings)
    if directory is None:
        return frozenset()
    try:
        with os.scandir(directory) as entries:
            return frozenset(
                e.name.removesuffix(".pretty")
                for e in entries
                if e.name.endswith(".pretty") and e.is_dir()
            )
    except OSError:
        return frozenset()


def _serving(sessions: KicadSessionManager, project: Project) -> KicadSession | None:
    """The open session of `project`, else None (another or no project open)."""
    current = sessions.current()
    if current is None or current.closed or current.project.id != project.id:
        return None
    return current


def make_library_locator(settings: Settings, sessions: KicadSessionManager) -> LibraryLocator:
    """`HistoryService.locate_library` (FP-C6): the repo's local library of `project`,
    also for a project that is not open (restore of a closed project). None while
    `repo_policy` is transient. The global nicknames come from the project's own
    session (its `LibraryService`, cached per generation) and otherwise - also when
    that read fails - from the stock directory, so the derivation matches
    `LibraryService.local_library`."""

    async def locate(project: Project) -> LibraryLocation | None:
        policy = await repo_policy(project, settings.projects_root)
        if policy.transient:
            return None
        nicknames: frozenset[str] | None = None
        session = _serving(sessions, project)
        if session is not None:
            try:
                nicknames = await library_for(session).global_footprint_nicknames()
            except Exception as exc:  # KiCad down: the stock dir names the same table
                log.warning("global footprint table unavailable, using the stock dir: %s", exc)
        if nicknames is None:
            nicknames = await asyncio.to_thread(stock_footprint_nicknames, settings)
        lib = derive_local_library(policy.project_dir, policy.root, nicknames)
        return LibraryLocation(
            dir=lib.dir, repo_root=lib.repo_root, table=lib.table, nickname=lib.nickname
        )

    return locate


# -- history wiring: library listener (FP-C6) ----------------------------------------------


def _reload_footprint_library(nickname: str, h: KicadHandle) -> None:
    """ReloadLibrary of the project row, then wait for the (asynchronous) load. Without
    a project row of that nickname there is nothing to reload (no request, no warning)."""
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    if nickname not in _project_rows(h):
        return
    h.kicad.reload_library(LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_PROJECT, [nickname])
    h.kicad.wait_for_libraries(
        LibraryType.LT_FOOTPRINT,
        interval_ms=100,
        timeout_s=RELOAD_WAIT_S,
        scope=LibraryTableScope.LTS_PROJECT,
    )


LibraryListener = Callable[[Project, list[str], str], Awaitable[None]]


def make_library_listener(settings: Settings, sessions: KicadSessionManager) -> LibraryListener:
    """The library listener (FP-C6) of the project's open session: `own` / `restore` ->
    `invalidate_footprints(nick)`; `external` -> `ReloadLibrary(nick)` +
    `wait_for_libraries` only when the footprint tools are on and the libraries are
    loaded (a reload would otherwise load them: +1.7 GB), then invalidate - also when
    the reload failed (the error goes on to `notify_library`, which logs it). Without
    an open session there is nothing cached; while the root is unknown every library
    is invalidated."""
    locate = make_library_locator(settings, sessions)

    async def listener(project: Project, rels: list[str], reason: str) -> None:
        session = _serving(sessions, project)
        if session is None:
            return
        location = await locate(project)
        nickname = None if location is None else location.nickname
        svc = library_for(session)
        try:
            if (
                reason == "external"
                and nickname is not None
                and settings.footprint_tools
                and svc.footprint_libraries_ready()
            ):
                await session.call(
                    partial(_reload_footprint_library, nickname),
                    timeout=RELOAD_WAIT_S + settings.kicad_api_timeout_s,
                )
        finally:
            svc.invalidate_footprints(nickname)

    return listener
