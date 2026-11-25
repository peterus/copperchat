"""Generates the `sp4_synth` fixture (P1b, spec §18.2) and checks it against the golden.

`test_build_sp4_synth` starts from KiCad's own empty project (template `.kicad_pro`, a
schematic created and saved by a standalone api-server, no board), builds the circuit of
`sp4_synth_steps.py` through the real schematic tools, saves, and compares the summary of
KiCad's ERC report and netlist with `goldens/sp4_synth_summary.json` (`UPDATE_GOLDENS=1`
rewrites it). With `COPPERCHAT_SP4_SYNTH_OUT` set (scripts/make-sp4-synth.py) the
KiCad-saved files are copied verbatim to `$COPPERCHAT_SP4_SYNTH_OUT/sp4_synth/`.

`netlist_summary` / `erc_summary` are pure (unit-tested in
tests/unit/test_sp4_synth_summary.py): the summary is what "the same fixture" means —
parts, nets, sheets, ERC counts; never UUIDs, timestamps, positions or file bytes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.kicad_session import KicadSessionManager
from copperchat.kicad_session.launcher import kicad_cli_env
from copperchat.projects import discover_projects
from tests.kicad.board.sp4_synth_steps import EXPECTED_PARTS, POWER_STEPS, ROOT_STEPS
from tests.support.kicad_cli import cli_netlist
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import real_settings

NAME = "sp4_synth"
GOLDEN = Path(__file__).resolve().parent / "goldens" / "sp4_synth_summary.json"
TEMPLATE = Path("/usr/share/kicad/template/kicad.kicad_pro")
OUT_ENV = "COPPERCHAT_SP4_SYNTH_OUT"
# The files of the fixture: KiCad-written, copied verbatim (never .kicad_prl, .lck, backups).
FIXTURE_FILES = (f"{NAME}.kicad_pro", f"{NAME}.kicad_sch", "power.kicad_sch")


# -- summary (pure) --------------------------------------------------------------------


def netlist_summary(root: ET.Element) -> dict[str, Any]:
    """Parts, nets (name -> sorted "REF.pin", single-node nets kept) and sheet paths of a
    `kicadxml` netlist, every mapping sorted by key."""
    parts: dict[str, dict[str, str]] = {}
    for comp in root.iterfind("./components/comp"):
        src = comp.find("libsource")
        sheet = comp.find("sheetpath")
        parts[comp.get("ref", "")] = {
            "value": comp.findtext("value", ""),
            "footprint": comp.findtext("footprint", ""),
            "lib_id": "" if src is None else f"{src.get('lib', '')}:{src.get('part', '')}",
            "sheet": "" if sheet is None else sheet.get("names", ""),
        }
    nets = {
        net.get("name", ""): sorted(
            f"{node.get('ref', '')}.{node.get('pin', '')}" for node in net.iterfind("node")
        )
        for net in root.iterfind("./nets/net")
    }
    sheets = sorted(s.get("name", "") for s in root.iterfind("./design/sheet"))
    return {
        "parts": dict(sorted(parts.items())),
        "nets": dict(sorted(nets.items())),
        "sheets": sheets,
    }


def erc_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Errors counted, warnings counted by type (excluded items ignored)."""
    violations = [v for sheet in report.get("sheets", []) for v in sheet.get("violations", [])]
    warnings = Counter(v.get("type", "") for v in violations if v.get("severity") == "warning")
    return {
        "errors": sum(1 for v in violations if v.get("severity") == "error"),
        "warnings": dict(sorted(warnings.items())),
    }


# -- KiCad -----------------------------------------------------------------------------


def cli_erc(kicad_cli: str, root_sch: Path, out_dir: Path) -> dict[str, Any]:
    """`kicad-cli sch erc --format json` (errors and warnings) of the whole hierarchy."""
    out = out_dir / f"{root_sch.stem}-erc.json"
    env = kicad_cli_env(kicad_cli, out_dir / "kicad-config")  # never the user's config
    subprocess.run(
        [kicad_cli, "sch", "erc", "--format", "json", "--severity-error",
         "--severity-warning", "-o", str(out), str(root_sch)],
        check=True, capture_output=True, timeout=120, env=env,
    )  # fmt: skip
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return report


def summarize(kicad_cli: str, root_sch: Path, work: Path) -> dict[str, Any]:
    """The comparison key of a saved project: netlist summary + ERC summary."""
    work.mkdir(parents=True, exist_ok=True)
    summary = netlist_summary(cli_netlist(kicad_cli, root_sch, work))
    summary["erc"] = erc_summary(cli_erc(kicad_cli, root_sch, work))
    return summary


def _wait_for_server(proc: subprocess.Popen[bytes], url: str, timeout_s: float) -> Any:
    from kipy import KiCad

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"api-server exited with code {proc.returncode}")
        try:
            client = KiCad(socket_path=url, client_name="make-sp4-synth", timeout_ms=60_000)
            client.ping()  # type: ignore[no-untyped-call]
            return client
        except Exception:  # not ready yet
            time.sleep(0.2)
    raise RuntimeError(f"api-server not ready after {timeout_s:.0f}s")


@contextmanager
def standalone_server(kicad_cli: str = KICAD_CLI) -> Iterator[Any]:
    """A kipy client of a private `kicad-cli api-server` (own socket, own config home
    seeded with the stock library tables as CopperChat's sessions), terminated on exit."""
    with tempfile.TemporaryDirectory(prefix="cc-sp4-") as tmp:
        socket = Path(tmp) / "api.sock"
        env = kicad_cli_env(kicad_cli, Path(tmp) / "config")  # stock library tables
        proc = subprocess.Popen(
            [kicad_cli, "api-server", "--socket", str(socket)],
            cwd=tmp,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield _wait_for_server(proc, f"ipc://{socket}", 60)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def create_empty_project(root: Path, name: str, kicad_cli: str = KICAD_CLI) -> Path:
    """KiCad's empty project `root/name/` without a board (port of
    scripts/make-blank-fixture.py): the template `.kicad_pro` copied verbatim and a
    schematic created and saved by a standalone api-server."""
    from kipy.proto.common.types import DocumentType

    dest = root / name
    dest.mkdir(parents=True)
    with standalone_server(kicad_cli) as client:
        client.create_document(str(dest / f"{name}.kicad_sch"), DocumentType.DOCTYPE_SCHEMATIC)
        client.get_schematic().save()
    shutil.copyfile(TEMPLATE, dest / f"{name}.kicad_pro")
    for junk in (*dest.glob("*.lck"), *dest.glob("*.kicad_prl")):
        junk.unlink()
    return dest


def copy_out(project_dir: Path, out: Path) -> Path:
    dest = out / NAME
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for name in FIXTURE_FILES:
        shutil.copy2(project_dir / name, dest / name)  # verbatim: written by KiCad
    return dest


def check_golden(summary: dict[str, Any]) -> None:
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    if not GOLDEN.is_file():
        pytest.fail(f"{GOLDEN.name} missing: run scripts/make-sp4-synth.py with UPDATE_GOLDENS=1")
    assert summary == json.loads(GOLDEN.read_text())


# -- the generator ---------------------------------------------------------------------


@pytest.mark.kicad
async def test_build_sp4_synth(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    from tests.kicad.edit.test_scenarios import blank_ctx, run_script

    settings = real_settings(make_settings)
    project_dir = create_empty_project(settings.projects_root, NAME, settings.kicad_cli)
    assert not list(project_dir.glob("*.kicad_pcb"))
    project = next(p for p in discover_projects(settings.projects_root) if p.name == NAME)
    manager = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "edit.db")
    try:
        session = await manager.open(project)
        ctx = await blank_ctx(session, db)
        refs = await run_script(ctx, ROOT_STEPS)
        await run_script(ctx, POWER_STEPS, refs)
        assert refs == {alias: alias for alias in EXPECTED_PARTS}  # references as planned
        await session.save("sch")
    finally:
        await manager.close_all()
        await db.close()

    assert sorted(p.name for p in project_dir.glob("*.kicad_sch")) == sorted(FIXTURE_FILES[1:])
    summary = summarize(settings.kicad_cli, project_dir / f"{NAME}.kicad_sch", tmp_path / "cli")
    assert summary["erc"] == {"errors": 0, "warnings": {}}, summary["erc"]
    check_golden(summary)
    if out := os.environ.get(OUT_ENV):
        copy_out(project_dir, Path(out))
