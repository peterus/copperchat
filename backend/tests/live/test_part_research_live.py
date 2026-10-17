"""Opt-in eval: the real part-researcher writes notes that meet the part-notes template.

Costly (web research, several minutes): run deliberately, e.g.
``uv run pytest tests/live/test_part_research_live.py -m live_sdk -s``.
"""

from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.events import AgentEvent
from copperchat.research.notes import check_notes
from copperchat.research.runner import ResearchRunner
from copperchat.skills.store import BUILTIN_ROOT, parse_front_matter
from tests.unit.phase8.conftest import make_project

from .conftest import live, live_settings

pytestmark = live


@pytest.mark.timeout(1200)
async def test_researcher_writes_template_complete_notes(tmp_path: Path) -> None:
    project = make_project(tmp_path / "proj")
    _, notes_skill = parse_front_matter((BUILTIN_ROOT / "part-notes" / "SKILL.md").read_text())
    events: list[AgentEvent] = []
    runner = ResearchRunner(
        project, live_settings(tmp_path), emit=events.append, notes_skill=notes_skill.strip()
    )
    args: dict[str, Any] = {
        "mpn": "TPS62133",
        "query": "Pinout (VQFN-16), application circuit for 12 V in / 3.3 V 2 A out with "
        "component values, and layout rules.",
    }
    runner.note_call(args, "toolu_live")
    summary = await runner.run(args)
    usage = runner.take_usage()
    notes = (project.dir / "docs/parts/TPS62133/notes.md").read_text()
    report = check_notes(notes, mpn="TPS62133")
    print(f"\nscore={report.score:.3f} ({report.passed}/{report.total}) cost={usage}")
    print("issues:", report.issues)
    print("summary:", summary)
    assert (project.dir / "docs/parts/TPS62133/datasheet.pdf").is_file()
    assert events and all(getattr(e, "parent_tool_use_id", None) == "toolu_live" for e in events)
    assert report.score >= 0.9, report.issues
    pinout = notes.split("## Packages and pinout", 1)[1].split("\n## ", 1)[0]
    for pin in ("SW", "EN", "PG", "FB"):
        assert pin in pinout, pin
    app = notes.split("## Typical application circuit", 1)[1].split("\n## ", 1)[0]
    assert "µH" in app or "uH" in app
