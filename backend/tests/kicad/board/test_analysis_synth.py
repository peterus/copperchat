"""`pcb_analyze` on the `sp4_synth` board (SP4 P3a Task 5): the classification matches the P1b
circuit (index "As built (P1b)"), the full result matches the golden, and a project without a
board is refused. The synth tests share one api-server (`synth_board`, module scope)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.board.analysis import pcb_analyze
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad
GOLDEN = Path(__file__).resolve().parent / "goldens" / "sp4_synth_classification.json"


def _leaf(net: str) -> str:
    return net.rsplit("/", 1)[-1]


async def _analyze(s: KicadSession, **args: Any) -> dict[str, Any]:
    data = (await pcb_analyze(edit_ctx(s), args)).data
    assert data is not None
    return data


@pytest.mark.asyncio(loop_scope="module")
async def test_classification_matches_circuit(synth_board: KicadSession) -> None:
    data = await _analyze(synth_board)
    assert all(data[k] == 0 for k in data if k.endswith("_omitted"))
    assert data["board_md_errors"] == [] and data["overrides_applied"] == []
    parts, nets = data["parts"], data["nets"]
    assert len(parts) == 26

    (sw,) = data["switchers"]
    assert sw["ic"] == "U3" and parts["U3"] == "switching_regulator"
    assert sw["inductor"] == "L1" and parts["L1"] == "inductor"
    assert _leaf(sw["sw_net"]) == "SW" and nets[sw["sw_net"]] == "switch_node"
    assert sw["in_caps"] == ["C7"]
    assert sw["out_caps"] == ["C8", "C9"]
    assert sw["fb_parts"] == ["R5", "R6"]

    (xtal,) = data["crystals"]
    assert xtal["ref"] == "Y1" and parts["Y1"] == "crystal"
    assert xtal["ic"] == "U1"
    assert xtal["load_caps"] == ["C4", "C5"]

    (pair,) = data["pairs"]
    assert (_leaf(pair["p"]), _leaf(pair["n"])) == ("USB_DP", "USB_DN")
    assert nets[pair["p"]] == nets[pair["n"]] == "diff_pair"

    dec = {d["cap"]: d for d in data["decoupling"]}
    for cap in ("C1", "C2", "C3"):
        assert dec[cap]["ic"] == "U1", dec[cap]
        assert _leaf(dec[cap]["supply_net"]) == "+3V3", dec[cap]
        assert parts[cap] == "decoupling_cap"

    assert parts["J1"] == "connector"
    assert parts["H1"] == parts["H2"] == "mounting_hole"
    assert nets["GND"] == "ground"
    assert nets["VBUS"] == nets["+3V3"] == "supply"

    blocks = {b["anchor"]: b for b in data["blocks"]}
    assert blocks["U1"]["sheet"] == "root"
    # the sheet is named "Power" in sp4_synth (net prefix /Power/)
    assert blocks["U3"]["sheet"].casefold() == "power"
    assert {"L1", "C7", "C8", "C9", "R5", "R6"} <= set(blocks["U3"]["refs"])
    assert {"Y1", "C1", "C2", "C3", "C4", "C5", "R4", "D1"} <= set(blocks["U1"]["refs"])


@pytest.mark.asyncio(loop_scope="module")
async def test_classification_golden(synth_board: KicadSession) -> None:
    data = await _analyze(synth_board)
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert data == json.loads(GOLDEN.read_text())
    for scope in ("parts", "nets", "blocks"):
        part = await _analyze(synth_board, scope=scope)
        assert part == {k: v for k, v in data.items() if k in part}


async def test_analyze_boardless(blank_nopcb_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as err:
        await pcb_analyze(edit_ctx(blank_nopcb_session), {})
    assert err.value.code == "no_board"
