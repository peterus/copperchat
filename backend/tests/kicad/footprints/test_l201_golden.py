"""The owner's L201 end to end on a real api-server (F5 Task 2, spec §16.2): `fp_create`
with the datasheet numbers in `main`, the `fp_get` golden, the file and the project row,
`fp_check` against the inductor symbol, the courtyard drawn by KiCad, approval in a later
turn after a human message, assignment in the sibling project `io` and KiCad's own ERC.

Runs in the run-engine rig of `test_turn_end.py` (the repo of `repo_session`: `main` and
`io` without a board) because `fp_approve` needs the conversation, the run's human message
and the review stamps. Every KiCad file is written by KiCad; the only file the test writes
into the synthetic repo is a placeholder PDF (the cited datasheet, not a KiCad file)."""

from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from copperchat.projects import discover_projects
from tests.kicad.footprints.test_assign_local import kinds, place_inductor
from tests.kicad.footprints.test_fp_approve import OK_QUOTE, tool, user_says
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    URI,
    l201_args,
    lib_dir,
    project_rows,
)
from tests.kicad.footprints.test_fp_render import _PATH_TOKEN, SVG
from tests.kicad.footprints.test_turn_end import RUN1, RUN2, Rig
from tests.kicad.footprints.test_turn_end import rig as rig
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "fp_get_L201.json"
DATASHEET = "docs/parts/SRN3012BTA-2R2M/datasheet.pdf"
LOCATION = "p.2, 'Recommended layout'"
# The pinned KiCad (52b43fb8) ships no `Inductor` symbol library (only `Device`), so the
# inductor symbol is `Device:L`; the test below asserts `Inductor:L` is unknown, so a KiCad
# bump that adds it shows up here and the choice is made again.
SYMBOL = "Device:L"


def owner_args() -> dict[str, Any]:
    """The owner's numbers (spike 3A): 1.3 x 3.5 mm pads at x = +-1.1 (pad 1 left), body
    3.0 x 3.0, courtyard 4.0 x 4.0, silk auto, the local datasheet as the source."""
    return l201_args(
        keywords="inductor shielded power Bourns SRN3012BTA",
        courtyard={"rect": [4.0, 4.0]},
        silkscreen="auto",
        source={"document": DATASHEET, "location": LOCATION},
        symbol=SYMBOL,
    )


def commit_datasheet(root: Path) -> None:
    """A small placeholder PDF at the cited path, committed into the synthetic repo."""
    pdf = root / DATASHEET
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n% placeholder for the Bourns SRN3012BTA datasheet\n%%EOF\n")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", DATASHEET], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "datasheet"], check=True)


def normalised(data: dict[str, Any]) -> dict[str, Any]:
    """`fp_get` output without its run-dependent parts: a `footprint_libraries_loaded`
    note carries the measured activation `seconds` (wall clock), so `seconds` is replaced
    by a marker. Nothing else in `fp_get` is volatile: no KIIDs, timestamps or paths."""
    out = json.loads(json.dumps(data))
    for note in out.get("notes", []):
        if isinstance(note, dict) and "seconds" in note:
            note["seconds"] = "<seconds>"
    return out  # type: ignore[no-any-return]


def drawn_extent(svg: Path) -> tuple[float, float, float, float]:
    """The bbox of KiCad's path centrelines in a render's SVG (absolute M/L/Z only)."""
    root = ET.fromstring(svg.read_bytes())
    (nested,) = root.findall(f"{SVG}svg")
    xs: list[float] = []
    ys: list[float] = []
    commands: set[str] = set()
    for path in nested.iter(f"{SVG}path"):
        nums: list[float] = []
        for num, cmd in _PATH_TOKEN.findall(path.get("d", "")):
            if cmd:
                commands.add(cmd.upper())
            else:
                nums.append(float(num))
        xs += nums[0::2]
        ys += nums[1::2]
    assert commands <= {"M", "L", "Z"}, commands
    assert xs and ys
    return min(xs), min(ys), max(xs), max(ys)


async def test_l201_end_to_end(rig: Rig) -> None:
    commit_datasheet(rig.root)
    session = rig.session
    await session.ensure_symbol_libraries()

    # the inductor symbol of the pinned KiCad (see SYMBOL)
    ctx0 = edit_ctx(session)
    out, err = await call_tool(ctx0, "lib_get_symbol", lib_id="Inductor:L")
    assert err, out
    out, err = await call_tool(ctx0, "lib_get_symbol", lib_id=SYMBOL)
    assert not err, out

    # -- turn 1: create, golden, file, row, check, render ---------------------------------
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    created = await tool(rig, RUN1, "fp_create", **owner_args())
    assert created["created"] == [LIB_ID] and created["needs_user_review"] is True
    assert created["check"]["passed"], created["check"]

    got = await tool(rig, RUN1, "fp_get", lib_id=LIB_ID, detail="full")
    table = normalised(got)
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    assert table == json.loads(GOLDEN.read_text())
    # the golden holds the owner's numbers (also guards a regenerated golden)
    pads = {p["number"]: p for p in got["pads"]}
    assert pads["1"]["at"] == [-1.1, 0.0] and pads["2"]["at"] == [1.1, 0.0]
    assert pads["1"]["size"] == pads["2"]["size"] == [1.3, 3.5]
    assert got["source"] == f"{DATASHEET}, {LOCATION}"

    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    assert file == rig.root.resolve() / "footprints" / f"{NICK}.pretty" / f"{NAME}.kicad_mod"
    assert file.is_file()
    assert (await project_rows(session))[NICK] == URI
    text = file.read_text()  # read-only look at the KiCad-written file
    assert '(descr "' in text and '(tags "' in text and "(attr smd" in text
    assert "(model" not in text

    checked = await tool(rig, RUN1, "fp_check", lib_id=LIB_ID, symbol=SYMBOL)
    assert checked["passed"], checked["findings"]
    assert checked["geometry_hash"] == got["geometry_hash"]

    render = await tool(rig, RUN1, "fp_render", lib_id=LIB_ID, layers=["F.CrtYd"])
    assert render["geometry_hash"] == got["geometry_hash"]
    svg = session.project.dir / ".kicad-agent" / "renders" / render["svg"]
    # `extent_mm` includes the render margins (F2 as built): the drawn courtyard is measured
    x0, y0, x1, y1 = drawn_extent(svg)
    assert x1 - x0 == pytest.approx(4.0, abs=0.01)
    assert y1 - y0 == pytest.approx(4.0, abs=0.01)
    await rig.end_turn(RUN1)

    # -- turn 2: the user's OK, approval ----------------------------------------------------
    await user_says(rig, RUN2)
    approved = await tool(
        rig,
        RUN2,
        "fp_approve",
        lib_id=LIB_ID,
        geometry_hash=got["geometry_hash"],
        user_quote=OK_QUOTE,
    )
    assert approved["review"] == "approved"
    assert approved["geometry_hash"] == got["geometry_hash"]
    again = await tool(rig, RUN2, "fp_get", lib_id=LIB_ID)
    assert again["review"] == "approved" and again["geometry_hash"] == got["geometry_hash"]
    await rig.end_turn(RUN2)

    # -- io: assignment through sch_set_fields, KiCad's ERC --------------------------------
    io_dir = (rig.root / "hardware" / "io").resolve()
    io_project = next(
        p for p in discover_projects(session.settings.projects_root) if p.dir.resolve() == io_dir
    )
    io = await rig.history.sessions.open(io_project)
    io_ctx = edit_ctx(io, rig.db)
    ref = await place_inductor(io_ctx, io)
    out, err = await call_tool(io_ctx, "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    if err:
        pytest.fail(json.dumps(out, indent=1))
    # io has no library row yet: the edit adds it, so the review state is unknown here
    # (specs_place: footprint_review_unknown) and is read with fp_get afterwards
    assert "footprint_review_unknown" in kinds(out), out
    assert f'"{LIB_ID}"' in (io_dir / "blank.kicad_sch").read_text()
    in_io, err = await call_tool(io_ctx, "fp_get", lib_id=LIB_ID)
    assert not err, in_io
    assert in_io["review"] == "approved" and in_io["geometry_hash"] == got["geometry_hash"]
    erc, err = await call_tool(io_ctx, "run_erc", type="footprint_link_issues")
    assert not err, erc
    assert erc["total"] == 0, erc["violations"]
