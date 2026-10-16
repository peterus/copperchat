from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadSession
from copperchat.kicad_session.launcher import kicad_cli_env, kicad_config_home
from copperchat.kicad_tools.filtering import matches
from copperchat.tools.spec import ToolContext, ToolResult

# KiCad-written project files the checks need next to the saved copies: the
# project settings (ERC/DRC severities, design rules) and the project library
# tables. Byte copies; never modified.
_PROJECT_SUPPORT_FILES = ("sym-lib-table", "fp-lib-table")
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
    for source in (project.pro_file, *(project.dir / n for n in _PROJECT_SUPPORT_FILES)):
        # Only files that really live in the project (no symlinks out of it).
        if source.is_file() and source.resolve().is_relative_to(root):
            shutil.copyfile(source, dest / source.name)
    return copies


async def run_kicad_check(session: KicadSession, doc: DocKind, settings: Settings) -> str:
    """Saves a copy via the API, runs kicad-cli ERC/DRC on it, returns the JSON text.

    The copy lives in a temporary directory, so `${KIPRJMOD}` is pointed back at
    the project directory: project-relative libraries resolve exactly as in KiCad.
    """
    name = _check_name(doc)
    if (session.project.sch_file if doc == "sch" else session.project.pcb_file) is None:
        raise StructuredToolError(
            f"{name} not possible",
            f"the project has no {'schematic' if doc == 'sch' else 'board'}",
            "Run the check for the document the project has",
        )
    with tempfile.TemporaryDirectory(prefix="cc-check-") as tmp:
        copies = await _save_copies(session, Path(tmp))
        report = Path(tmp) / "report.json"
        sub = ["sch", "erc"] if doc == "sch" else ["pcb", "drc"]
        if doc == "pcb" and "sch" in copies:
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
            f"KIPRJMOD={session.project.dir}",
            "--output",
            str(report),
            str(copies[doc]),
            cwd=tmp,
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
            raise
        if not report.is_file():
            detail = (err or out).decode(errors="replace").strip()[-500:]
            raise StructuredToolError(
                f"{name} failed",
                detail or f"kicad-cli exited with {proc.returncode} and wrote no report",
                "Check that the document loads in KiCad",
            )
        return report.read_text(encoding="utf-8")


async def check_violations(session: KicadSession, doc: DocKind) -> list[dict[str, Any]]:
    raw = await run_kicad_check(session, doc, session.settings)
    try:
        return parse_violations(json.loads(raw), doc)
    except (ValueError, TypeError, KeyError) as exc:
        raise StructuredToolError(
            f"{_check_name(doc)} report unreadable",
            f"kicad-cli wrote an unexpected report ({exc})",
            "Retry; if it persists the KiCad version changed its report format",
        ) from exc


async def _check(ctx: ToolContext, doc: DocKind, args: dict[str, Any]) -> ToolResult:
    vs = await check_violations(ctx.session, doc)
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
    return await _check(ctx, "sch", args)


async def run_drc(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _check(ctx, "pcb", args)
