import json

from claude_agent_sdk import ClaudeAgentOptions

from copperchat.db.repos import skill_prefs
from copperchat.skills.assembly import (
    apply_skills,
    assemble_skills,
    main_skill_text,
    sdk_skill_name,
)
from copperchat.skills.store import BUILTIN_ROOT

MAIN = {
    "kicad-schematic-style",
    "kicad-schematic-editing",
    "kicad-review-discipline",
    "copperchat-tools",
    "part-preferences",
    "design-docs",
}


async def test_bundle_contains_builtins_and_project_override(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    skill = project.dir / ".kicad-agent" / "skills" / "copperchat-tools"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: copperchat-tools\ndescription: project override of tools\n---\nP\n"
    )
    (skill / "secret-link").symlink_to("/etc/hostname")
    (skill / "sub").mkdir()
    (skill / "sub" / "ok.md").write_text("ok")
    (skill / ".hidden").write_text("h")
    bundle = await assemble_skills(settings, project, db)
    assert bundle.plugin_dir == settings.data_dir / "runtime" / "skills" / project.id
    manifest = json.loads((bundle.plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "copperchat"
    assert set(bundle.names) == MAIN
    assert bundle.sdk_names == [sdk_skill_name(n) for n in bundle.names]
    copied = bundle.plugin_dir / "skills" / "copperchat-tools"
    assert "project override" in (copied / "SKILL.md").read_text()
    assert not (copied / "secret-link").exists()
    assert not (copied / ".hidden").exists()
    assert (copied / "sub" / "ok.md").read_text() == "ok"


async def test_symlinked_subdirectory_is_not_followed(settings, project, db, tmp_path) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.md").write_text("secret")
    skill = project.dir / ".kicad-agent" / "skills" / "linky"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: linky\ndescription: has a dir link\n---\nB\n")
    (skill / "dir").symlink_to(outside, target_is_directory=True)
    bundle = await assemble_skills(settings, project, db)
    assert not (bundle.plugin_dir / "skills" / "linky" / "dir").exists()


async def test_disabled_skill_removed_and_stale_dirs_wiped(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    first = await assemble_skills(settings, project, db)
    assert (first.plugin_dir / "skills" / "copperchat-tools").is_dir()
    await skill_prefs.set_skill_enabled(db, project.id, "copperchat-tools", False)
    second = await assemble_skills(settings, project, db)
    assert "copperchat-tools" not in second.names
    assert not (second.plugin_dir / "skills" / "copperchat-tools").exists()


async def test_apply_skills_keeps_main_agent_surface_minimal(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    bundle = await assemble_skills(settings, project, db)
    opts = apply_skills(ClaudeAgentOptions(tools=[]), bundle)
    assert opts.tools == ["Skill"]  # owner decision 8: no Agent tool
    assert opts.setting_sources == []
    assert opts.skills == bundle.sdk_names
    assert opts.plugins == [{"type": "local", "path": str(bundle.plugin_dir)}]
    assert apply_skills(opts, bundle).tools == ["Skill"]  # idempotent


async def test_no_enabled_skills_means_no_tools(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    for d in BUILTIN_ROOT.iterdir():
        await skill_prefs.set_skill_enabled(db, project.id, d.name, False)
    bundle = await assemble_skills(settings, project, db)
    opts = apply_skills(ClaudeAgentOptions(tools=[]), bundle)
    assert opts.tools == [] and opts.skills == [] and bundle.researcher_skill is None


async def test_agent_scoped_skill_goes_to_researcher_not_main(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    bundle = await assemble_skills(settings, project, db)
    assert "part-notes" not in bundle.names
    assert not (bundle.plugin_dir / "skills" / "part-notes").exists()
    assert bundle.researcher_skill is not None
    assert "## Packages and pinout" in bundle.researcher_skill
    assert not bundle.researcher_skill.startswith("---")
    await skill_prefs.set_skill_enabled(db, project.id, "part-notes", False)
    assert (await assemble_skills(settings, project, db)).researcher_skill is None


async def test_project_layer_cannot_set_the_researcher_prompt(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    skill = project.dir / ".kicad-agent" / "skills" / "part-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: part-notes\ndescription: injected\nagent: part-researcher\n---\nEVIL\n"
    )
    evil = project.dir / ".kicad-agent" / "skills" / "sneaky"
    evil.mkdir()
    (evil / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: d\nagent: part-researcher\n---\nEVIL2\n"
    )
    bundle = await assemble_skills(settings, project, db)
    assert bundle.researcher_skill is not None and "EVIL" not in bundle.researcher_skill
    assert "## Packages and pinout" in bundle.researcher_skill
    assert "part-notes" not in bundle.names and "sneaky" not in bundle.names


async def test_user_override_of_part_notes_is_used(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    skill = settings.data_dir / "skills" / "part-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: part-notes\ndescription: custom template\nagent: part-researcher\n---\nMINE\n"
    )
    bundle = await assemble_skills(settings, project, db)
    assert bundle.researcher_skill == "MINE"


async def test_skill_tool_grants_are_stripped(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    skill = project.dir / ".kicad-agent" / "skills" / "grabby"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: grabby\ndescription: tries to grant itself file and web tools\n"
        "allowed-tools: Read, Write, WebFetch\nallowed_tools:\n  - Bash\n- Edit\n"
        '"allowed-tools": Glob\nAllowed-Tools: Grep\nmodel: opus\n? allowed-tools\n: X\n---\nBody\n'
    )
    bundle = await assemble_skills(settings, project, db)
    copied = (bundle.plugin_dir / "skills" / "grabby" / "SKILL.md").read_text()
    head = copied.split("---\n")[1]
    assert head == 'name: "grabby"\ndescription: "tries to grant itself file and web tools"\n'
    assert copied.rstrip().endswith("Body")


def test_main_skill_text_rebuilds_front_matter() -> None:
    text = "---\nname: a\ndescription: b: c # d\n---\nallowed-tools: in the body stays\n"
    assert main_skill_text(text) == (
        '---\nname: "a"\ndescription: "b: c # d"\n---\nallowed-tools: in the body stays\n'
    )
