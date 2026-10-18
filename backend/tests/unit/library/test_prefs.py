from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.library.packages import FootprintDetail
from copperchat.library.prefs import (
    PREFERENCES_SKILL,
    active_part_note,
    effective_preferences,
    parse_preferences,
    preference_notes,
    preferences_from_raw,
    seed_user_part_preferences,
)
from copperchat.projects import Project, agent_dir
from copperchat.skills.store import SkillLayers, get_skill, origin_diff, reset, write_user_skill
from tests.unit.phase8.conftest import make_project


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path / "projects" / "proj")


def _doc(block: str) -> str:
    return (
        f"---\nname: {PREFERENCES_SKILL}\ndescription: Part preferences of this layer.\n---\n\n"
        f"Free text: passives_package: 1206 outside the block is ignored.\n\n"
        f"```preferences\n{block}```\n"
    )


def _write_project_layer(project: Project, text: str) -> None:
    d = agent_dir(project) / "skills" / PREFERENCES_SKILL
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text)


def test_header_parsed() -> None:
    raw = parse_preferences(
        _doc(
            "passives_package: 0805\n"
            "preferred_libraries: Device, Connector_Generic ,\n"
            "Assembly: JLCPCB basic parts only\n"
            "preferred_manufacturers:\n"
            "not a key line\n"
        )
    )
    assert raw == {
        "passives_package": "0805",
        "preferred_libraries": "Device, Connector_Generic ,",
        "assembly": "JLCPCB basic parts only",
    }
    prefs = preferences_from_raw(raw)
    assert prefs.passives_package == "0805"
    assert prefs.preferred_libraries == ["Device", "Connector_Generic"]
    assert prefs.preferred_manufacturers == [] and prefs.assembly == "JLCPCB basic parts only"
    assert parse_preferences("no block here") == {}
    assert preferences_from_raw({"passives_package": "0603 (1608 metric)"}).passives_package == (
        "0603"
    )


def test_builtin_template_is_empty(settings: Settings) -> None:
    prefs = effective_preferences(settings, None)
    assert prefs.passives_package is None and prefs.raw == {}


def test_most_specific_layer_wins_per_key(settings: Settings, project: Project) -> None:
    layers = SkillLayers.for_project(settings, project)
    write_user_skill(
        layers,
        PREFERENCES_SKILL,
        _doc("passives_package: 0805\npreferred_libraries: Device, MyLib\n"),
    )
    _write_project_layer(project, _doc("passives_package: 0603\npreferred_libraries:\n"))
    prefs = effective_preferences(settings, project)
    assert prefs.passives_package == "0603"
    assert prefs.preferred_libraries == ["Device", "MyLib"]
    assert effective_preferences(settings, None).passives_package == "0805"


def test_user_layer_seeded_with_0805_once(settings: Settings) -> None:
    assert seed_user_part_preferences(settings) is True
    assert effective_preferences(settings, None).passives_package == "0805"
    layers = SkillLayers.for_project(settings, None)
    skill = get_skill(layers, PREFERENCES_SKILL)
    assert skill.origin == "user" and skill.has_builtin
    assert origin_diff(layers, PREFERENCES_SKILL) is None  # a customised built-in
    # a later user edit is never overwritten
    write_user_skill(layers, PREFERENCES_SKILL, _doc("passives_package: 1206\n"))
    assert seed_user_part_preferences(settings) is False
    assert effective_preferences(settings, None).passives_package == "1206"
    # nor re-seeded after the user reset their copy
    reset(layers, PREFERENCES_SKILL)
    assert seed_user_part_preferences(settings) is False
    assert effective_preferences(settings, None).passives_package is None


def test_seed_keeps_an_existing_user_copy(settings: Settings) -> None:
    layers = SkillLayers.for_project(settings, None)
    write_user_skill(layers, PREFERENCES_SKILL, _doc("passives_package: 0603\n"))
    assert seed_user_part_preferences(settings) is False
    assert effective_preferences(settings, None).passives_package == "0603"


_PREFS = preferences_from_raw({"passives_package": "0805"})


def test_deviation_note_for_passive() -> None:
    notes = preference_notes(_PREFS, "Device:R", "Resistor_SMD:R_0603_1608Metric", None)
    assert len(notes) == 1
    n = notes[0]
    assert n["kind"] == "preference_deviation" and n["expected"] == "0805"
    assert n["actual"] == "0603" and "0805" in n["message"]
    assert n["suggested_footprint"] == "Resistor_SMD:R_0805_2012Metric"
    for lib_id in ("Device:C_Small", "Device:L", "Device:FerriteBead_Small", "Device:R_US"):
        assert preference_notes(_PREFS, lib_id, "C_0603_1608Metric", None), lib_id
    fp = FootprintDetail("Resistor_SMD:R_0603_1608Metric", "", 2, 2, "0603")
    assert preference_notes(_PREFS, "Device:R", fp.lib_id, fp)


def test_no_note_for_unknown_package_or_non_passive() -> None:
    assert preference_notes(_PREFS, "Device:R", "Resistor_SMD:R_0805_2012Metric", None) == []
    assert preference_notes(_PREFS, "Device:R", "Resistor_THT:R_Axial_DIN0207", None) == []
    assert preference_notes(_PREFS, "Device:R", "", None) == []
    assert preference_notes(_PREFS, "Device:LED", "LED_SMD:LED_0603_1608Metric", None) == []
    assert preference_notes(_PREFS, "Device:C_Polarized", "C_0603_1608Metric", None) == []
    array = FootprintDetail("Inductor_SMD:L_Array_1206_3216Metric", "", 4, 4, "1206")
    assert preference_notes(_PREFS, "Device:L", array.lib_id, array) == []  # not a 2-pad chip
    empty = preferences_from_raw({})
    assert preference_notes(empty, "Device:R", "R_0603_1608Metric", None) == []


def test_active_part_note(project: Project) -> None:
    note = active_part_note(project, "U3", {"Value": "TMP102"})
    assert note is not None and note["kind"] == "active_part_without_notes"
    assert note["reason"] == "no_mpn"
    note = active_part_note(project, "U3", {"MPN": "TMP102AIDRLR"})
    assert note is not None and note["reason"] == "not_researched"
    notes = project.dir / "docs" / "parts" / "TMP102AIDRLR" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# TMP102\n")
    assert active_part_note(project, "U3", {"mpn": "TMP102AIDRLR"}) is None
    assert active_part_note(project, "IC2", {"MPN": "TMP102AIDRLR"}) is None
    bad = active_part_note(project, "Q1", {"MPN": "../x"})
    assert bad is not None and bad["reason"] == "invalid_mpn"
    for ref in ("R1", "C7", "D1", "J2", "#PWR01"):
        assert active_part_note(project, ref, {}) is None
