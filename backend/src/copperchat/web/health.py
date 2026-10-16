from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["health"])


async def kicad_cli_version(cli: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except (FileNotFoundError, PermissionError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        return None
    text = out.decode().strip()
    return text if proc.returncode == 0 and text else None


@router.get("/health")
async def health(request: Request) -> dict[str, str | None]:
    state = request.app.state
    if not hasattr(state, "kicad_version"):
        state.kicad_version = await kicad_cli_version(state.settings.kicad_cli)
    return {"status": "ok", "kicad": state.kicad_version}
