import re

from copperchat.agent.profiles import PROFILES
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
    "design-docs": [
        "doc_list",
        "doc_read",
        "base_version",
        "decisions.md",
        "Open points",
        "## Conflicts",
        "front matter",
        "template",
        "stop and ask the user",
    ],
    "session-requirements": [
        "function",
        "interfaces",
        "acceptance",
        "ask",
        "assumption",
        "research_part",
        "Open points",
        "approve",
        "doc_write",
    ],
    "session-schematic": [
        "requirements.md",
        "architecture.md",
        "parts.md",
        "sheet",
        "gate",
        "once",
        "decisions.md",
        "placed",
        "Key parts",
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


TOOL_NAME = re.compile(
    r"`((?:sch|lib|plan|parts|pcb|run|render|research|project|git|doc)_[a-z0-9_]+)"
)
# Session types a user can create; Board (SP4) is hidden and its profile is defined there,
# so skills are not checked against it yet.
ACTIVE_TYPES = ("free", "requirements", "schematic")

# backticked identifiers in the skills that look like tool names but are not
NOT_TOOLS = {
    "lib_id",
    "plan_open",
    "plan_required",
    "plan_step_required",
    "plan_step_exceeded",
    # design-doc error codes (sessions S-C6)
    "doc_path_refused",
    "doc_conflict",
    "doc_user_edited",
    "doc_append_only",
    "doc_scope_refused",
    "doc_heading_refused",
    "doc_frontmatter_refused",
    "doc_too_large",
    "doc_read_too_large",
    "doc_section_not_found",
    "doc_section_ambiguous",
    "doc_limit",
}


def test_skill_tool_names_exist_in_every_profile_they_apply_to() -> None:
    """Each built-in main-agent skill names only tools of the profile of every (active)
    session type it applies to: a Requirements skill never tells the agent to call
    sch_connect (spec §8.4)."""
    known = {s.name for s in registry.all_specs()}
    layers = SkillLayers(
        builtin_root=BUILTIN_ROOT, user_root=BUILTIN_ROOT / "_none", project_root=None
    )
    checked = 0
    for skill in effective_skills(layers):
        if skill.agent is not None:
            continue  # the researcher's skill: not the main agent's tools
        body = (skill.dir / "SKILL.md").read_text()
        mentioned = set(TOOL_NAME.findall(body)) - NOT_TOOLS
        assert mentioned <= known, (skill.name, sorted(mentioned - known))
        types = ACTIVE_TYPES if skill.sessions is None else sorted(skill.sessions)
        for kind in types:
            if kind not in ACTIVE_TYPES:
                continue
            outside = mentioned - PROFILES[kind]
            assert not outside, (skill.name, kind, sorted(outside))
        checked += 1
    assert checked >= 7


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


def test_tools_skill_points_to_the_editing_and_docs_skills() -> None:
    body = (BUILTIN_ROOT / "copperchat-tools" / "SKILL.md").read_text()
    for phrase in (
        "kicad-schematic-editing",
        "`parts_get_notes`",
        "## Design documents",
        "`doc_list`",
    ):
        assert phrase in body, phrase
    assert "## Editing the schematic" not in body
    editing = (BUILTIN_ROOT / "kicad-schematic-editing" / "SKILL.md").read_text()
    for phrase in ("`plan_propose`", "`sch_changes`", "`parts_get_notes`"):
        assert phrase in editing, phrase
