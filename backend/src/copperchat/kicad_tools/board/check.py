"""`pcb_check` (spec §7.1, §8.3, §12; P3b Task 2): the board's DRC and metrics as one
report, recorded as evidence for `pcb_approve` (index call 14).

* **quick**: KiCad's DRC task (API) + metrics M1-M14 in the CPU worker (no preset: M14
  `n/a`).
* **full**: additionally `kicad-cli pcb drc --schematic-parity` on a copy of the live
  board and schematic in `.kicad-agent/board/check/<uuid>/` (nothing in the project is
  written; the copy is removed afterwards), and M14 against the fab preset.

One source per M3 term: errors and warnings come only from the API DRC task, parity
markers only from the CLI report's `schematic_parity`; the CLI's other violations are only
cross-checked against the API markers (`cli_crosscheck`, informational). The hashes are
read before the DRC and again after: a board that changed meanwhile gives `stale: true`
and no evidence row. A read tool: never the edit lock, no KiCad data written."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import stat
import time
import uuid
from collections import Counter
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Literal

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.jobs import metrics_job
from copperchat.board.metrics import MetricsReport
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.board.worker import run_cpu
from copperchat.db import utcnow_iso
from copperchat.db.repos.board import BoardReviewRepo, board_file
from copperchat.errors import StructuredToolError
from copperchat.kicad_session.errors import DocumentModified
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.analysis import metrics_data, preset_for
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult
from copperchat.kicad_tools.board.checkpoints import board_written_since, record_unless_written
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext, ToolResult
from copperchat.views.board_render import BOARD_CLI_LOCK

log = logging.getLogger(__name__)

Level = Literal["quick", "full"]
LEVELS: tuple[Level, ...] = ("quick", "full")
CHECK_KEYS = frozenset({"level"})
MAX_LISTED = 50  # errors, warnings and parity markers listed each; the rest is counted
MAX_TEXT = 500  # characters of one marker text
CHECK_DRC_TIMEOUT_S = 60.0  # the API DRC task (B-C4: explicit, longer call timeout)
CHECK_METRICS_TIMEOUT_S = 30.0  # run_cpu budget of the metrics job
BOARD_PARITY_TIMEOUT_S = 120.0  # kicad-cli pcb drc --schematic-parity (B-C9)
MAX_REPORT_BYTES = 8 * 1024 * 1024  # kicad-cli's JSON report (synth: a few KB)
# kicad-cli exits 0 with an empty schematic_parity when it cannot build the netlist
PARITY_FAILED = "failed to fetch schematic netlist"
MAX_COPY_BYTES = 16 * 1024 * 1024  # each byte-copied project file (.kicad_pro, lib tables)
_SIDE_FILES = ("fp-lib-table", "sym-lib-table")


def _invalid(what: str, why: str) -> StructuredToolError:
    return StructuredToolError(what, why, "see the tool schema", code="invalid_argument")


def _rejected(why: str) -> StructuredToolError:
    return StructuredToolError(
        "The full board check could not run",
        why,
        "Run pcb_check level 'quick', or try 'full' again; tell the user if it keeps failing.",
        code="drc_rejected",
    )


def parse_level(args: dict[str, Any]) -> Level:
    """The schema on the server: exactly `{level: quick | full}`."""
    unknown = sorted(set(args) - CHECK_KEYS)
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown[:10]}")
    level = args.get("level")
    if not isinstance(level, str) or level not in LEVELS:
        raise _invalid("Invalid level", "level must be 'quick' or 'full'")
    return level


# -- kicad-cli JSON report ----------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _marker(entry: Any) -> DrcMarker:
    """One violation of the report; odd shapes give empty fields, never an exception."""
    e = entry if isinstance(entry, dict) else {}
    items = e.get("items")
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    kiids = tuple(i["uuid"] for i in items if isinstance(i.get("uuid"), str))
    pos = None
    for item in items:
        p = item.get("pos")
        if isinstance(p, dict):
            x, y = _num(p.get("x")), _num(p.get("y"))
            if x is not None and y is not None:
                pos = (x, y)
            break
    severity = e.get("severity")
    sev: Literal["error", "warning", "exclusion"]
    if e.get("excluded") is True or severity == "exclusion":
        sev = "exclusion"
    elif severity == "error":
        sev = "error"
    else:
        sev = "warning"
    t = e.get("type")
    text = e.get("description")
    return DrcMarker(
        type=t[:100] if isinstance(t, str) else "",
        severity=sev,
        text=text[:MAX_TEXT] if isinstance(text, str) else "",
        kiids=kiids,
        pos=pos,
    )


def _entries(doc: dict[str, Any], key: str) -> list[Any]:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def parse_cli_drc(
    report: dict[str, Any],
) -> tuple[tuple[DrcMarker, ...], tuple[DrcMarker, ...], int]:
    """(violations, parity, unconnected) of a `kicad-cli pcb drc --format json` document
    (`violations[]`, `schematic_parity[]`, `unconnected_items[]`, each `{type, severity,
    description, items[{uuid, pos{x, y}}]}`)."""
    violations = tuple(_marker(e) for e in _entries(report, "violations"))
    parity = tuple(_marker(e) for e in _entries(report, "schematic_parity"))
    return violations, parity, len(_entries(report, "unconnected_items"))


def _norm(t: str) -> str:
    """DRC type as M3 normalises it (P3a): casefold, spaces → `_`, `drce_` stripped."""
    return "_".join(t.strip().casefold().split()).removeprefix("drce_")


def crosscheck(cli: tuple[DrcMarker, ...], api_markers: tuple[DrcMarker, ...]) -> dict[str, int]:
    """How many CLI violations the API task did not report and vice versa, compared per
    normalised type (count difference): pad KIIDs differ between the live board and the
    saved copy kicad-cli reads, so KIID sets would never match. Unconnected markers are
    M2's and never compared. Informational only."""
    a = Counter(_norm(m.type) for m in api_markers)
    c = Counter(_norm(m.type) for m in cli)
    for counts in (a, c):
        for t in [t for t in counts if t.startswith("unconnected")]:
            del counts[t]
    return {"only_cli": sum((c - a).values()), "only_api": sum((a - c).values())}


def verdict(report: MetricsReport) -> Literal["pass", "review", "fail"]:
    """Any failing metric → fail, else any metric to review → review, else pass."""
    verdicts = {m.verdict for m in report.metrics}
    if "fail" in verdicts:
        return "fail"
    return "review" if "review" in verdicts else "pass"


# -- result rows ----------------------------------------------------------------------------


def _refs(model: BoardModel) -> dict[str, str]:
    return {p.kiid: p.ref for p in model.parts}


def _accepted(mk: DrcMarker, refs: tuple[str, ...], doc: BoardDoc) -> bool:
    """M3's rule (P3a): a warning whose type equals a `board.md` Checks row's type and
    whose part refs are non-empty and all items of that row."""
    if mk.severity != "warning" or not refs:
        return False
    t = _norm(mk.type)
    return any(
        r.items and t == _norm(r.drc_type) and set(refs) <= set(r.items) for r in doc.accepted
    )


def _row(mk: DrcMarker, refs: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": mk.type,
        "severity": mk.severity,
        "text": mk.text[:MAX_TEXT],
        "refs": list(refs),
        "x": None if mk.pos is None else round(mk.pos[0], 4),
        "y": None if mk.pos is None else round(mk.pos[1], 4),
    }


def _listed(rows: list[dict[str, Any]], key: str, out: dict[str, Any]) -> None:
    out[key] = rows[:MAX_LISTED]
    out[f"{key}_omitted"] = max(0, len(rows) - MAX_LISTED)


def check_rows(
    model: BoardModel,
    doc: BoardDoc,
    drc: DrcResult,
    parity: tuple[DrcMarker, ...] | None,
) -> dict[str, Any]:
    """`errors`, `warnings` (each `accepted`) from the API task and `parity` (None for
    quick) from the CLI report, each at most MAX_LISTED plus `<key>_omitted`."""
    by_kiid = _refs(model)

    def refs_of(mk: DrcMarker) -> tuple[str, ...]:
        return tuple(dict.fromkeys(by_kiid[k] for k in mk.kiids if k in by_kiid))

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for mk in drc.markers:
        if _norm(mk.type).startswith("unconnected") or mk.severity == "exclusion":
            continue
        refs = refs_of(mk)
        if mk.severity == "error":
            errors.append(_row(mk, refs))
        else:
            warnings.append(_row(mk, refs) | {"accepted": _accepted(mk, refs, doc)})
    out: dict[str, Any] = {}
    _listed(errors, "errors", out)
    _listed(warnings, "warnings", out)
    if parity is None:
        out["parity"], out["parity_omitted"] = None, 0
    else:
        _listed([_row(mk, refs_of(mk)) for mk in parity], "parity", out)
    return out


# -- the full check's kicad-cli run --------------------------------------------------------


def _copy_side_file(src: Path, dest: Path) -> None:
    """Byte copy of a read-only project input (regular file, not a symlink, bounded)."""
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_COPY_BYTES:
        log.warning("pcb_check: %s skipped (not a regular file or too large)", src.name)
        return
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_COPY_BYTES + 1)
    if len(data) > MAX_COPY_BYTES:
        return
    dest.write_bytes(data)


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REPORT_BYTES:
        raise _rejected("kicad-cli wrote no usable DRC report (not a file or too large)")
    try:
        doc = json.loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError):
        raise _rejected("kicad-cli's DRC report is not valid JSON") from None
    if not isinstance(doc, dict):
        raise _rejected("kicad-cli's DRC report is not a JSON object")
    return doc


def _read_and_parse(
    path: Path,
) -> tuple[tuple[DrcMarker, ...], tuple[DrcMarker, ...]] | None:
    """(violations, parity) of the report at `path` (None when there is none); excluded
    parity markers are dropped, as the API task's exclusions are."""
    report = _read_report(path)
    if report is None:
        return None
    violations, parity, _ = parse_cli_drc(report)
    return violations, tuple(mk for mk in parity if mk.severity != "exclusion")


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "no output")[:200]


async def _copy_project(ctx: ToolContext, work: Path) -> Path:
    """Board, schematic (with sub-sheets) and the project's read-only inputs copied into
    `work`; returns the board copy."""
    project = ctx.project
    if project.sch_file is None:
        raise _rejected("the project has no schematic to compare the board with")
    stem = project.pro_file.stem
    try:
        await ctx.session.save_copy("sch", work / f"{stem}.kicad_sch", save_modified=False)
    except DocumentModified:
        raise StructuredToolError(
            "The schematic has unsaved edits",
            "the full check compares the board with the saved schematic",
            "Save the schematic (or end the turn so it is saved), then run the full check.",
            code="schematic_unsaved",
        ) from None
    board = await ctx.session.save_copy("pcb", work / f"{stem}.kicad_pcb")

    def side_files() -> None:
        _copy_side_file(project.pro_file, work / f"{stem}.kicad_pro")
        for name in _SIDE_FILES:
            _copy_side_file(project.dir / name, work / name)

    await asyncio.to_thread(side_files)
    return board


STALE_CHECK_S = 3600.0  # a check/<uuid> dir older than this is left over from a killed call


def _check_dir(ctx: ToolContext) -> Path:
    """A fresh copy dir `.kicad-agent/board/check/<uuid>`; a symlinked `.kicad-agent`,
    `board` or `check` dir is refused (never written through), and copy dirs a killed
    call left behind are swept."""
    agent = agent_dir(ctx.project)
    base = agent / "board" / "check"
    for d in (agent, agent / "board", base):
        if d.is_symlink():
            raise StructuredToolError(
                "The full board check could not run",
                f"{d.relative_to(ctx.project.dir)} is a symlink",
                "Ask the user to replace it with a plain directory, then retry.",
                code="output_path_unsafe",
            )
    base.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in base.iterdir():
        try:
            st = old.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode) and now - st.st_mtime > STALE_CHECK_S:
            shutil.rmtree(old, ignore_errors=True)
    work = base / uuid.uuid4().hex
    work.mkdir()
    return work


async def cli_parity(ctx: ToolContext) -> tuple[tuple[DrcMarker, ...], tuple[DrcMarker, ...]]:
    """(violations, parity) of `kicad-cli pcb drc --schematic-parity` on a copy of the
    live board under `.kicad-agent/board/check/<uuid>/` (removed in `finally`), run inside
    `BOARD_CLI_LOCK` with BOARD_PARITY_TIMEOUT_S; a timeout or a failed run without a
    report → `drc_rejected`."""
    try:
        work = await asyncio.to_thread(_check_dir, ctx)
    except OSError as exc:
        raise _rejected(f"the copy directory could not be made ({type(exc).__name__})") from None
    try:
        board = await _copy_project(ctx, work)
        out = work / "drc.json"
        args = ["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
                "-o", str(out), str(board)]  # fmt: skip
        async with BOARD_CLI_LOCK:
            try:
                result = await run_kicad_cli(
                    ctx.settings, args, timeout=BOARD_PARITY_TIMEOUT_S, cwd=work
                )
            except TimeoutError:
                raise _rejected(
                    f"kicad-cli pcb drc took longer than {BOARD_PARITY_TIMEOUT_S:.0f} s"
                ) from None
            except OSError as exc:
                raise _rejected(f"kicad-cli could not be started ({type(exc).__name__})") from None
        if PARITY_FAILED in f"{result.stdout}\n{result.stderr}".casefold():
            # an empty parity list would read as "in sync": never fail open
            raise _rejected(
                "kicad-cli could not build the schematic netlist for the parity check "
                f"({_tail(result.stderr or result.stdout)}); check the schematic (ERC, "
                "annotation) first"
            )
        parsed = await asyncio.to_thread(_read_and_parse, out)  # big reports: off the loop
        if parsed is None:
            raise _rejected(
                f"kicad-cli pcb drc exited with {result.returncode} and wrote no report: "
                f"{_tail(result.stderr or result.stdout)}"
            )
        return parsed
    except OSError as exc:  # the copy directory or a copy could not be written
        raise _rejected(f"the board copy could not be written ({type(exc).__name__})") from None
    finally:
        work_task = asyncio.ensure_future(
            asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)
        )
        await asyncio.shield(work_task)


# -- the tool -------------------------------------------------------------------------------


def _hashes(model: BoardModel) -> tuple[str, str]:
    return placement_hash(model), board_hash(model)


async def pcb_check(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The check report (module docstring). With a conversation and a database the result
    is recorded as a `check` row (stage `placement` for quick, `board` for full) unless the
    board changed while it ran."""
    start = time.monotonic()
    level = parse_level(args)
    require_board(ctx)
    since = utcnow_iso()
    doc = await read_board_doc(ctx)
    # Model and DRC under the apply lock: no write is half pushed or being compensated
    # while they are read (a write undone by compensation logs no change row).
    async with ctx.session.applying():
        model = await board_model(ctx)
        drc = await ctx.session.call(
            lambda h: api.run_drc_task(h, timeout_s=CHECK_DRC_TIMEOUT_S),
            timeout=api.drc_call_timeout(CHECK_DRC_TIMEOUT_S),
        )
    before = await asyncio.to_thread(_hashes, model)
    parity: tuple[DrcMarker, ...] | None = None
    cross: dict[str, int] | None = None
    if level == "full":
        cli_violations, parity = await cli_parity(ctx)
        cross = await asyncio.to_thread(crosscheck, cli_violations, drc.markers)
        if cross["only_cli"] or cross["only_api"]:
            log.info("pcb_check full: CLI/API DRC differ %s", cross)
    after = await asyncio.to_thread(_hashes, await board_model(ctx))
    report = await run_cpu(
        metrics_job,
        model,
        doc,
        drc,
        preset_for(model, doc) if level == "full" else None,
        None,
        parity,
        timeout_s=CHECK_METRICS_TIMEOUT_S,
    )
    data: dict[str, Any] = {"level": level}
    data |= await asyncio.to_thread(check_rows, model, doc, drc, parity)
    data |= await asyncio.to_thread(metrics_data, report, model)
    data["verdict"] = verdict(report)
    if cross is not None:
        data["cli_crosscheck"] = cross
    stale = before != after
    if not stale and ctx.conversation_id is not None and ctx.editing is not None:
        m1 = next((m for m in report.metrics if m.id == "M1"), None)
        db, cid = ctx.editing.db, ctx.conversation_id

        def record() -> Awaitable[int]:
            return BoardReviewRepo(db).record_check(
                ctx.project.id,
                board_file(ctx.project),
                cid,
                ctx.run_id,
                level=level,
                hard_failures=report.hard_failures,
                m1_pass=m1 is not None and m1.verdict == "pass",
                placement_hash=before[0],
                board_hash=before[1],
                stage="placement" if level == "quick" else "board",
            )

        stale = not await record_unless_written(db, ctx.project.id, since, record)
    elif not stale and ctx.editing is not None:
        stale = await board_written_since(ctx.editing.db, ctx.project.id, since)
    data["stale"] = stale
    data["seconds"] = round(time.monotonic() - start, 3)
    return ToolResult(data=data)
