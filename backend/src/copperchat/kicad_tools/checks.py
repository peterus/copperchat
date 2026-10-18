from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
import weakref
from collections import Counter
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadSession
from copperchat.kicad_session.launcher import kicad_cli_env, kicad_config_home
from copperchat.kicad_tools.filtering import matches
from copperchat.projects import Project
from copperchat.tools.spec import ToolContext, ToolResult

# KiCad-written project files the checks need next to the saved copies: the
# project settings (ERC/DRC severities, design rules) and the project library
# tables. Byte copies; never modified.
PROJECT_SUPPORT_FILES = ("sym-lib-table", "fp-lib-table")
SEVERITIES = ("error", "warning", "info", "exclusion", "ignore")


def _pos(p: dict[str, Any] | None) -> dict[str, float] | None:
    # `--units mm`: kicad-cli reports `coordinate_units: "mm"` (spike 1C, Q6).
    if not p:
        return None
    return {"x": round(float(p["x"]), 4), "y": round(float(p["y"]), 4)}


def _entry(e: dict[str, Any], default_type: str) -> dict[str, Any]:
    items = [
        {
            "uuid": str(i.get("uuid", "")),
            "description": str(i.get("description", "")),
            "position_mm": _pos(i.get("pos")),
        }
        for i in e.get("items", [])
    ]
    return {
        "type": str(e.get("type", default_type)),
        "severity": str(e.get("severity", "error")),
        "description": str(e.get("description", "")),
        "position_mm": items[0]["position_mm"] if items else None,
        "items": items,
    }


def parse_violations(raw: dict[str, Any], doc: DocKind) -> list[dict[str, Any]]:
    """kicad-cli ERC/DRC JSON -> flat violation list (ERC entries carry their sheet path)."""
    if doc == "sch":
        return [
            _entry(v, "erc") | {"sheet": str(s.get("path", "/"))}
            for s in raw.get("sheets", [])
            for v in s.get("violations", [])
        ]
    out = [_entry(v, "drc") for v in raw.get("violations", [])]
    out += [_entry(v, "unconnected_items") for v in raw.get("unconnected_items", [])]
    out += [_entry(v, "schematic_parity") for v in raw.get("schematic_parity", [])]
    return out


def _check_name(doc: DocKind) -> str:
    return "ERC" if doc == "sch" else "DRC"


async def _save_copies(session: KicadSession, dest: Path) -> dict[DocKind, Path]:
    """The open documents as KiCad holds them, plus the project files they need."""
    project = session.project
    copies: dict[DocKind, Path] = {}
    if project.sch_file is not None:
        copies["sch"] = await session.save_copy("sch", dest / project.sch_file.name)
    if project.pcb_file is not None:
        copies["pcb"] = await session.save_copy("pcb", dest / project.pcb_file.name)
    root = project.dir.resolve()
    for source in (project.pro_file, *(project.dir / n for n in PROJECT_SUPPORT_FILES)):
        # Only files that really live in the project (no symlinks out of it).
        if source.is_file() and source.resolve().is_relative_to(root):
            shutil.copyfile(source, dest / source.name)
    return copies


async def run_kicad_check(session: KicadSession, doc: DocKind, settings: Settings) -> str:
    """Saves a copy via the API, runs kicad-cli ERC/DRC on it, returns the JSON text."""
    name = _check_name(doc)
    if (session.project.sch_file if doc == "sch" else session.project.pcb_file) is None:
        raise StructuredToolError(
            f"{name} not possible",
            f"the project has no {'schematic' if doc == 'sch' else 'board'}",
            "Run the check for the document the project has",
        )
    with tempfile.TemporaryDirectory(prefix="cc-check-") as tmp:
        copies = await _save_copies(session, Path(tmp))
        return await run_kicad_check_on_dir(
            settings, session.project, copies[doc], doc, parity="sch" in copies
        )


async def run_kicad_check_on_dir(
    settings: Settings, project: Project, doc_path: Path, doc: DocKind, *, parity: bool = False
) -> str:
    """kicad-cli ERC/DRC on `doc_path`, a document inside a temporary copy of the
    project directory (with every sheet and the project files); returns the JSON text.
    The report is written next to it.

    The copy lives in a temporary directory, so `${KIPRJMOD}` is pointed back at
    the project directory: project-relative libraries resolve exactly as in KiCad.
    `parity`: DRC with `--schematic-parity` (the schematic copy lies next to it).
    """
    name = _check_name(doc)
    workdir = doc_path.parent
    report = workdir / f"cc-{doc}-report.json"
    sub = ["sch", "erc"] if doc == "sch" else ["pcb", "drc"]
    if doc == "pcb" and parity:
        sub.append("--schematic-parity")
    proc = await asyncio.create_subprocess_exec(
        settings.kicad_cli,
        *sub,
        "--format",
        "json",
        "--units",
        "mm",
        "--severity-all",
        "--define-var",
        f"KIPRJMOD={project.dir}",
        "--output",
        str(report),
        str(doc_path),
        cwd=workdir,
        env=kicad_cli_env(settings.kicad_cli, kicad_config_home(settings)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), settings.kicad_api_timeout_s * 4)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise StructuredToolError(
            f"{name} timed out",
            "kicad-cli did not finish in time",
            "Retry once; if it persists, inspect the design with render_view instead",
        ) from None
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap it before the temp dir goes away; the cancellation still wins.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(proc.wait())
        raise
    if not report.is_file():
        detail = (err or out).decode(errors="replace").strip()[-500:]
        raise StructuredToolError(
            f"{name} failed",
            detail or f"kicad-cli exited with {proc.returncode} and wrote no report",
            "Check that the document loads in KiCad",
        )
    return report.read_text(encoding="utf-8")


# Markers for renders: one check per document state, one kicad-cli run at a time.
# Keyed by the session (a restart or reopen is a new state) and its view version,
# which the turn-end publish bumps whenever a document changed (X2).
_MARKER_CACHE: weakref.WeakKeyDictionary[
    KicadSession, dict[tuple[DocKind, int, int], list[dict[str, Any]]]
] = weakref.WeakKeyDictionary()
_MARKER_LOCKS: weakref.WeakKeyDictionary[KicadSession, asyncio.Lock] = weakref.WeakKeyDictionary()


async def cached_violations(session: KicadSession, doc: DocKind) -> list[dict[str, Any]]:
    """`check_violations`, reused while the documents are unchanged (render markers)."""
    lock = _MARKER_LOCKS.setdefault(session, asyncio.Lock())
    key = (doc, session.view_version, session.restart_count)
    async with lock:
        cache = _MARKER_CACHE.setdefault(session, {})
        if key not in cache:
            for stale in [k for k in cache if k[1:] != key[1:]]:
                del cache[stale]
            cache[key] = await check_violations(session, doc)
        return cache[key]


async def check_violations(session: KicadSession, doc: DocKind) -> list[dict[str, Any]]:
    return parse_report(await run_kicad_check(session, doc, session.settings), doc)


def parse_report(raw: str, doc: DocKind) -> list[dict[str, Any]]:
    try:
        return parse_violations(json.loads(raw), doc)
    except (ValueError, TypeError, KeyError) as exc:
        raise StructuredToolError(
            f"{_check_name(doc)} report unreadable",
            f"kicad-cli wrote an unexpected report ({exc})",
            "Retry; if it persists the KiCad version changed its report format",
        ) from exc


def _result(vs: list[dict[str, Any]], args: dict[str, Any]) -> ToolResult:
    severity = args.get("severity")
    shown = [
        v
        for v in vs
        if (not severity or v["severity"] == severity) and matches(args.get("type"), v["type"])
    ]
    return ToolResult(
        data={
            "count": len(shown),
            "total": len(vs),
            "bySeverity": dict(Counter(v["severity"] for v in vs)),
            "violations": shown,
        }
    )


async def run_erc(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    vs = await check_violations(ctx.session, "sch")
    if ctx.editing is not None and ctx.run_id is not None:
        # E-C5 "no double runs": the step-/turn-end ERC delta reuses this report while
        # no mutation followed it (copperchat.changes.erc.current_erc).
        state = ctx.editing.turn(ctx.project.id, ctx.run_id)
        state.last_erc = vs
        state.last_erc_seq = state.call_seq
    return _result(vs, args)


async def run_drc(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _result(await check_violations(ctx.session, "pcb"), args)
