import json
from pathlib import Path

import pytest

from copperchat.skills import store
from copperchat.skills.store import SkillLayers


def skill_md(name: str, desc: str = "Test skill", body: str = "Body\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def put(root: Path, name: str, text: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(text)


@pytest.fixture
def layers(tmp_path: Path) -> SkillLayers:
    b, u, p = tmp_path / "builtin", tmp_path / "user", tmp_path / "proj"
    for d in (b, u, p):
        d.mkdir()
    put(b, "alpha", skill_md("alpha", "builtin alpha"))
    put(b, "beta", skill_md("beta", "builtin beta"))
    return SkillLayers(builtin_root=b, user_root=u, project_root=p)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../x",
        "a/b",
        "a\\b",
        ".hidden",
        "Upper",
        "x" * 65,
        "-lead",
        "trail-",
        "a b",
        "a:b",
        "..",
        "a\x00b",
    ],
)
def test_invalid_names_rejected(bad: str) -> None:
    with pytest.raises(store.InvalidSkillName):
        store.validate_skill_name(bad)


def test_most_specific_layer_wins(layers: SkillLayers) -> None:
    put(layers.user_root, "alpha", skill_md("alpha", "user alpha"))
    assert layers.project_root is not None
    put(layers.project_root, "alpha", skill_md("alpha", "project alpha"))
    by_name = {s.name: s for s in store.effective_skills(layers)}
    assert by_name["alpha"].origin == "project"
    assert by_name["alpha"].description == "project alpha"
    assert by_name["alpha"].shadowed == ("user", "builtin")
    assert by_name["beta"].origin == "builtin"


def test_symlinked_and_mismatched_entries_skipped(layers: SkillLayers, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    put(outside, "evil", skill_md("evil"))
    assert layers.project_root is not None
    (layers.project_root / "evil").symlink_to(outside / "evil", target_is_directory=True)
    (layers.user_root / "gamma").mkdir()
    (layers.user_root / "gamma" / "SKILL.md").symlink_to(outside / "evil" / "SKILL.md")
    put(layers.user_root, "delta", skill_md("not-delta"))
    put(layers.user_root, "Bad_Name", skill_md("Bad_Name"))
    put(layers.user_root, "huge", skill_md("huge", body="x" * (store.MAX_SKILL_BYTES + 1)))
    names = {s.name for s in store.effective_skills(layers)}
    assert names == {"alpha", "beta"}


def test_agent_front_matter_is_exposed(layers: SkillLayers) -> None:
    put(
        layers.user_root,
        "notes",
        "---\nname: notes\ndescription: d\nagent: part-researcher\n---\nB",
    )
    by_name = {s.name: s for s in store.effective_skills(layers)}
    assert by_name["notes"].agent == "part-researcher"
    assert by_name["alpha"].agent is None


def test_customise_copies_and_writes_sidecar(layers: SkillLayers, tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("s")
    (layers.builtin_root / "alpha" / "link").symlink_to(tmp_path / "secret")
    dest = store.customise(layers, "alpha")
    assert dest == layers.user_root / "alpha"
    assert (dest / "SKILL.md").read_text() == skill_md("alpha", "builtin alpha")
    assert not (dest / "link").exists() and not (dest / "link").is_symlink()
    sidecar = json.loads((dest / ".origin.json").read_text())
    assert set(sidecar) == {"builtinSha256", "builtinContent"}
    with pytest.raises(store.SkillConflict):
        store.customise(layers, "alpha")
    with pytest.raises(store.SkillNotFound):
        store.customise(layers, "nope")


def test_origin_updated_diff_and_acknowledge(layers: SkillLayers) -> None:
    store.customise(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin_updated is False
    assert store.origin_diff(layers, "alpha") is None
    put(layers.builtin_root, "alpha", skill_md("alpha", "builtin alpha", "Body\nNew rule\n"))
    assert store.get_skill(layers, "alpha").origin_updated is True
    diff = store.origin_diff(layers, "alpha")
    assert diff is not None and "+New rule" in diff
    store.acknowledge_update(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin_updated is False


def test_corrupt_sidecar_is_not_an_update(layers: SkillLayers) -> None:
    dest = store.customise(layers, "alpha")
    (dest / ".origin.json").write_text("{not json")
    assert store.get_skill(layers, "alpha").origin_updated is False
    assert store.origin_diff(layers, "alpha") is None


def test_write_user_skill_validates_front_matter(layers: SkillLayers) -> None:
    with pytest.raises(store.InvalidSkill):
        store.write_user_skill(layers, "mine", skill_md("other"))
    with pytest.raises(store.InvalidSkill):
        store.write_user_skill(layers, "mine", "no front matter")
    with pytest.raises(store.InvalidSkillName):
        store.write_user_skill(layers, "../escape", skill_md("escape"))
    path = store.write_user_skill(layers, "mine", skill_md("mine"))
    assert path == layers.user_root / "mine" / "SKILL.md"
    assert not (layers.user_root.parent / "escape").exists()


def test_symlinked_user_entries_are_refused(layers: SkillLayers, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (layers.user_root / "mine").symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.InvalidSkill):
        store.write_user_skill(layers, "mine", skill_md("mine"))
    with pytest.raises(store.InvalidSkill):
        store.reset(layers, "mine")
    assert list(outside.iterdir()) == []
    assert outside.exists()


def test_reset_removes_user_layer(layers: SkillLayers) -> None:
    store.customise(layers, "alpha")
    store.reset(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin == "builtin"
    with pytest.raises(store.SkillNotFound):
        store.reset(layers, "alpha")
