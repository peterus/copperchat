from collections.abc import Callable
from pathlib import Path

import pytest

from copperchat.research.agent import RESEARCHER_PROMPT, researcher_prompt
from copperchat.research.notes import (
    REQUIRED_SECTIONS,
    check_notes,
    template_from_skill,
    validate_notes,
)
from copperchat.skills.store import BUILTIN_ROOT, parse_front_matter

SKILL = (BUILTIN_ROOT / "part-notes" / "SKILL.md").read_text()
GOOD = (Path(__file__).parents[2] / "fixtures/parts/CC1234/notes.md").read_text()


def test_skill_is_agent_scoped_and_contains_template() -> None:
    meta, _body = parse_front_matter(SKILL)
    assert meta["name"] == "part-notes" and meta["agent"] == "part-researcher"
    template = template_from_skill(SKILL)
    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in template
    assert validate_notes(template, structure_only=True) == []
    assert any("placeholder" in i for i in validate_notes(template))  # unfilled ≠ notes


def test_good_fixture_scores_full() -> None:
    report = check_notes(GOOD, mpn="CC1234")
    assert report.issues == [] and report.score == 1.0
    assert report.total >= 30


def test_wrong_mpn_in_front_matter_is_reported() -> None:
    assert any("mpn" in i for i in validate_notes(GOOD, mpn="LM358"))


def _drop_section(text: str, name: str) -> str:
    head, rest = text.split(f"## {name}\n", 1)
    nxt = rest.find("\n## ")
    return head + (rest[nxt + 1 :] if nxt >= 0 else "")


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_missing_section_is_reported(section: str) -> None:
    issues = validate_notes(_drop_section(GOOD, section))
    assert any(section in i for i in issues), issues


def _swap_sections(t: str) -> str:
    a = t.index("## Absolute maximum ratings")
    b = t.index("## Recommended operating conditions")
    c = t.index("## Typical application circuit")
    return t[:a] + t[b:c] + t[a:b] + t[c:]


MUTATIONS: list[tuple[Callable[[str], str], str]] = [
    (lambda t: t.replace("| If unused |", "| Notes |", 1), "If unused"),
    (lambda t: t.replace("| tie to VIN for always-on (max 18 V) |", "|  |"), "If unused"),
    (lambda t: t.replace("[D1 p.", "[D1 page "), "page"),
    (lambda t: t.replace("Example:", "E.g.:"), "Example"),
    (lambda t: t.replace("| KiCad footprint |", "| Footprint |"), "KiCad footprint"),
    (lambda t: t.replace("Package_TO_SOT_SMD:SOT-23-6", "SOT-23-6 standard"), "Library:Footprint"),
    (lambda t: t.replace("user_edited: false\n", ""), "user_edited"),
    (lambda t: t + "\n<fill me>\n", "placeholder"),
    (lambda t: t.replace("### SOT-23-6", "SOT-23-6"), "### <package>"),
    (_swap_sections, "order"),
    (lambda t: t.replace("- [D1]", "- [X1]"), "[D1]"),
]


@pytest.mark.parametrize(("mutate", "needle"), MUTATIONS)
def test_degraded_notes_lose_score(mutate: Callable[[str], str], needle: str) -> None:
    report = check_notes(mutate(GOOD))
    assert report.score < 1.0 and any(needle in i for i in report.issues), report.issues


def test_values_without_units_fail_the_concrete_value_check() -> None:
    app_head, rest = GOOD.split("## Typical application circuit\n", 1)
    section, tail = rest.split("\n## Design equations", 1)
    lines = [
        ln for ln in section.splitlines() if not ln.startswith("| C") and not ln.startswith("| L")
    ]
    lines = [ln.replace("kΩ", "k") for ln in lines]
    text = (
        app_head
        + "## Typical application circuit\n"
        + "\n".join(lines)
        + "\n## Design equations"
        + tail
    )
    assert any("concrete component value" in i for i in validate_notes(text))


def test_prose_comparisons_are_not_placeholders() -> None:
    text = GOOD.replace(
        "## Errata and pitfalls\n", "## Errata and pitfalls\n\n- Keep VIN < 18 V and VOUT > 0.6 V\n"
    )
    assert check_notes(text).score == 1.0


def test_researcher_prompt_carries_the_template() -> None:
    _, body = parse_front_matter(SKILL)
    prompt = researcher_prompt(body)
    assert prompt.startswith(RESEARCHER_PROMPT)
    assert "## Packages and pinout" in prompt and "If unused" in prompt and "[D1 p." in prompt
    assert researcher_prompt(None) == RESEARCHER_PROMPT
    assert "docs/parts/<MPN>/notes.md" in RESEARCHER_PROMPT
