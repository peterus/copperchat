"""`fp_create.prepare` (spec §7.6; F4a Task 2): validate, check the name, the source and
the model link, build the target geometry, run `fp_check` on it and plan the change set.
Nothing is written here; the stages (S1 library row, S2 seed copy, S3 rebuild) run in
`apply` (engine).

Order: schema → name → activation + seed check → local library → name free (or, for
`replace`, the API pre-image) → source → model → target → checks → `Logical(5)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from google.protobuf.any_pb2 import Any as PbAny  # type: ignore[import-untyped]

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import SEED_LIB_ID
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.footprints.build import (
    CreateArgs,
    ModelSpec,
    SourceSpec,
    build_target,
    parse_create_args,
    source_error,
)
from copperchat.kicad_tools.footprints.checks import CheckInput, run_checks, to_result
from copperchat.kicad_tools.footprints.engine import FootprintChangeSet, FootprintPreImage, FpStage
from copperchat.kicad_tools.footprints.model import geometry_from_proto, geometry_hash
from copperchat.kicad_tools.footprints.read import require_enabled, symbol_pins
from copperchat.library.local import LocalLibrary
from copperchat.library.service import LibraryService, library_for, local_footprint_file_safe
from copperchat.repo_context.paths import RepoPathError, open_regular, repo_policy, resolve_file
from copperchat.tools.spec import ToolContext

TOOL = "fp_create"
LOGICAL_CHANGES = 5  # spec §12 (owner decision D3), new and replace alike
ABSENT = "absent"  # FP-C7: the sha of a file that does not exist

# Whether an existing `<name>` of the local library is a half-built copy the next write
# cleans up (spec §9.3, FP-C8 `classify_pending`); such a copy counts as free.
CleanableCopy = Callable[[ToolContext, LocalLibrary, str], Awaitable[bool]]


async def no_cleanable_copy(ctx: ToolContext, lib: LocalLibrary, name: str) -> bool:
    """Without the pending-write classifier every existing footprint counts as taken."""
    return False


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


def _file_sha256(path: Path) -> str:
    """sha256 of the regular file `path` (no symlink followed), or `absent`."""
    try:
        fd = open_regular(path)
    except FileNotFoundError:
        return ABSENT
    except OSError as exc:  # swapped for a symlink (ELOOP), a FIFO (EINVAL), ...
        raise StructuredToolError(
            "The local footprint library is not safe to write",
            f"{path} cannot be read as a regular file: {exc.strerror or exc}",
            "Ask the user to replace it with the footprint file KiCad saved; nothing was written.",
            code="library_conflict",
            details={"path": str(path)},
        ) from exc
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _packed(item: Any) -> bytes:
    packed = PbAny()
    packed.Pack(item)
    return bytes(packed.SerializeToString(deterministic=True))


async def read_pre_image(ctx: ToolContext, lib: LocalLibrary, name: str) -> FootprintPreImage:
    """What `replace` overwrites, as API data (spec §9.5): every item of the footprint
    document (each a serialized `google.protobuf.Any`), the serialized
    `FootprintInstance`, the file's sha256 and the geometry hash. Read with the document
    closed again before anything else runs (no `_fp_lock` held afterwards)."""
    from kipy.errors import ApiError

    lib_id = f"{lib.nickname}:{name}"
    try:
        async with ctx.session.footprint_document(lib_id) as doc:
            items = await doc.items()
            instance = await doc.footprint()
    except (ApiError, RuntimeError) as exc:  # KicadUnavailable passes through
        raise StructuredToolError(
            f"Could not read footprint {lib_id}",
            f"KiCad could not open the footprint to replace: {exc}",
            "Check it with fp_get; ask the user to open it in KiCad if it keeps failing.",
            code="library_error",
        ) from exc
    geometry = geometry_from_proto(instance, instance.definition.items, lib_id, "local")

    def file_sha() -> str:
        local_footprint_file_safe(lib, name)
        return _file_sha256(lib.dir / f"{name}.kicad_mod")

    return FootprintPreImage(
        items=[_packed(i) for i in items],
        definition=bytes(instance.SerializeToString(deterministic=True)),
        file_sha256=await asyncio.to_thread(file_sha),
        geometry_hash=geometry_hash(geometry),
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


async def _check(ctx: ToolContext, args: CreateArgs, target: Any) -> dict[str, Any]:
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
    return result


async def prepare_create(
    ctx: ToolContext, args: dict[str, Any], *, cleanable: CleanableCopy = no_cleanable_copy
) -> FootprintChangeSet:
    """Plan an `fp_create` (spec §7.6); refuses with a structured error, writes nothing.

    `cleanable` decides whether an existing local footprint of the requested name is a
    half-built copy the write path cleans up first (it then counts as free)."""
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
    taken = name in names and not await cleanable(ctx, lib, name)
    pre_image = None
    if parsed.replace:
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
    check = await _check(ctx, parsed, target)

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
    )
