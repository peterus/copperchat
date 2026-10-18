import re

from copperchat.skills.store import BUILTIN_ROOT, SkillLayers, effective_skills, parse_front_matter
from copperchat.tools import registry

EXPECTED = {
    "kicad-schematic-style": ["left-to-right", "labels", "bus"],
    "kicad-schematic-editing": [
        "## Tool order",
        "## Addressing pins",
        "## Rails and labels",
        "## Plans",
        "## Self-check",
        "## Missing parts",
        "parts_get_notes",
        "research_part",
        "sch_find_space",
        "pin_directions_by_rotation",
        "PWR_FLAG",
        "unverified",
        "preference_deviation",
        "justif",
    ],
    "kicad-review-discipline": ["decoupling", "pull-up", "run_erc", "render_view", "research_part"],
    "copperchat-tools": ["mm", "full UUID", "region", "research_part", "attachment_paths", "mpn"],
    "part-notes": ["If unused", "[D1 p.", "Example:", "check_notes", "KiCad"],
    "part-preferences": [
        "```preferences",
        "passives_package:",
        "preferred_libraries:",
        "deviation",
    ],
}


def test_builtin_skills_present_and_valid(tmp_path) -> None:  # type: ignore[no-untyped-def]
    for name, phrases in EXPECTED.items():
        text = (BUILTIN_ROOT / name / "SKILL.md").read_text()
        meta, body = parse_front_matter(text)
        assert meta["name"] == name
        assert len(meta["description"]) > 40
        for phrase in phrases:
            assert phrase.lower() in body.lower(), (name, phrase)
    layers = SkillLayers(builtin_root=BUILTIN_ROOT, user_root=tmp_path, project_root=None)
    assert {s.name for s in effective_skills(layers)} == set(EXPECTED)


def test_main_agent_skills_never_mention_the_agent_tool() -> None:
    for name in ("kicad-review-discipline", "copperchat-tools"):
        body = (BUILTIN_ROOT / name / "SKILL.md").read_text()
        assert "subagent_type" not in body and "Agent tool" not in body


EDITING_SKILLS = ("kicad-schematic-editing", "kicad-schematic-style", "copperchat-tools")
TOOL_NAME = re.compile(
    r"`((?:sch|lib|plan|parts|pcb|run|render|research|project|git)_[a-z0-9_]+)"
)

# backticked identifiers in the skills that look like tool names but are not
NOT_TOOLS = {"lib_id", "plan_open", "plan_required", "plan_step_required"}


def test_skill_tool_names_exist() -> None:
    known = {s.name for s in registry.all_specs()} | NOT_TOOLS
    for name in EDITING_SKILLS:
        body = (BUILTIN_ROOT / name / "SKILL.md").read_text()
        mentioned = set(TOOL_NAME.findall(body))
        assert mentioned, name
        assert mentioned <= known, (name, sorted(mentioned - known))


def test_editing_skill_mentions_every_editing_tool() -> None:
    body = (BUILTIN_ROOT / "kicad-schematic-editing" / "SKILL.md").read_text()
    editing = {
        s.name
        for s in registry.all_specs()
        if s.name.startswith(("sch_", "lib_", "plan_")) and s.name != "sch_get_symbol"
    }
    missing = {n for n in editing if f"`{n}" not in body}
    assert not missing, sorted(missing)


def test_style_skill_has_placement_recipes() -> None:
    body = (BUILTIN_ROOT / "kicad-schematic-style" / "SKILL.md").read_text().lower()
    assert "## placement recipes" in body
    for phrase in (
        "next to the supply pin",
        "input on the left",
        "mcu in the centre",
        "sheet edge",
        "pull-ups above",
        "pwr_flag",
    ):
        assert phrase in body, phrase


def test_tools_skill_lists_editing_tools() -> None:
    body = (BUILTIN_ROOT / "copperchat-tools" / "SKILL.md").read_text()
    for phrase in ("kicad-schematic-editing", "`parts_get_notes`", "`plan_propose`", "`sch_changes`"):
        assert phrase in body, phrase
