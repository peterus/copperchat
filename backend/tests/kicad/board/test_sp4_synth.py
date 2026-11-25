"""The committed `sp4_synth` fixture (P1b): schematic only, ERC-clean, the circuit classes
the board phases rely on, stock footprints, a clean netlist import, and unchanged since
it was generated (golden summary)."""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession
from tests.kicad.board.test_sp4_synth_build import (
    GOLDEN,
    NAME,
    standalone_server,
    summarize,
)
from tests.support.edit import call_tool, edit_ctx
from tests.support.kicad_cli import cli_netlist
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import FIXTURES

pytestmark = pytest.mark.kicad

STOCK_FOOTPRINTS = Path("/usr/share/kicad/footprints")


async def _netlist(s: KicadSession, tmp_path: Path) -> ET.Element:
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    return cli_netlist(s.settings.kicad_cli, dest, tmp_path)


async def test_opens_schematic_only(synth_session: KicadSession) -> None:
    s = synth_session
    assert s.project.pcb_file is None
    assert HistoryService.docs_of(s.project) == {"sch"}
    info, err = await call_tool(edit_ctx(s), "project_info")
    assert not err, info
    assert info["name"] == NAME and info["files"]["board"] is None


async def test_erc_clean(synth_session: KicadSession) -> None:
    erc, err = await call_tool(edit_ctx(synth_session), "run_erc")
    assert not err, erc
    # accepted ERC items (scripts/make-sp4-synth.py): none
    assert erc["violations"] == [], erc["violations"]


def _nodes(root: ET.Element) -> dict[str, set[str]]:
    """net name -> references on it."""
    return {
        n.get("name", ""): {x.get("ref", "") for x in n.iterfind("node")}
        for n in root.iterfind("./nets/net")
    }


def _pins_of(root: ET.Element) -> dict[str, int]:
    count: dict[str, int] = {}
    for net in root.iterfind("./nets/net"):
        for node in net.iterfind("node"):
            ref = node.get("ref", "")
            count[ref] = count.get(ref, 0) + 1
    return count


def _comps(root: ET.Element) -> dict[str, tuple[str, str, str]]:
    """ref -> (library, symbol name, sheet path)."""
    out = {}
    for c in root.iterfind("./components/comp"):
        src, sheet = c.find("libsource"), c.find("sheetpath")
        assert src is not None and sheet is not None
        out[c.get("ref", "")] = (src.get("lib", ""), src.get("part", ""), sheet.get("names", ""))
    return out


async def test_netlist_classes(synth_session: KicadSession, tmp_path: Path) -> None:
    root = await _netlist(synth_session, tmp_path)
    comps, nets, pins = _comps(root), _nodes(root), _pins_of(root)

    def refs(lib: str, prefix: str = "") -> set[str]:
        return {r for r, (lb, part, _) in comps.items() if lb == lib and part.startswith(prefix)}

    def sheet(ref: str) -> str:
        return comps[ref][2]

    def nets_of(ref: str) -> set[str]:
        return {n for n, members in nets.items() if ref in members}

    assert {sheet(r) for r in comps} == {"/", "/Power/"}
    mcus = [r for r, n in pins.items() if n >= 20 and sheet(r) == "/"]
    assert len(mcus) == 1, pins
    mcu = mcus[0]
    bucks = refs("Regulator_Switching")
    inductors = {r for r in refs("Device") if comps[r][1] == "L"}  # not "LED"
    assert bucks and {sheet(r) for r in bucks} == {"/Power/"}
    assert inductors and {sheet(r) for r in inductors} == {"/Power/"}

    caps = {r for r in refs("Device") if comps[r][1] == "C"}
    (xtal,) = refs("Device", "Crystal")
    signal = nets_of(xtal) - {"GND"}  # the two non-GND pins
    assert len(signal) == 2
    for n in signal:
        assert nets[n] & caps and mcu in nets[n], (n, nets[n])

    (usb,) = refs("Connector", "USB_C")
    (esd,) = refs("Power_Protection")
    for pair in ("USB_DP", "USB_DN"):
        (name,) = [n for n in nets if n.rsplit("/", 1)[-1] == pair]
        assert {usb, esd, mcu} <= nets[name], (pair, nets[name])

    assert refs("Device", "LED")
    assert refs("Connector_Generic", "Conn_01x")
    rail_caps = {c for c in caps if {"+3V3", "GND"} <= nets_of(c)}
    assert len(rail_caps) >= 3 and {"+3V3", "GND"} <= nets_of(mcu)


async def test_every_symbol_has_existing_footprint(
    synth_session: KicadSession, tmp_path: Path
) -> None:
    root = await _netlist(synth_session, tmp_path)
    footprints = {c.get("ref", ""): c.findtext("footprint", "") for c in root.iterfind(".//comp")}
    assert footprints and all(footprints.values()), footprints
    missing = {
        ref: fp
        for ref, fp in footprints.items()
        if not (
            STOCK_FOOTPRINTS / f"{fp.split(':')[0]}.pretty" / f"{fp.split(':')[1]}.kicad_mod"
        ).is_file()
    }
    assert not missing, missing


async def test_netlist_imports_cleanly(tmp_path: Path) -> None:
    """A dry-run netlist import into a new board on a temporary copy: no pin/pad mismatch,
    one new footprint per part (P2's sync report must be clean on this fixture)."""
    from kipy.proto.common.types import DocumentType

    work = tmp_path / NAME
    shutil.copytree(FIXTURES / NAME, work)
    root_sch = work / f"{NAME}.kicad_sch"
    netlist = tmp_path / f"{NAME}.net"
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(netlist),
         str(root_sch)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    parts = len(cli_netlist(KICAD_CLI, root_sch, tmp_path).findall("./components/comp"))
    pcb = work / f"{NAME}.kicad_pcb"
    with standalone_server(KICAD_CLI) as client:
        client.create_document(str(pcb), DocumentType.DOCTYPE_PCB)
        client.get_board().save()
        for doc in client.get_open_documents(DocumentType.DOCTYPE_PCB):
            client.close_document(doc)
        client.open_document(str(pcb), DocumentType.DOCTYPE_PCB)
        board = client.get_board()
        result = board.import_netlist(str(netlist), dry_run=True)
        assert result.error_count == 0, result.report
        assert result.warning_count == 0, result.report
        assert result.new_footprint_count == parts == 26, result
        assert board.get_footprints() == []  # a dry run changes nothing


async def test_matches_golden(tmp_path: Path) -> None:
    work = tmp_path / NAME
    shutil.copytree(FIXTURES / NAME, work)
    assert sorted(p.name for p in work.iterdir()) == sorted(
        [f"{NAME}.kicad_pro", f"{NAME}.kicad_sch", "power.kicad_sch"]
    )  # no board, no local state committed
    summary = summarize(KICAD_CLI, work / f"{NAME}.kicad_sch", tmp_path / "cli")
    assert summary == json.loads(GOLDEN.read_text())
