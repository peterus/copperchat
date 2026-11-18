"""`fp_create.prepare` (spec §7.6; F4a Task 2): validate, check the name, the source and
the model link, build the target geometry, run `fp_check` on it and plan the change set.
Nothing is written here; the stages (S1 library row, S2 seed copy, S3 rebuild) run in
`apply` (engine).

Order: schema → name → activation + seed check → local library → name free (or, for
`replace`, the API pre-image) → source → model → target → checks → `Logical(5)`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from google.protobuf.any_pb2 import Any as PbAny  # type: ignore[import-untyped]

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import SEED_LIB_ID
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import record_applied
from copperchat.kicad_tools.footprints.build import (
    CreateArgs,
    ModelSpec,
    SourceSpec,
    build_target,
    parse_create_args,
    source_error,
)
from copperchat.kicad_tools.footprints.checks import CheckInput, run_checks, to_result
from copperchat.kicad_tools.footprints.engine import (
    FootprintApplied,
    FootprintChangeSet,
    FootprintPreImage,
    FpStage,
    apply_footprint_change_set,
    cleanable_copy,
)
from copperchat.kicad_tools.footprints.journal import file_sha256 as _file_sha256
from copperchat.kicad_tools.footprints.journal import scrub_repo
from copperchat.kicad_tools.footprints.model import SOURCE_FIELD, geometry_from_proto, geometry_hash
from copperchat.kicad_tools.footprints.read import require_enabled, symbol_pins
from copperchat.library.local import LocalLibrary
from copperchat.library.service import LibraryService, library_for, local_footprint_file_safe
from copperchat.repo_context.paths import RepoPathError, open_regular, repo_policy, resolve_file
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

TOOL = "fp_create"
LOGICAL_CHANGES = 5  # spec §12 (owner decision D3), new and replace alike

# Whether an existing `<name>` of the local library is a half-built copy the next write
# cleans up (spec §9.3, FP-C8 `classify_pending`, read-only): free for a new name,
# `not_found` for `replace`.
CleanableCopy = Callable[[ToolContext, LocalLibrary, str], Awaitable[bool]]


def _error(what: str, why: str, suggestion: str, code: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code=code)


async def _activate(svc: LibraryService, notes: list[dict[str, Any]]) -> None:
    """Load the footprint libraries (also on a board server: S2/S3 open documents
    anyway) and make sure the pinned seed exists, before anything is written."""
    await svc.activate_footprints(notes)
    if not await svc.has_footprint(SEED_LIB_ID):
        raise StructuredToolError(
            f"could not create footprints (seed {SEED_LIB_ID} missing)",
            f"KiCad does not serve the seed footprint {SEED_LIB_ID}, which every new "
            "footprint is copied from",
            "Check that the KiCad stock footprint libraries are installed; nothing was written.",
            code="seed_missing",
            details={"lib_id": SEED_LIB_ID},
        )


def _packed(item: Any) -> bytes:
    packed = PbAny()
    packed.Pack(item)
    return bytes(packed.SerializeToString(deterministic=True))


async def read_pre_image(ctx: ToolContext, lib: LocalLibrary, name: str) -> FootprintPreImage:
    """What `replace` overwrites, as API data (spec §9.5): every item of the footprint
    document (each a serialized `google.protobuf.Any`), the serialized
    `FootprintInstance`, the file's sha256, the geometry hash and the fields. Read with the document
    closed again before anything else runs (no `_fp_lock` held afterwards)."""
    from kipy.errors import ApiError

    lib_id = f"{lib.nickname}:{name}"
    try:
        async with ctx.session.footprint_document(lib_id) as doc:
            items = await doc.items()
            instance = await doc.footprint()
    except (ApiError, RuntimeError) as exc:  # KicadUnavailable passes through
        log.warning("reading the pre-image of %s failed: %s", lib_id, exc)
        raise StructuredToolError(
            f"Could not read footprint {lib_id}",
            "KiCad could not open the footprint to replace: " + scrub_repo(str(exc), lib.repo_root),
            "Check it with fp_get; ask the user to open it in KiCad if it keeps failing.",
            code="library_error",
        ) from exc
    geometry = geometry_from_proto(instance, instance.definition.items, lib_id, "local")

    def file_sha() -> str:
        local_footprint_file_safe(lib, name)
        return _file_sha256(lib.dir / f"{name}.kicad_mod", lib.repo_root)

    return FootprintPreImage(
        items=[_packed(i) for i in items],
        definition=bytes(instance.SerializeToString(deterministic=True)),
        file_sha256=await asyncio.to_thread(file_sha),
        geometry_hash=geometry_hash(geometry),
        fields=dict(geometry.fields),
    )


async def _check_source(ctx: ToolContext, source: SourceSpec) -> SourceSpec:
    """A URL must be http(s) with a host; a repo path must resolve (repo root, block
    rules, symlink policy) to an existing regular file. Every failure is the same
    `source_error`. A repo path is returned normalised (repo-relative POSIX)."""
    from urllib.parse import urlsplit

    if source.url:
        try:
            parts = urlsplit(source.document)
        except ValueError:
            raise source_error() from None
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise source_error()
        if any(c.isspace() for c in source.document):
            raise source_error()
        return source
    policy = await repo_policy(ctx.project, ctx.settings.projects_root)
    if policy.transient:
        raise source_error()

    def resolve() -> str:
        resolved, rel = resolve_file(policy, source.document)
        fd = open_regular(resolved)  # a regular file, not a FIFO or device
        os.close(fd)
        return rel.as_posix()

    try:
        rel = await asyncio.to_thread(resolve)
    except (RepoPathError, OSError):
        raise source_error() from None
    return SourceSpec(rel, source.location, source.mpn, False)


def _model_note(ctx: ToolContext, model: ModelSpec) -> dict[str, Any] | None:
    """The `model_missing` note when the linked file cannot be found from here (a link
    only: the footprint is created anyway)."""
    if model.variable == "KIPRJMOD":
        base = str(ctx.project.dir)
    else:
        # the api-server inherits this process's environment (launcher.kicad_cli_env);
        # an unset variable is resolved by KiCad itself: no claim either way, no note
        env = os.environ.get(model.variable)
        if not env:
            return None
        base = env
    if os.path.isfile(os.path.join(base, *model.rel.split("/"))):
        return None
    return {
        "kind": "model_missing",
        "path": model.path,
        "message": f"3D model {model.path} not found; the footprint links it anyway. "
        "Tell the user to add the model file.",
    }


async def _check(
    ctx: ToolContext, args: CreateArgs, target: Any
) -> tuple[dict[str, Any], list[str] | None, bool]:
    """fp_check of the target (errors refuse); returns the result and the symbol pins
    it used (the read-back is checked against the same pins)."""
    pins, polarized = await symbol_pins(ctx, args.symbol) if args.symbol else (None, False)
    inp = CheckInput(geometry=target, pins=pins, polarized=polarized, local=True)
    report = await asyncio.to_thread(run_checks, inp)
    result = to_result(report)
    if not report.passed:
        first = next(f for f in report.findings if f.severity == "error")
        raise StructuredToolError(
            f"The footprint {target.lib_id} would fail fp_check",
            f"{report.counts['error']} error(s), e.g. {first.rule}: {first.message}",
            "Fix the input (pads, layers, courtyard, body) against the datasheet and call "
            "fp_create again; nothing was written.",
            code="check_failed",
            details=result,
        )
    return result, pins, polarized


async def prepare_create(
    ctx: ToolContext, args: dict[str, Any], *, cleanable: CleanableCopy = cleanable_copy
) -> FootprintChangeSet:
    """Plan an `fp_create` (spec §7.6); refuses with a structured error, writes nothing.

    `cleanable` decides whether an existing local footprint of the requested name is a
    half-built copy the write path cleans up first (spec §9.3): prepare validates
    against the state after that cleanup, so the copy counts as free for a new name
    and as `not_found` for `replace` (such a call is refused; its cleanup never runs)."""
    parsed = parse_create_args(args)  # schema + NAME_RE
    require_enabled(ctx)
    notes: list[dict[str, Any]] = []
    svc = library_for(ctx.session)
    await _activate(svc, notes)
    info = await svc.local_library(for_write=True)
    assert info is not None  # for_write raises repo_root_unknown instead
    if info.row_conflict:
        raise _error(
            "The local footprint library conflicts with the project's library table",
            info.row_conflict,
            "Ask the user to rename or remove the conflicting fp-lib-table row; nothing "
            "was written.",
            "library_conflict",
        )
    lib = info.lib
    name = parsed.name
    lib_id = f"{lib.nickname}:{name}"
    if info.row_present:
        names = await svc.footprint_names(lib.nickname)
    else:
        names = await asyncio.to_thread(svc.local_names_on_disk, lib)
    cleanable_now = name in names and await cleanable(ctx, lib, name)
    taken = name in names and not cleanable_now
    pre_image = None
    if parsed.replace:
        if info.row_present and cleanable_now:
            raise _error(
                f"Footprint {lib_id} not found",
                f"{lib_id} is only an unfinished copy left by an earlier footprint call that "
                "stopped when the KiCad server died; the next footprint write removes it",
                "Leave replace out to create the footprint under this name; list the local "
                "ones with fp_list_local.",
                "not_found",
            )
        if not (info.row_present and taken):
            why = (
                f"this project has no library row for the local library ({lib.nickname}) "
                "yet, so it has no local footprint to rebuild"
                if not info.row_present
                else f"no footprint named {name!r} is in the local library ({lib.nickname}); "
                "replace=true only rebuilds an existing one"
            )
            raise _error(
                f"{lib_id} is not a footprint of the local library",
                why,
                "Leave replace out to create a new footprint; list the local ones with "
                "fp_list_local.",
                "not_local",
            )
        pre_image = await read_pre_image(ctx, lib, name)
    elif taken:
        where = "in the local library" if info.row_present else "on disk in the local library"
        raise _error(
            f"Footprint {lib_id} already exists",
            f"a footprint named {name!r} is {where} (another project of this repository may "
            "have created it)",
            "Pick another name, or pass replace=true to rebuild it (needs the user's OK).",
            "already_exists",
        )
    source = await _check_source(ctx, parsed.source)
    if parsed.model is not None:
        note = await asyncio.to_thread(_model_note, ctx, parsed.model)
        if note is not None:
            notes.append(note)
    parsed = replace(parsed, source=source)
    target = await asyncio.to_thread(build_target, parsed, lib)
    check, pins, polarized = await _check(ctx, parsed, target)

    stages: list[FpStage] = []
    if not info.row_present:
        stages.append(FpStage("ensure_library"))
    stages += [FpStage("copy_seed", lib_id), FpStage("rebuild", lib_id)]
    library_created = not await asyncio.to_thread(lib.dir.is_dir)
    return FootprintChangeSet(
        tool=TOOL,
        logical=Logical(LOGICAL_CHANGES),
        summary={
            "lib_id": lib_id,
            "replace": parsed.replace,
            "library": lib.nickname,
            "library_row_planned": not info.row_present,
            "library_dir_planned": library_created,
            "check": check,
            "notes": notes,
        },
        fp_stages=stages,
        target=target,
        pre_image=pre_image,
        library=lib,
        check_pins=pins,
        polarized=polarized,
    )


RENDER_SUGGESTION = (
    "Call fp_render on {lib_id} and show the user the render, this check and the source "
    "({source}); the footprint needs their review before it is used (needs_user_review)."
)


async def apply_footprint_create(ctx: ToolContext, cs: Any) -> ToolResult:
    """`fp_create.apply` (spec §7.6): run the write engine, then build the result from
    the read-back: `created`, `file`, `library_created`, `check` (fp_check on the
    read-back), `geometry` (summary), `needs_user_review`, `logical_changes`, `budget`
    (filled in by the adapter), `notes` and the suggestion to render and present it."""
    applied = await apply_footprint_change_set(ctx, cs)
    readback = applied.readback
    assert readback is not None
    inp = CheckInput(
        geometry=readback, pins=cs.check_pins, polarized=cs.polarized, local=True, pending=False
    )
    check = to_result(await asyncio.to_thread(run_checks, inp))
    lib_id = applied.lib_ids[0]
    source = readback.fields.get(SOURCE_FIELD, "")
    # one footprint per call: the read-back (and its hash) is that of lib_ids[0]
    assert len(applied.lib_ids) == 1, applied.lib_ids
    await record_applied(ctx, cs.tool, change_entries(ctx, cs, applied, geometry_hash(readback)))
    data: dict[str, Any] = {
        "created": list(applied.lib_ids),
        "file": applied.files[0],
        "library_created": applied.row_added,
        "check": check,
        "geometry": readback.to_dict("summary"),
        "needs_user_review": True,
        "logical_changes": cs.logical.changes,
        "budget": None,
        "notes": [*cs.summary.get("notes", []), *applied.notes],
        "suggestion": RENDER_SUGGESTION.format(lib_id=lib_id, source=source),
    }
    # Library-only (spec §9.1): saved and journalled inside apply, so the adapter's save
    # step has nothing to do and its generation check is skipped.
    return ToolResult(data=data, changed_docs=set(), library_changed={cs.library.nickname})


def change_entries(
    ctx: ToolContext, cs: Any, applied: FootprintApplied, new_hash: str
) -> list[ChangeEntry]:
    """The change-log rows of an applied footprint write (spec §10): the project's
    library row when this call added it (`fp_lib_table_row_added`, ref = the nickname,
    after = its URI), then per footprint `footprint_created` (op add) or, for
    `replace`, `footprint_replaced` (op modify, before = the pre-image's hash); ref and
    uuid = the lib id, after = {"geometry_hash": ...}. No sheet: never diffed."""
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None

    def entry(op: str, kind: str, ref: str, **values: Any) -> ChangeEntry:
        return ChangeEntry(
            project_id=ctx.project.id,
            run_id=ctx.run_id or "",
            plan_step_id=state.active_step_id if state else None,
            call_seq=state.call_seq if state else 0,
            tool=cs.tool,
            op=op,
            kind=kind,
            uuid=ref,
            sheet_path="",
            ref=ref,
            **values,
        )

    out: list[ChangeEntry] = []
    lib = cs.library
    if applied.row_added and lib is not None:
        out.append(entry("add", "fp_lib_table_row_added", lib.nickname, after={"uri": lib.uri}))
    after = {"geometry_hash": new_hash}
    for lib_id in applied.lib_ids:
        if cs.pre_image is not None:
            before = {"geometry_hash": cs.pre_image.geometry_hash}
            out.append(
                entry("modify", "footprint_replaced", lib_id, before=before, after=dict(after))
            )
        else:
            out.append(entry("add", "footprint_created", lib_id, after=dict(after)))
    return out
