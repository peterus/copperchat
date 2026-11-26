"""COPPERCHAT_FREEROUTING removes only `pcb_route_fill` (SP4 P6b Task 1, index B-C1)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent import fake, sdk_backend
from copperchat.agent.fake import FakeAgent
from copperchat.agent.profiles import FILL_TOOL, PCB_WRITES, profile_specs
from copperchat.agent.sdk_backend import SdkStreamingBackend
from copperchat.config import Settings
from copperchat.db.repos.conversations import ConversationRow
from copperchat.projects import Project
from copperchat.tools import ToolResult, ToolSpec


def _spec(name: str) -> ToolSpec:
    async def handler(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(text="ok")

    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, handler=handler)


SPECS = [_spec(n) for n in sorted(PCB_WRITES | {"pcb_status", "pcb_render"})]


def _names(kind: str, **flags: bool) -> set[str]:
    return {s.name for s in profile_specs(kind, SPECS, board_tools=True, **flags)}


def test_freerouting_off_drops_only_fill() -> None:
    for kind in ("board", "free"):
        on, off = _names(kind), _names(kind, freerouting=False)
        assert FILL_TOOL in on, kind
        assert on - off == {FILL_TOOL}, kind
        assert "pcb_route" in off, kind
    assert FILL_TOOL == "pcb_route_fill"


class _Stop(Exception):
    pass


def _conversation(kind: str = "board") -> ConversationRow:
    return ConversationRow(
        id="c1",
        project_id="p1",
        seq=1,
        title="t",
        type=kind,  # type: ignore[arg-type]
        status="open",  # type: ignore[arg-type]
        archived=False,
        sdk_session_id=None,
        docs_seen={},
        created_at="",
        updated_at="",
        last_message_at=None,
        closed_at=None,
    )


async def _empty() -> AsyncIterator[Any]:
    if False:  # pragma: no cover
        yield None


def _spy(seen: list[dict[str, Any]]) -> Callable[..., Any]:
    def spy(kind: str, specs: Any, **kw: Any) -> Any:
        seen.append(kw)
        names = {s.name for s in profile_specs(kind, specs, **kw)}
        seen.append({"names": names})
        raise _Stop

    return spy


@pytest.mark.parametrize("module", ["sdk_backend", "fake"])
async def test_both_call_sites_pass_flag(
    module: str,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(board_tools=True, freerouting=False)
    seen: list[dict[str, Any]] = []
    mod = sdk_backend if module == "sdk_backend" else fake
    monkeypatch.setattr(mod, "profile_specs", _spy(seen))
    backend: Any
    if module == "sdk_backend":
        backend = SdkStreamingBackend(
            settings=settings,
            specs=SPECS,
            ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        )
    else:
        backend = FakeAgent(specs=SPECS, settings=settings)
    project = Project(
        id="p1",
        name="demo",
        rel_path="demo",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )
    with pytest.raises(_Stop):
        async for _ in backend.start(project, _conversation(), _empty(), None):
            pass
    assert seen[0]["freerouting"] is False
    assert seen[0]["board_tools"] is True
    assert FILL_TOOL not in seen[1]["names"]
    assert "pcb_route" in seen[1]["names"]
