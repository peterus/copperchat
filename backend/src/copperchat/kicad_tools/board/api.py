"""Raw-protobuf board API wrappers for what kipy lacks at the pin (B-C3; spike 4A gaps G3,
G5): the DRC task, the busy-safe zone refill, Specctra DSN export / session import,
`SaveDocumentToString`, board creation, the project path fix and group names.

Every function is synchronous and runs inside `KicadSession.call`; long ones pass an
explicit `timeout=` there (`drc_call_timeout`, `refill_call_timeout`, B-C4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any, Literal

from kipy.proto.board import board_commands_pb2, board_jobs_pb2, board_rules_pb2, board_tasks_pb2
from kipy.proto.common import tasks_pb2
from kipy.proto.common.commands import editor_commands_pb2
from kipy.proto.common.types import DocumentType, base_types_pb2, jobs_pb2

from copperchat.board.model import Point
from copperchat.errors import StructuredToolError
from copperchat.kicad_session.session import KicadHandle, kicad_busy
from copperchat.kicad_tools.units import nm_to_mm

if TYPE_CHECKING:
    from kipy.project import Project as KipyProject

# -- DRC task --------------------------------------------------------------------------

DRC_STALE_GRACE_S = 30.0  # how long a call waits for an earlier, timed-out DRC task
DRC_POLL_S = 0.05


@dataclass(frozen=True)
class DrcMarker:
    type: str  # KiCad's DrcErrorType without the DRCET_ prefix, lower case ("clearance")
    severity: Literal["error", "warning", "exclusion"]
    text: str
    kiids: tuple[str, ...]
    pos: Point | None


@dataclass(frozen=True)
class DrcResult:
    markers: tuple[DrcMarker, ...]
    unconnected: int  # unconnected-items markers (DrcTaskResult has no count at the pin)
    seconds: float


class DrcTimeout(StructuredToolError):
    """The DRC task was still running at `timeout_s` (B-C14 `drc_timeout`). Distinct from
    `drc_rejected` so deadline-bound callers can report "not verified" instead of
    failing. The pin has no task cancel: the task is remembered on the session's DRC
    slot and the next `run_drc_task` waits for it first."""

    def __init__(self, timeout_s: float, why: str) -> None:
        super().__init__(
            "DRC did not finish",
            why,
            "retry the check, or check a smaller change",
            code="drc_timeout",
            details={"timeout_s": timeout_s},
        )


def drc_call_timeout(timeout_s: float) -> float:
    """The `session.call` timeout around `run_drc_task(timeout_s=…)` (B-C4): its own
    budget, the stale-task grace and a margin."""
    return timeout_s + DRC_STALE_GRACE_S + 15


def _poll_task(client: Any, token: tasks_pb2.TaskToken, deadline: float) -> Any:
    """`GetTaskStatus` every DRC_POLL_S until the task leaves TS_RUNNING (returns that
    status) or `deadline` passes first (returns None: still running as far as known)."""
    while monotonic() < deadline:
        status = client.send(tasks_pb2.GetTaskStatus(token=token), tasks_pb2.TaskStatusResponse)
        if status.state != tasks_pb2.TS_RUNNING:
            return status
        sleep(max(0.0, min(DRC_POLL_S, deadline - monotonic())))
    return None


def _marker_type(error_type: int) -> str:
    try:
        name = str(board_rules_pb2.DrcErrorType.Name(error_type))
    except ValueError:
        return f"unknown_{error_type}"
    return name.removeprefix("DRCET_").lower()


def _severity(m: Any) -> Literal["error", "warning", "exclusion"]:
    if m.excluded or m.severity == base_types_pb2.RS_EXCLUSION:
        return "exclusion"
    return "error" if m.severity == base_types_pb2.RS_ERROR else "warning"


def _marker(m: Any) -> DrcMarker:
    pos = (nm_to_mm(m.position.x_nm), nm_to_mm(m.position.y_nm)) if m.HasField("position") else None
    return DrcMarker(
        type=_marker_type(m.error_type),
        severity=_severity(m),
        text=str(m.description),
        kiids=tuple(str(k.value) for k in m.items),
        pos=pos,
    )


def run_drc_task(h: KicadHandle, *, timeout_s: float = 60.0) -> DrcResult:
    """StartDrcTask(report_all_track_errors) → GetTaskStatus → GetDrcTaskResult.

    A task an earlier call left running (`h.drc_slot`, same server generation) is waited
    for first - at most DRC_STALE_GRACE_S and never past this call's own `timeout_s` - so
    two DRC tasks never overlap; still running → DrcTimeout without a second task."""
    t0 = monotonic()
    deadline = t0 + timeout_s
    board = h.board()
    client = board.client
    pending = h.drc_slot.get()
    if pending is not None:
        generation, task_id = pending
        if generation == h.generation:
            stale = tasks_pb2.TaskToken(value=task_id)
            if _poll_task(client, stale, min(t0 + DRC_STALE_GRACE_S, deadline)) is None:
                raise DrcTimeout(timeout_s, "an earlier DRC task on this board is still running")
        h.drc_slot.set(None)  # finished, or it died with an earlier server

    start = board_tasks_pb2.StartDrcTask(board=board.document, report_all_track_errors=True)
    token = client.send(start, tasks_pb2.StartTaskResponse).token
    status = _poll_task(client, token, deadline)
    if status is None:
        h.drc_slot.set((h.generation, str(token.value)))
        raise DrcTimeout(timeout_s, f"KiCad's DRC task was still running after {timeout_s:g} s")
    if status.state != tasks_pb2.TS_COMPLETED:
        state = tasks_pb2.TaskState.Name(status.state)
        raise StructuredToolError(
            "DRC failed",
            f"KiCad's DRC task ended {state}: {status.error_message or 'no message'}",
            "check the board for problems KiCad cannot check (e.g. an invalid outline)",
            code="drc_rejected",
        )
    result = client.send(
        board_tasks_pb2.GetDrcTaskResult(token=token), board_tasks_pb2.DrcTaskResult
    )
    markers = tuple(_marker(m) for m in result.violations)
    unconnected = sum(
        1 for m in result.violations if "UNCONNECTED" in _marker_type(m.error_type).upper()
    )
    return DrcResult(markers=markers, unconnected=unconnected, seconds=monotonic() - t0)


# -- zone refill -------------------------------------------------------------------------

REFILL_BUSY_GRACE_S = 30.0  # how long `refill` waits for a fill past its own budget
REFILL_MIN_S = 20.0  # deadline-bound callers skip a refill with less time left (P6b, P7)
REFILL_TIMEOUT_S = 60.0  # default refill budget (one definition for P6b and P7)
REFILL_POLL_S = 0.5


class RefillTimeout(StructuredToolError):
    """KiCad was still filling zones `timeout_s` + REFILL_BUSY_GRACE_S after the refill
    started (B-C14 `refill_timeout`). The session's busy slot is set, so its next call
    pings until KiCad is idle before running anything."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            "Zone refill did not finish",
            f"KiCad was still filling zones {timeout_s:g} s + {REFILL_BUSY_GRACE_S:g} s "
            "after the refill started",
            "simplify the zones (fewer or smaller pours) or retry",
            code="refill_timeout",
            details={"timeout_s": timeout_s, "grace_s": REFILL_BUSY_GRACE_S},
        )


def refill_call_timeout(timeout_s: float) -> float:
    """The `session.call` timeout around `refill(timeout_s=…)` (B-C4)."""
    return timeout_s + REFILL_BUSY_GRACE_S + 15


def refill(h: KicadHandle, *, timeout_s: float) -> None:
    """THE zone refill (P6b, P7 never call `refill_zones` directly).

    kipy's blocking `refill_zones` returns silently at `max_poll_seconds` while KiCad may
    still be filling (the next request would then fail with AS_BUSY), so afterwards this
    probes with `Ping` - and sends nothing else - until idle, at most
    REFILL_BUSY_GRACE_S; still busy → RefillTimeout with the session's busy slot set."""
    h.board().refill_zones(block=True, max_poll_seconds=timeout_s)
    grace_end = monotonic() + REFILL_BUSY_GRACE_S
    while kicad_busy(h.kicad):
        if monotonic() >= grace_end:
            h.busy_slot.set(h.generation)
            raise RefillTimeout(timeout_s)
        sleep(REFILL_POLL_S)
    h.busy_slot.set(None)


# -- documents, Specctra, project, groups -------------------------------------------------


def export_specctra_dsn(h: KicadHandle, dest: Path) -> None:
    """RunBoardJobExportSpecctraDsn: the live board (unsaved changes included) as DSN."""
    board = h.board()
    cmd = board_jobs_pb2.RunBoardJobExportSpecctraDsn()
    cmd.job_settings.document.CopyFrom(board.document)
    cmd.job_settings.output_path = str(dest)
    response = board.client.send(cmd, jobs_pb2.RunJobResponse)
    if response.status == jobs_pb2.JS_ERROR:
        raise StructuredToolError(
            "Specctra DSN export failed",
            response.message or "KiCad reported an error without a message",
            "check the board outline and footprints",
            code="export_failed",
        )


def import_specctra_session(h: KicadHandle, ses: Path) -> None:
    """ImportSpecctraSession: KiCad's errors (e.g. a missing file) raise kipy `ApiError`."""
    board = h.board()
    cmd = board_commands_pb2.ImportSpecctraSession(board=board.document, session_path=str(ses))
    board.client.send(cmd, board_commands_pb2.ImportSpecctraSessionResponse)


def save_document_to_string(h: KicadHandle) -> str:
    """The live board as KiCad would write it (SaveDocumentToString)."""
    board = h.board()
    cmd = editor_commands_pb2.SaveDocumentToString(document=board.document)
    return str(board.client.send(cmd, editor_commands_pb2.SavedDocumentResponse).contents)


def create_board_document(h: KicadHandle, path: Path) -> None:
    """CreateDocument(DOCTYPE_PCB) + save: an empty board written by KiCad. The session
    does not know the new document; the caller reopens it (B-C4 `creates_board`)."""
    h.kicad.create_document(str(path), DocumentType.DOCTYPE_PCB)
    h.kicad.get_board().save()


def board_project(h: KicadHandle) -> KipyProject:
    """`Board.get_project()` with the trailing "/" KiCad compares the path with (G3:
    without it every project command fails with "project … is not open at path")."""
    project = h.board().get_project()
    spec = project.document.project
    if not spec.path.endswith("/"):
        spec.path += "/"
    return project


def set_group_name(group_proto: Any, name: str) -> None:
    """kipy's `Group.name` has no setter at the pin: set the protobuf field."""
    group_proto.name = name
