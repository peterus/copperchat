"""The researcher's in-process MCP server ``parts``: datasheet download and notes check."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from copperchat.errors import StructuredToolError
from copperchat.parts import InvalidMpn, part_dir
from copperchat.projects import Project
from copperchat.research.agent import PARTS_SERVER
from copperchat.research.net import Resolver, url_refusal
from copperchat.research.notes import check_notes

MAX_DATASHEET_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 5
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def _folder(project: Project, mpn: str) -> Path:
    try:
        return part_dir(project, mpn)
    except InvalidMpn as exc:
        raise StructuredToolError(
            "Invalid part number", str(exc), "Use the plain MPN, e.g. TPS62133"
        ) from exc


async def download_datasheet(
    project: Project,
    mpn: str,
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> Path:
    """Fetch a PDF into ``docs/parts/<MPN>/datasheet.pdf``. Every redirect hop is checked
    against :func:`url_refusal` (public http(s) hosts only); at most 30 MB; the body must
    start with ``%PDF-``."""
    dest_dir = _folder(project, mpn)
    own = client is None
    c = client or httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT)
    data = b""
    try:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            reason = await url_refusal(current, resolver=resolver)
            if reason is not None:
                raise StructuredToolError("Download refused", reason, "Pass a public PDF URL")
            async with c.stream("GET", current, follow_redirects=False) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise StructuredToolError(
                            "Download failed", "redirect without location", "Try another URL"
                        )
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DATASHEET_BYTES:
                        raise StructuredToolError(
                            "Datasheet too large", "over 30 MB", "Find a smaller PDF"
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                break
        else:
            raise StructuredToolError("Download failed", "too many redirects", "Try another URL")
    except httpx.HTTPError as exc:
        raise StructuredToolError("Download failed", str(exc), "Try another source URL") from exc
    finally:
        if own:
            await c.aclose()
    if not data.startswith(b"%PDF-"):
        raise StructuredToolError(
            "Not a PDF", "the URL did not return a PDF file", "Use a direct link to the PDF"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "datasheet.pdf"
    if target.is_symlink():
        raise StructuredToolError("Download refused", "datasheet.pdf is a symlink", "Ask the user")
    tmp = dest_dir / ".datasheet.pdf.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def _text(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    }
    if is_error:
        out["is_error"] = True
    return out


def notes_check(project: Project, mpn: str) -> dict[str, Any]:
    """Result of ``check_notes`` for ``docs/parts/<MPN>/notes.md`` as tool payload."""
    notes = _folder(project, mpn) / "notes.md"
    if notes.is_symlink() or not notes.is_file():
        raise StructuredToolError(
            "No notes",
            f"{notes.relative_to(project.dir).as_posix()} does not exist",
            "Write notes.md first",
        )
    folder = notes.parent.name
    report = check_notes(notes.read_text(encoding="utf-8", errors="replace"), mpn=folder)
    return {
        "score": round(report.score, 3),
        "passed": report.passed,
        "total": report.total,
        "issues": report.issues,
    }


def parts_mcp_server(project: Project, *, resolver: Resolver | None = None) -> McpSdkServerConfig:
    async def _download(args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = await download_datasheet(
                project, str(args.get("mpn", "")), str(args.get("url", "")), resolver=resolver
            )
        except StructuredToolError as err:
            return _text({"error": err.to_dict()}, is_error=True)
        return _text({"saved": path.relative_to(project.dir).as_posix()})

    async def _check(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(notes_check(project, str(args.get("mpn", ""))))
        except StructuredToolError as err:
            return _text({"error": err.to_dict()}, is_error=True)

    mpn_schema = {"type": "string", "description": "Bare manufacturer part number"}
    tools: list[SdkMcpTool[Any]] = [
        SdkMcpTool(
            name="download_datasheet",
            description="Download a datasheet PDF (direct http(s) link) into "
            "docs/parts/<MPN>/datasheet.pdf.",
            input_schema={
                "type": "object",
                "properties": {"mpn": mpn_schema, "url": {"type": "string"}},
                "required": ["mpn", "url"],
            },
            handler=_download,
        ),
        SdkMcpTool(
            name="check_notes",
            description="Validate docs/parts/<MPN>/notes.md against the part-notes template; "
            "returns a score and the list of issues to fix.",
            input_schema={
                "type": "object",
                "properties": {"mpn": mpn_schema},
                "required": ["mpn"],
            },
            handler=_check,
        ),
    ]
    return create_sdk_mcp_server(name=PARTS_SERVER, version="1.0.0", tools=tools)
