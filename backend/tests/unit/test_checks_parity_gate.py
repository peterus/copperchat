"""The legacy parity path (`run_kicad_check_on_dir`, used by `run_drc` and the board
render's markers) runs its `kicad-cli pcb drc --schematic-parity` inside the heavy-process
gate (SP4 round 2 R6); ERC and DRC without parity stay ungated."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from copperchat.board import heavy
from copperchat.board.heavy import heavy_slot
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import checks


@pytest.mark.parametrize(
    ("doc", "parity", "gated"),
    [("pcb", True, True), ("pcb", False, False), ("sch", False, False), ("sch", True, False)],
)
async def test_run_kicad_check_parity_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, doc: str, parity: bool, gated: bool
) -> None:
    seen: list[tuple[list[str], str | None]] = []

    async def fake_cli(settings: Any, args: list[str], **kw: Any) -> Any:
        seen.append((args, heavy.holder()))
        Path(args[args.index("--output") + 1]).write_text("{}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(checks, "run_kicad_cli", fake_cli)
    path = tmp_path / f"x.kicad_{'pcb' if doc == 'pcb' else 'sch'}"
    project = SimpleNamespace(dir=tmp_path)
    raw = await checks.run_kicad_check_on_dir(
        SimpleNamespace(kicad_api_timeout_s=1.0),  # type: ignore[arg-type]
        project,  # type: ignore[arg-type]
        path,
        doc,  # type: ignore[arg-type]
        parity=parity,
    )
    assert raw == "{}"
    [(args, holder)] = seen
    assert ("--schematic-parity" in args) is gated
    assert holder == ("parity" if gated else None)
    assert heavy.holder() is None


async def test_run_kicad_check_parity_gate_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cli(*a: Any, **kw: Any) -> Any:
        pytest.fail("kicad-cli started without the gate")

    monkeypatch.setattr(checks, "run_kicad_cli", fake_cli)
    monkeypatch.setattr(checks, "HEAVY_WAIT_S", 0.05)
    async with heavy_slot("freerouting", wait_s=1):
        with pytest.raises(StructuredToolError) as ei:
            await checks.run_kicad_check_on_dir(
                SimpleNamespace(kicad_api_timeout_s=1.0),  # type: ignore[arg-type]
                SimpleNamespace(dir=tmp_path),  # type: ignore[arg-type]
                tmp_path / "x.kicad_pcb",
                "pcb",
                parity=True,
            )
    assert ei.value.code == "board_compute_timeout"


async def test_marker_check_waits_only_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The render markers' DRC waits at most MARKER_GATE_WAIT_S for the gate (the live
    view's HTTP request must not stall for HEAVY_WAIT_S during a fill); a timeout is not
    cached, the next render tries again."""
    waits: list[float] = []

    async def fake_check(session: Any, doc: str, *, heavy_wait_s: float) -> list[Any]:
        waits.append(heavy_wait_s)
        raise heavy.heavy_timeout("freerouting", heavy_wait_s)

    class _Session:
        view_version = 1
        restart_count = 0

    monkeypatch.setattr(checks, "check_violations", fake_check)
    session = _Session()
    for _ in range(2):
        with pytest.raises(StructuredToolError):
            await checks.cached_violations(session, "pcb")  # type: ignore[arg-type]
    assert waits == [checks.MARKER_GATE_WAIT_S] * 2
    assert checks.MARKER_GATE_WAIT_S < checks.HEAVY_WAIT_S


async def test_check_wait_passed_to_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cli(*a: Any, **kw: Any) -> Any:
        pytest.fail("kicad-cli started without the gate")

    monkeypatch.setattr(checks, "run_kicad_cli", fake_cli)
    async with heavy_slot("freerouting", wait_s=1):
        with pytest.raises(StructuredToolError) as ei:
            await checks.run_kicad_check_on_dir(
                SimpleNamespace(kicad_api_timeout_s=1.0),  # type: ignore[arg-type]
                SimpleNamespace(dir=tmp_path),  # type: ignore[arg-type]
                tmp_path / "x.kicad_pcb",
                "pcb",
                parity=True,
                heavy_wait_s=0.05,
            )
    assert ei.value.code == "board_compute_timeout"
