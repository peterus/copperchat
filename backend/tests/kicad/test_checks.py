import hashlib
from pathlib import Path

import pytest

from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools import checks
from copperchat.tools.spec import ToolContext

pytestmark = pytest.mark.kicad


def _hashes(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and not p.name.endswith(".lck") and ".kicad-agent" not in p.parts
    }


def ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


async def test_run_drc_and_erc(ecc83_session: KicadSession) -> None:
    before = _hashes(ecc83_session.project.dir)
    drc = await checks.run_drc(ctx(ecc83_session), {})
    erc = await checks.run_erc(ctx(ecc83_session), {})
    assert drc.data is not None and drc.data["count"] == len(drc.data["violations"])
    assert erc.data is not None and erc.data["count"] == len(erc.data["violations"])
    # Stock and project-local libraries resolve (seeded global tables, KIPRJMOD):
    # no library noise in either report.
    types = {v["type"] for v in drc.data["violations"] + erc.data["violations"]}
    assert not types & {"lib_symbol_issues", "footprint_link_issues", "lib_footprint_issues"}
    assert {"silk_edge_clearance", "footprint_symbol_mismatch"} <= types
    # The checks run on saved copies: the project's own files are untouched.
    assert _hashes(ecc83_session.project.dir) == before


async def test_filters(ecc83_session: KicadSession) -> None:
    only = await checks.run_drc(ctx(ecc83_session), {"type": "silk_*", "severity": "warning"})
    assert only.data is not None and only.data["count"] >= 1
    assert only.data["total"] > only.data["count"]
    assert {v["type"] for v in only.data["violations"]} == {"silk_edge_clearance"}


async def test_hierarchical_erc_sees_every_sheet(hier_session: KicadSession) -> None:
    raw = await checks.run_kicad_check(hier_session, "sch", hier_session.settings)
    assert '"ampli_ht_vertical' in raw or "/ampli_ht_vertical/" in raw
