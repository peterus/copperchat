"""The ``sessions`` skill key and per-type assembly (sessions C5 Task 2, spec §8.4)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.projects import Project, discover_projects
from copperchat.skills.assembly import assemble_skills, main_skill_text
from copperchat.skills.store import BUILTIN_ROOT, SkillLayers, effective_skills, parse_front_matter
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}

# spec §8.4 (None = every type)
MAPPING: dict[str, frozenset[str] | None] = {
    "copperchat-tools": None,
    "part-preferences": None,
    "design-docs": None,
    "kicad-review-discipline": frozenset({"schematic", "board", "free"}),
    "kicad-schematic-editing": frozenset({"schematic", "free"}),
    "kicad-schematic-style": frozenset({"schematic", "free"}),
    "session-requirements": frozenset({"requirements"}),
    "session-schematic": frozenset({"schematic"}),
}


def _user_skill(settings: Settings, name: str, sessions: str | None) -> None:
    d = settings.data_dir / "skills" / name
    d.mkdir(parents=True)
    key = "" if sessions is None else f"sessions: {sessions}\n"
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test skill {name}\n{key}---\nB\n")


def test_sessions_key_is_parsed(settings: Settings) -> None:
    _user_skill(settings, "only-two", "schematic, Free")
    _user_skill(settings, "everywhere", None)
    _user_skill(settings, "blank", "")
    layers = SkillLayers(BUILTIN_ROOT, settings.data_dir / "skills", None)
    by_name = {s.name: s for s in effective_skills(layers)}
    assert by_name["only-two"].sessions == frozenset({"schematic", "free"})
    assert by_name["everywhere"].sessions is None
    assert by_name["blank"].sessions is None


def test_builtin_mapping_follows_spec_84(settings: Settings) -> None:
    layers = SkillLayers(BUILTIN_ROOT, settings.data_dir / "skills", None)
    by_name = {s.name: s for s in effective_skills(layers)}
    for name, types in MAPPING.items():
        assert by_name[name].sessions == types, name
    assert by_name["part-notes"].agent == "part-researcher"


def test_sessions_key_never_reaches_the_cli() -> None:
    text = "---\nname: x\ndescription: d\nsessions: schematic, free\n---\nbody\n"
    meta, _ = parse_front_matter(main_skill_text(text))
    assert set(meta) == {"name", "description"}


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [
        (
            "requirements",
            {"session-requirements", "design-docs", "copperchat-tools", "part-preferences"},
            {"kicad-schematic-editing", "kicad-schematic-style", "session-schematic"},
        ),
        (
            "schematic",
            {"session-schematic", "kicad-schematic-editing", "kicad-review-discipline"},
            {"session-requirements"},
        ),
        (
            "free",
            {"kicad-schematic-editing", "design-docs", "kicad-review-discipline"},
            {"session-requirements", "session-schematic"},
        ),
    ],
)
async def test_assembly_filters_by_type_into_a_per_type_dir(
    settings: Settings, project: Project, db: Any, kind: str, present: set[str], absent: set[str]
) -> None:
    _user_skill(settings, "only-two", "schematic, free")
    bundle = await assemble_skills(settings, project, db, kind)
    assert bundle.plugin_dir == settings.data_dir / "runtime" / "skills" / project.id / kind
    names = set(bundle.names)
    assert present <= names, sorted(present - names)
    assert not names & absent, sorted(names & absent)
    assert ("only-two" in names) is (kind in ("schematic", "free"))
    on_disk = {p.name for p in (bundle.plugin_dir / "skills").iterdir()}
    assert on_disk == names
    copied = (bundle.plugin_dir / "skills" / "kicad-schematic-style").exists()
    assert copied is (kind != "requirements")
    for name in names:
        text = (bundle.plugin_dir / "skills" / name / "SKILL.md").read_text()
        assert set(parse_front_matter(text)[0]) == {"name", "description"}, name


async def test_types_get_separate_plugin_dirs(
    settings: Settings, project: Project, db: Any
) -> None:
    req = await assemble_skills(settings, project, db, "requirements")
    sch = await assemble_skills(settings, project, db, "schematic")
    assert req.plugin_dir != sch.plugin_dir
    assert (req.plugin_dir / "skills" / "session-requirements").is_dir()
    assert not (sch.plugin_dir / "skills" / "session-requirements").exists()


async def test_unknown_type_is_refused(settings: Settings, project: Project, db: Any) -> None:
    with pytest.raises(ValueError):
        await assemble_skills(settings, project, db, "../x")


def test_skills_dto_carries_sessions(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(dev_fake_agent=True)
    d: Path = settings.projects_root / "board"
    d.mkdir(parents=True)
    (d / "board.kicad_pro").write_text("")
    pid = discover_projects(settings.projects_root)[0].id
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/auth/login", json=GOOD).status_code == 204
        by_name = {s["name"]: s for s in c.get(f"/api/skills?projectId={pid}").json()}
    assert by_name["copperchat-tools"]["sessions"] is None
    assert by_name["kicad-schematic-editing"]["sessions"] == ["free", "schematic"]
    assert by_name["session-requirements"]["sessions"] == ["requirements"]
