from copperchat.skills.store import BUILTIN_ROOT, SkillLayers, effective_skills, parse_front_matter

EXPECTED = {
    "kicad-schematic-style": ["left-to-right", "labels", "bus"],
    "kicad-review-discipline": ["decoupling", "pull-up", "run_erc", "render_view", "research_part"],
    "copperchat-tools": ["mm", "full UUID", "region", "research_part", "attachment_paths", "mpn"],
    "part-notes": ["If unused", "[D1 p.", "Example:", "check_notes", "KiCad"],
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
