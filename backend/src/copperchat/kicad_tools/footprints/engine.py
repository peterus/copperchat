"""Footprint write path (FP-C8, spec §9): the change-set types the footprint tools plan in
`prepare`, and the history wiring of the local library (FP-C6: library locator and
listener). The staged engine (S1-S3, compensation, journal) builds on these types.

`FootprintChangeSet` is a `ChangeSet` so the tool adapter, the gates and the change log
treat it like any two-phase edit; the schematic engine (`apply_change_set`) refuses it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from copperchat.config import Settings
from copperchat.history.store import LibraryLocation, LibraryLocator
from copperchat.kicad_session import KicadHandle, KicadSession, KicadSessionManager
from copperchat.kicad_session.launcher import kicad_template_dir
from copperchat.kicad_tools.edit.changeset import ChangeSet, NetIntent, SheetRef, Stage
from copperchat.library.local import LocalLibrary, derive_local_library
from copperchat.library.service import library_for
from copperchat.projects import Project
from copperchat.repo_context.paths import repo_policy

if TYPE_CHECKING:
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


@dataclass
class FootprintApplied:
    """What a footprint change set wrote: the lib ids, the repo-relative files, whether
    this call created the library row / directory, the read-back geometry and notes."""

    lib_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    row_added: bool = False
    library_created: bool = False
    readback: FootprintGeometry | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)


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
    """ReloadLibrary of the project row, then wait for the (asynchronous) load."""
    from kipy.proto.common.types import LibraryTableScope, LibraryType

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
