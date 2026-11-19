import re
from pathlib import Path

from copperchat.agent.profiles import FP_READ, FP_WRITES, PROFILES
from copperchat.agent.sdk_backend import system_prompt
from copperchat.research.agent import RESEARCHER_PROMPT
from copperchat.research.notes import REQUIRED_SECTIONS, check_notes, template_from_skill
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
    "kicad-footprints": [
        "## Library rule",
        "## No invented dimensions",
        "## Procedure",
        "## Checking existing footprints",
        "## Memory and time",
        "## Errors",
        "Approve this footprint?",
        "stop the turn",
        "user_quote",
        "allow_unmatched_pads",
        "footprint_libraries_loaded",
        "0.25 mm",
        "parts_get_notes",
        "sch_set_fields",
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
    r"`((?:sch|lib|plan|parts|pcb|run|render|research|project|git|doc|fp)_[a-z0-9_]+)"
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
    # footprint error code and note field (footprints FP-C9)
    "render_failed",
    "lib_ids",
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


FP_TOOLS = sorted(FP_READ | FP_WRITES)
# FP-C9 (footprints plan): every error code and note kind the footprint tools return.
FP_ERROR_CODES = (
    "not_found",
    "invalid_argument",
    "library_error",
    "footprint_tools_disabled",
    "seed_missing",
    "repo_root_unknown",
    "library_conflict",
    "not_local",
    "already_exists",
    "check_failed",
    "render_failed",
    "verification_failed",
    "document_inconsistent",
    "approval_not_allowed",
    "in_use",
)
FP_NOTE_KINDS = (
    "footprint_libraries_loaded",
    "footprint_needs_review",
    "footprint_review_unknown",
    "footprint_unverified",
    "possibly_in_use_elsewhere",
    "in_use_on_board",
    "model_missing",
    "pending_cleaned",
    "pending_leftover",
    "footprint_attributes_unavailable",
    "no_local_library",
    "footprint_unreadable",
    "strings_truncated",
)
FOOTPRINTS = BUILTIN_ROOT / "kicad-footprints" / "SKILL.md"


def test_footprints_skill_front_matter() -> None:
    meta, _ = parse_front_matter(FOOTPRINTS.read_text())
    assert meta["name"] == "kicad-footprints"
    assert meta["description"].startswith(
        "Read, check, create, render, approve and delete footprints; library order; "
        "no invented dimensions."
    )
    assert meta["sessions"] == "schematic, free"


def test_footprints_skill_names_only_existing_tools_and_every_fp_tool() -> None:
    body = FOOTPRINTS.read_text()
    known = {s.name for s in registry.all_specs()}
    mentioned = set(TOOL_NAME.findall(body)) - NOT_TOOLS
    assert mentioned <= known, sorted(mentioned - known)
    assert {s.name for s in registry.all_specs() if s.name.startswith("fp_")} == set(FP_TOOLS)
    missing = [n for n in FP_TOOLS if f"`{n}`" not in body and f"`{n}(" not in body]
    assert not missing, missing


def test_footprints_skill_explains_every_error_code_and_note() -> None:
    body = FOOTPRINTS.read_text()
    errors = body.split("## Errors", 1)[1]
    for code in FP_ERROR_CODES:
        assert re.search(rf"^- `{code}`", errors, re.M), code
    for kind in FP_NOTE_KINDS:
        assert f"`{kind}`" in body, kind


def test_footprints_skill_states_the_as_built_result_fields() -> None:
    body = FOOTPRINTS.read_text()
    for phrase in (
        "svg_url",
        "scale_px_per_mm",
        "extent_mm",
        "origin_mm",
        "grid_mm",
        "geometry_hash",
        "used_by",
        "passed",
        "findings",
        "needs_user_review",
        "`Description`",
        "`source`",
        "`symbol`",
        "`expected`",
        "in_use",
    ):
        assert phrase in body, phrase


def test_editing_skill_points_to_the_footprints_skill() -> None:
    body = (BUILTIN_ROOT / "kicad-schematic-editing" / "SKILL.md").read_text()
    assert "not something you can do" not in body
    assert "`kicad-footprints`" in body
    assert "symbols cannot be created" in body
    for kind in (
        "footprint_needs_review",
        "footprint_review_unknown",
        "footprint_libraries_loaded",
        "footprint_unverified",
    ):
        assert f"`{kind}`" in body, kind
    assert "this project has no board" not in body


def test_tools_and_schematic_session_skills_list_the_footprint_tools() -> None:
    for name in ("copperchat-tools", "session-schematic"):
        body = (BUILTIN_ROOT / name / "SKILL.md").read_text()
        for tool in FP_TOOLS:
            assert tool in body, (name, tool)
        assert "kicad-footprints" in body, name
        assert "Schematic and Free" in body, name


def test_main_prompt_points_editing_sessions_to_the_footprints_skill() -> None:
    for kind in ("schematic", "free"):
        assert "kicad-footprints" in system_prompt("demo", kind), kind
    assert "kicad-footprints" not in system_prompt("demo", "requirements")


def test_main_prompt_drops_footprints_when_the_tools_are_off() -> None:
    for kind in ("schematic", "free"):
        prompt = system_prompt("demo", kind, footprint_tools=False)
        assert "kicad-footprints" not in prompt and "fp_*" not in prompt, kind
        assert "{footprints}" not in prompt, kind
        assert "kicad-schematic-editing" in prompt, kind


GOOD_NOTES = (Path(__file__).parents[2] / "fixtures/parts/CC1234/notes.md").read_text()
LAND_PATTERN = """## Land pattern

| Pad | X (mm) | Y (mm) | W (mm) | H (mm) | Drill (mm) | Source |
|---|---|---|---|---|---|---|
| 1 | -0.95 | 1.1 | 0.6 | 1.0 | - | [D1 p.18] |
| 2 | 0 | 1.1 | 0.6 | 1.0 | - | [D1 p.18] |

- Courtyard: 3.6 x 3.2 mm [D1 p.18]
- Body: 2.9 x 1.6 mm [D1 p.17]

"""


def test_part_notes_land_pattern_is_optional() -> None:
    assert "Land pattern" not in REQUIRED_SECTIONS
    assert "## Land pattern" not in GOOD_NOTES
    assert check_notes(GOOD_NOTES, mpn="CC1234").issues == []
    with_lp = GOOD_NOTES.replace("## Errata and pitfalls", LAND_PATTERN + "## Errata and pitfalls")
    assert "## Land pattern" in with_lp
    assert check_notes(with_lp, mpn="CC1234").issues == []
    broken = with_lp.replace("| Pad | X (mm) |", "| Pin | X (mm) |")
    assert any("Land pattern" in i for i in check_notes(broken, mpn="CC1234").issues)
    uncited = with_lp.replace("| - | [D1 p.18] |\n| 2", "| - | |\n| 2")
    assert uncited != with_lp
    assert any("page reference" in i for i in check_notes(uncited, mpn="CC1234").issues)
    header_only = with_lp.replace(
        "| 1 | -0.95 | 1.1 | 0.6 | 1.0 | - | [D1 p.18] |\n"
        "| 2 | 0 | 1.1 | 0.6 | 1.0 | - | [D1 p.18] |\n",
        "",
    )
    assert header_only != with_lp
    assert any("one row per pad" in i for i in check_notes(header_only, mpn="CC1234").issues)
    for label in ("Courtyard", "Body"):
        no_cite = re.sub(rf"(- {label}: [^\n]*?) \[D1 p\.\d+\]", r"\1", with_lp)
        assert no_cite != with_lp, label
        issues = check_notes(no_cite, mpn="CC1234").issues
        assert any(f"'- {label}:" in i for i in issues), (label, issues)


def test_part_notes_skill_describes_the_land_pattern() -> None:
    skill = (BUILTIN_ROOT / "part-notes" / "SKILL.md").read_text()
    template = template_from_skill(skill)
    assert "## Land pattern" in template
    assert "| Pad | X (mm) | Y (mm) | W (mm) | H (mm) | Drill (mm) |" in template
    assert "<repo-nick>:<name>" in skill
    assert "## Land pattern" in RESEARCHER_PROMPT
